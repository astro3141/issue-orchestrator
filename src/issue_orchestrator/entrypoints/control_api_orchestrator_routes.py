"""Control Center orchestrator management routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..execution.control_center_actions import (
    DoctorActionRequest,
    RefreshActionRequest,
    RepoActionRequest,
)
from ..execution.control_center_runtime import (
    confirm_orchestrator_at_port,
    detect_repository_orchestrators,
    enrich_runtime_health,
    get_selected_launch_selection,
)
from ..execution.repository_engine_status_payload import (
    build_orphaned_engine_status,
)
from ..infra.config import Config, get_config_path
from ..infra.repo_guardrails import (
    RepoGuardrailsError,
    setup_repo_guardrails,
)
from ..infra.supervisor import (
    DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    MultiInstanceStatus,
)
from ..ports.repository_engine_supervisor import (
    RUNNING_SUPERVISOR_STATE,
    EngineStopDisposition,
    SupervisorOps,
)
from .control_api_orchestrator_support import (
    ControlApiOrchestratorDependency,
    EngineLeftRunning,
    EngineStopRequest,
    OrphanedEnginePayload,
    engines_left_running_payload,
    read_last_n_lines,
    run_engine_stop,
    serialize_guardrails_result,
)
from .shutdown_reason_support import parse_shutdown_reason

logger = logging.getLogger(__name__)

control_orchestrator_router = APIRouter()

RECONCILE_ACTOR = "control-center.reconcile"
# Reconcile sweeps every registered repository in one request, so it cannot
# inherit the 120 s per-engine engine-shutdown budget: an operator clicking
# "clean recovery state" would wait minutes per orphan and, without force
# authorization, stop nothing at the end of it (#326).
RECONCILE_GRACEFUL_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class OrphanedEngine:
    """One engine holding a port this repository's locks do not claim."""

    repo_root: str
    port: int | None
    active_selection: Any

    @classmethod
    def from_detection(
        cls, repo_path: Path, detected: Mapping[str, Any]
    ) -> OrphanedEngine:
        # ``detected`` is an untyped probe result. The port is the only
        # handle a stop has on an untracked engine, so it is narrowed
        # here rather than carried as ``Any`` to ``stop_by_port``.
        port = detected.get("port")
        return cls(
            repo_root=str(repo_path),
            port=port if isinstance(port, int) else None,
            active_selection=detected.get("active_selection")
            or detected.get("probed_selection"),
        )

    def to_payload(self) -> OrphanedEnginePayload:
        return {
            "repo_root": self.repo_root,
            "port": self.port,
            "active_selection": self.active_selection,
        }


@dataclass(frozen=True)
class RepoReconciliation:
    """What reconciling one repository observed, changed, and left running.

    It keeps the supervisor's own dispositions rather than a bare list
    of surviving engines, because the reason an engine is still up is
    part of the answer and only the stop that watched it knows it
    (#326).
    """

    reconciled_stale_lock: bool = False
    orphaned_detected: tuple[OrphanedEngine, ...] = ()
    stopped_orphaned: bool = False
    unresponsive_detected: tuple[dict[str, Any], ...] = ()
    stopped_unresponsive: bool = False
    stop_dispositions: tuple[EngineStopDisposition, ...] = ()

    def engines_left_running(self, repo_root: str) -> tuple[EngineLeftRunning, ...]:
        """Every engine this repository's stops could not confirm gone."""
        return tuple(
            EngineLeftRunning(
                repo_root=repo_root,
                engine=engine,
                outcome=disposition.outcome,
            )
            for disposition in self.stop_dispositions
            for engine in disposition.still_running
        )


def _normalize_config_name(raw: object) -> str | None:
    """Normalize a repo config name while preventing path traversal."""
    if raw in (None, ""):
        config_name = "default.yaml"
    elif isinstance(raw, str):
        config_name = raw
    else:
        return None

    if not config_name.endswith(".yaml"):
        config_name += ".yaml"

    config_path = Path(config_name)
    if (
        config_path.is_absolute()
        or config_path.name != config_name
        or config_name == ".yaml"
    ):
        return None
    return config_name


@control_orchestrator_router.post("/control/orchestrator/start")
async def control_start(  # noqa: C901, PLR0912 - startup orchestration spans validation and supervisor handoff
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Start an orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    port = body.get("port")
    if port is not None and (not isinstance(port, int) or port < 1 or port > 65535):
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode"),
            config_name=body.get("config_name"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    from ..execution.repository_engine_start import RepositoryEngineStartRequest

    result = deps.get_control_actions().start_repo_engine_cmd.execute(
        RepositoryEngineStartRequest(
            repo_root=repo_root,
            selection=selection,
            port=port,
            force_restart=bool(body.get("force_restart", False)),
            start_paused=bool(body.get("start_paused", False)),
            actor="control-center",
        )
    )
    if result.status_code == 200:
        deps.track_launched_pids(result.payload)
    return JSONResponse(dict(result.payload), status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/stop")
async def control_stop(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Stop the orchestrator for a repository."""
    sv = deps.get_supervisor()

    logger.info("[control_stop] Received stop request")

    try:
        body = await request.json()
        logger.info("[control_stop] Body: %s", body)
    except json.JSONDecodeError:
        logger.error("[control_stop] Invalid JSON")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        logger.error("[control_stop] Invalid repo_root: %s", body.get("repo_root"))
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    parsed = parse_shutdown_reason(
        body,
        endpoint="/control/orchestrator/stop",
        default_actor="control-center.stop",
    )
    if isinstance(parsed, JSONResponse):
        logger.error("[control_stop] Missing 'reason' in body")
        return parsed
    reason = parsed.reason
    actor = parsed.actor

    force = body.get("force", False)
    force_if_timeout = bool(body.get("force_if_timeout", True))
    graceful_timeout_seconds = deps.coerce_graceful_timeout_seconds(
        body.get("graceful_timeout_seconds"),
        DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    )
    port_override = body.get("port")
    if port_override is not None and (
        not isinstance(port_override, int) or port_override < 1 or port_override > 65535
    ):
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    if deps.global_shutdown_in_progress():
        return JSONResponse(
            {
                "error": "global_shutdown_in_progress",
                "detail": "Global shutdown is in progress and already controls engine shutdown behavior.",
                "actions": [
                    "View global shutdown status",
                    "Change global shutdown",
                    "Abort global shutdown",
                ],
            },
            status_code=409,
        )

    deps.begin_engine_shutdown_operation(
        repo_root,
        bool(force),
        force_if_timeout,
        graceful_timeout_seconds,
    )

    logger.info(
        "[control_stop] Calling supervisor.stop(%s, force=%s)", repo_root, force
    )

    try:
        response = await run_engine_stop(
            sv,
            EngineStopRequest(
                repo_root=repo_root,
                reason=reason,
                actor=actor,
                force=bool(force),
                force_if_timeout=force_if_timeout,
                graceful_timeout_seconds=graceful_timeout_seconds,
                port_override=port_override,
            ),
            confirm_port=confirm_orchestrator_at_port,
        )
        logger.info(
            "[control_stop] status=%d payload=%s",
            response.status_code,
            response.payload,
        )
        return JSONResponse(
            dict(response.payload),
            status_code=response.status_code,
        )
    finally:
        deps.finish_engine_shutdown_operation(repo_root)


@control_orchestrator_router.post("/control/orchestrator/reconcile")
async def control_reconcile(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Reconcile stale runtime metadata and optionally stop orphaned/unresponsive engines."""
    from ..infra.repo_registry import list_repos

    sv = deps.get_supervisor()
    stop_orphaned, stop_unresponsive, force = await _parse_reconcile_options(request)

    reconciled_stale_locks: list[str] = []
    orphaned_detected: list[OrphanedEnginePayload] = []
    stopped_orphaned: list[str] = []
    unresponsive_detected: list[dict[str, Any]] = []
    stopped_unresponsive: list[str] = []
    left_running: list[EngineLeftRunning] = []

    for repo in list_repos():
        selection = repo.launch_selection
        # Reconciling one repository is blocking supervisor work with a
        # graceful budget in it. Awaiting it on the event loop would freeze
        # status polling, SSE, and the spinner the operator is watching.
        reconciliation = await asyncio.to_thread(
            _reconcile_repo_runtime,
            sv=sv,
            repo_path=Path(repo.path),
            selected_config=selection.config.value,
            selected_mode=selection.mode.value,
            stop_orphaned=stop_orphaned,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )
        if reconciliation is None:
            continue

        if reconciliation.reconciled_stale_lock:
            reconciled_stale_locks.append(repo.path)
        orphaned_detected.extend(
            orphan.to_payload() for orphan in reconciliation.orphaned_detected
        )
        if reconciliation.stopped_orphaned:
            stopped_orphaned.append(repo.path)
        unresponsive_detected.extend(reconciliation.unresponsive_detected)
        if reconciliation.stopped_unresponsive:
            stopped_unresponsive.append(repo.path)
        left_running.extend(reconciliation.engines_left_running(repo.path))

    return JSONResponse(
        {
            "status": "ok",
            "reconciled_stale_locks": reconciled_stale_locks,
            "orphaned_detected": orphaned_detected,
            "stopped_orphaned": stopped_orphaned,
            "unresponsive_detected": unresponsive_detected,
            "stopped_unresponsive": stopped_unresponsive,
            # An engine reconcile asked to stop and could not confirm gone is
            # reported here — with the outcome of the stop that left it there
            # and the one sentence the stop owner derives from it — so the
            # surface can neither render a clean success nor name a reason of
            # its own for a sweep that left engines running (#326).
            **engines_left_running_payload(left_running),
        }
    )


async def _parse_reconcile_options(request: Request) -> tuple[bool, bool, bool]:
    stop_orphaned = False
    stop_unresponsive = False
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            stop_orphaned = bool(body.get("stop_orphaned", False))
            stop_unresponsive = bool(body.get("stop_unresponsive", False))
            force = bool(body.get("force", False))
    except Exception:
        pass
    return stop_orphaned, stop_unresponsive, force


def _reconcile_stop(
    sv: SupervisorOps,
    repo_path: Path,
    *,
    force: bool,
    reason: str,
    instance_id: str | None = None,
) -> EngineStopDisposition:
    """Stop one tracked engine under reconcile's own stop policy.

    Reconcile sweeps every registered repository, so it cannot inherit
    the engine-shutdown default of 120 s per engine, and it never
    escalates on its own: an operator who did not ask for force does
    not get a signal sent on their behalf (#326).
    """
    return sv.stop(
        repo_path,
        force=force,
        instance_id=instance_id,
        reason=reason,
        actor=RECONCILE_ACTOR,
        graceful_timeout_seconds=RECONCILE_GRACEFUL_TIMEOUT_SECONDS,
        force_if_graceful_fails=False,
    )


def _stop_orphaned_engines(
    *,
    sv: SupervisorOps,
    detected: tuple[OrphanedEngine, ...],
    stop_orphaned: bool,
    force: bool,
) -> RepoReconciliation:
    """Report (and optionally stop) engines this repository does not track."""
    if not stop_orphaned:
        return RepoReconciliation(orphaned_detected=detected)

    dispositions = tuple(
        sv.stop_by_port(
            orphan.port,
            force=force,
            reason="reconcile-runtime: stop orphaned orchestrator with no lock",
            actor=RECONCILE_ACTOR,
            graceful_timeout_seconds=RECONCILE_GRACEFUL_TIMEOUT_SECONDS,
            force_if_graceful_fails=False,
        )
        for orphan in detected
        if orphan.port
    )
    return RepoReconciliation(
        orphaned_detected=detected,
        stopped_orphaned=bool(dispositions)
        and all(disposition.stopped for disposition in dispositions),
        stop_dispositions=dispositions,
    )


def _reconcile_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    selected_config: str,
    selected_mode: str,
    stop_orphaned: bool,
    stop_unresponsive: bool,
    force: bool,
) -> RepoReconciliation | None:
    """Reconcile one repository and state what it changed and left running."""
    if not repo_path.exists():
        return None

    multi_status = sv.status_all_instances(
        repo_path,
        config_name=selected_config,
        **({"mode": selected_mode} if selected_mode != "default" else {}),
    )
    tracked_ports = {
        status.port
        for status in multi_status.instances
        if status.state == RUNNING_SUPERVISOR_STATE and status.port
    }
    orphans = _stop_orphaned_engines(
        sv=sv,
        detected=tuple(
            OrphanedEngine.from_detection(repo_path, detected)
            for detected in detect_repository_orchestrators(repo_path)
            if detected.get("port") not in tracked_ports
        ),
        stop_orphaned=stop_orphaned,
        force=force,
    )
    tracked = (
        _reconcile_multi_instance_repo_runtime(
            sv=sv,
            repo_path=repo_path,
            multi_status=multi_status,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )
        if _is_multi_instance_repo(multi_status)
        else _reconcile_single_instance_repo_runtime(
            sv=sv,
            repo_path=repo_path,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )
    )
    return replace(
        tracked,
        orphaned_detected=orphans.orphaned_detected,
        stopped_orphaned=orphans.stopped_orphaned,
        stop_dispositions=orphans.stop_dispositions + tracked.stop_dispositions,
    )


def _reconcile_single_instance_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    stop_unresponsive: bool,
    force: bool,
) -> RepoReconciliation:
    """Reconcile the single tracked engine of one repository."""
    status_info = sv.status(repo_path)
    if status_info.state == "failed":
        disposition = _reconcile_stop(
            sv,
            repo_path,
            force=False,
            reason="reconcile-runtime: stale lock for failed orchestrator",
        )
        return RepoReconciliation(
            reconciled_stale_lock=disposition.stopped,
            stop_dispositions=(disposition,),
        )
    if status_info.state != RUNNING_SUPERVISOR_STATE:
        return RepoReconciliation()

    payload = enrich_runtime_health(repo_path, status_info.to_dict())
    if payload is None or payload.get("runtime_health") != "unresponsive":
        return RepoReconciliation()

    unresponsive_detected = (
        {
            "repo_root": str(repo_path),
            "instance_id": None,
            "heartbeat_age_seconds": payload.get("heartbeat_age_seconds"),
            "pid": payload.get("pid"),
            "port": payload.get("port"),
        },
    )
    if not stop_unresponsive:
        return RepoReconciliation(unresponsive_detected=unresponsive_detected)

    disposition = _reconcile_stop(
        sv,
        repo_path,
        force=force,
        reason="reconcile-runtime: stop unresponsive orchestrator",
    )
    return RepoReconciliation(
        unresponsive_detected=unresponsive_detected,
        stopped_unresponsive=disposition.stopped,
        stop_dispositions=(disposition,),
    )


def _is_multi_instance_repo(multi_status: MultiInstanceStatus) -> bool:
    return multi_status.expected_count > 1 or any(
        inst.instance_id is not None for inst in multi_status.instances
    )


def _reconcile_instance_ids(multi_status: MultiInstanceStatus) -> list[str | None]:
    """Every instance slot this repository could hold a lock for."""
    instance_ids: list[str | None] = [None]
    instance_ids.extend(
        f"orchestrator-{i}" for i in range(1, multi_status.expected_count + 1)
    )
    instance_ids.extend(
        inst.instance_id
        for inst in multi_status.instances
        if inst.instance_id is not None
    )

    deduped_ids: list[str | None] = []
    for instance_id in instance_ids:
        if instance_id not in deduped_ids:
            deduped_ids.append(instance_id)
    return deduped_ids


def _stop_unresponsive_instance(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    payload: Mapping[str, Any],
    instance_id: str | None,
    force: bool,
) -> EngineStopDisposition:
    """Stop one unresponsive instance and state what happened to it."""
    reason = "reconcile-runtime: stop unresponsive multi-instance orchestrator"
    port = payload.get("port")
    if isinstance(port, int):
        return sv.stop_by_port(
            port,
            force=force,
            reason=reason,
            actor=RECONCILE_ACTOR,
            graceful_timeout_seconds=RECONCILE_GRACEFUL_TIMEOUT_SECONDS,
            force_if_graceful_fails=False,
        )
    return _reconcile_stop(
        sv,
        repo_path,
        force=force,
        instance_id=instance_id,
        reason=reason,
    )


def _reconcile_multi_instance_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    multi_status: MultiInstanceStatus,
    stop_unresponsive: bool,
    force: bool,
) -> RepoReconciliation:
    """Reconcile a multi-instance repository."""
    reconciled_stale_lock = False
    unresponsive_detected: list[dict[str, Any]] = []
    stopped_unresponsive = False
    dispositions: list[EngineStopDisposition] = []

    for instance_id in _reconcile_instance_ids(multi_status):
        status_info = sv.status(repo_path, instance_id=instance_id)
        if status_info.state == "failed":
            stale = _reconcile_stop(
                sv,
                repo_path,
                force=False,
                instance_id=instance_id,
                reason=(
                    "reconcile-runtime: stale lock for failed "
                    "multi-instance orchestrator"
                ),
            )
            dispositions.append(stale)
            if stale.stopped:
                reconciled_stale_lock = True
            continue

        if status_info.state != RUNNING_SUPERVISOR_STATE:
            continue

        payload = enrich_runtime_health(
            repo_path,
            status_info.to_dict(),
            instance_id=instance_id,
        )
        if payload is None or payload.get("runtime_health") != "unresponsive":
            continue

        unresponsive_detected.append(
            {
                "repo_root": str(repo_path),
                "instance_id": instance_id,
                "heartbeat_age_seconds": payload.get("heartbeat_age_seconds"),
                "pid": payload.get("pid"),
                "port": payload.get("port"),
            }
        )

        if not stop_unresponsive:
            continue

        disposition = _stop_unresponsive_instance(
            sv=sv,
            repo_path=repo_path,
            payload=payload,
            instance_id=instance_id,
            force=force,
        )
        dispositions.append(disposition)
        stopped_unresponsive = stopped_unresponsive or disposition.stopped

    return RepoReconciliation(
        reconciled_stale_lock=reconciled_stale_lock,
        unresponsive_detected=tuple(unresponsive_detected),
        stopped_unresponsive=stopped_unresponsive,
        stop_dispositions=tuple(dispositions),
    )


@control_orchestrator_router.get("/control/orchestrator/status")
async def control_status(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
    config_name: str | None = Query(None),
    mode: str | None = Query(None),
) -> JSONResponse:
    """Get the status of the orchestrator for a repository."""
    sv = deps.get_supervisor()

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    desired = get_selected_launch_selection(path)
    try:
        selection = RepositoryLaunchSelection.parse(
            mode=mode or desired.mode.value,
            config_name=config_name or desired.config.value,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    selected = selection.config.value
    multi_status = sv.status_all_instances(
        path, config_name=selected, mode=selection.mode.value
    )

    if multi_status.expected_count > 1 or len(multi_status.instances) > 1:
        return JSONResponse(
            {
                "multi_instance": True,
                "repo_root": str(path),
                "expected_count": multi_status.expected_count,
                "running_count": sum(
                    1 for status in multi_status.instances if status.state == "running"
                ),
                "instances": [status.to_dict() for status in multi_status.instances],
            }
        )

    if multi_status.instances and len(multi_status.instances) == 1:
        payload = enrich_runtime_health(path, multi_status.instances[0].to_dict())
        return JSONResponse(payload or multi_status.instances[0].to_dict())

    status_info = sv.status(path)
    if status_info.state != "running":
        detected_engines = detect_repository_orchestrators(path)
        if detected_engines:
            orphaned_payload = build_orphaned_engine_status(
                detected_engines[0],
                include_configuration_identity=True,
            )
            return JSONResponse(
                enrich_runtime_health(path, orphaned_payload, orphaned=True)
                or orphaned_payload
            )

    payload = enrich_runtime_health(path, status_info.to_dict())
    return JSONResponse(payload or status_info.to_dict())


@control_orchestrator_router.post("/control/orchestrator/pause")
async def control_pause(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Pause the orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.pause_cmd.execute(RepoActionRequest(repo_root=repo_root))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/resume")
async def control_resume(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Resume the orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.resume_cmd.execute(RepoActionRequest(repo_root=repo_root))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/refresh")
async def control_refresh(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Trigger refresh on the orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.refresh_cmd.execute(
        RefreshActionRequest(
            repo_root=repo_root,
            inflight_stable_ids=body.get("inflight_stable_ids"),
        )
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.get("/control/orchestrator/last_failure")
async def control_last_failure(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get the last startup failure for a repository."""
    from ..infra.repo_identity import state_dir

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    failure_path = state_dir(path) / "last_failure.json"
    if not failure_path.exists():
        return JSONResponse({"last_failure": None})

    try:
        with open(failure_path) as handle:
            data = json.load(handle)
        return JSONResponse({"last_failure": data})
    except (json.JSONDecodeError, OSError) as exc:
        return JSONResponse(
            {
                "error": "read_failed",
                "detail": str(exc),
            },
            status_code=500,
        )


@control_orchestrator_router.get("/control/orchestrator/doctor")
async def control_doctor(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
    config_name: str | None = Query(None),
    mode: str | None = Query(None),
) -> JSONResponse:
    """Run diagnostics for a repository."""
    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    desired = get_selected_launch_selection(path)
    try:
        selection = RepositoryLaunchSelection.parse(
            mode=mode or desired.mode,
            config_name=config_name or desired.config,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    result = await actions.doctor_cmd.execute(
        DoctorActionRequest(repo_root=path, selection=selection)
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/guardrails/repair")
async def control_repair_guardrails(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Repair repo-local guardrails by running the standard setup-guardrails flow."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    config_name = _normalize_config_name(body.get("config_name"))
    if config_name is None:
        return JSONResponse({"error": "Invalid config_name"}, status_code=400)

    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode"),
            config_name=config_name,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    config_path = get_config_path(repo_root, config_name, selection.mode)
    if not config_path.exists():
        return JSONResponse(
            {
                "error": "config_not_found",
                "detail": f"Config file not found: {config_name}",
                "config_name": config_name,
                "mode": selection.mode.value,
            },
            status_code=404,
        )

    try:
        config = Config.load(config_path)
        result = setup_repo_guardrails(config, target_root=repo_root)
    except RepoGuardrailsError as exc:
        return JSONResponse(
            {
                "error": "repair_failed",
                "detail": str(exc),
                "config_name": config_name,
                "mode": selection.mode.value,
            },
            status_code=400,
        )

    payload = serialize_guardrails_result(result)
    payload["config_name"] = config_name
    payload["mode"] = selection.mode.value
    payload["message"] = (
        "Repo guardrails repaired. Review and commit changed files if this updated "
        "tracked files."
    )
    return JSONResponse(payload)


@control_orchestrator_router.post("/control/orchestrator/ai_diagnose")
async def control_ai_diagnose(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Run AI-powered diagnostics for a repository."""
    from ..infra.ai_diagnose import run_ai_diagnose

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    timeout = body.get("timeout", 120)
    if not isinstance(timeout, int) or timeout < 10 or timeout > 600:
        timeout = 120

    identity = deps.get_control_actions().effective_configuration_identity(repo_root)
    result = run_ai_diagnose(
        repo_root,
        timeout_seconds=timeout,
        identity=identity,
    )
    return JSONResponse(result.to_dict())


@control_orchestrator_router.get("/control/orchestrator/log_tail")
async def control_log_tail(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
    n: int = Query(200, ge=1, le=10000),
) -> JSONResponse:
    """Get the last N lines of the orchestrator log."""
    from ..infra.repo_identity import state_dir

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    log_path = state_dir(path) / "logs" / "orchestrator.log"
    if not log_path.exists():
        return JSONResponse({"lines": [], "total_lines": 0})

    try:
        lines, total_lines = read_last_n_lines(log_path, n)
    except OSError as exc:
        return JSONResponse(
            {
                "error": "read_failed",
                "detail": str(exc),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "lines": lines,
            "total_lines": total_lines,
            "returned_lines": len(lines),
        }
    )


__all__ = ["control_orchestrator_router"]
