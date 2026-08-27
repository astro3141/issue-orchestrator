"""Dependency wiring for Control Center orchestrator route modules."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Mapping,
    Sequence,
    TypedDict,
)

from fastapi import Depends, FastAPI, Request

from ..infra.repo_guardrails import RepoGuardrailsInstallResult
from ..ports.repository_engine_supervisor import (
    RUNNING_SUPERVISOR_STATE,
    EngineStopDisposition,
    RunningEngine,
    StopOutcome,
)

_ORCHESTRATOR_DEPENDENCIES_STATE_KEY = "control_api_orchestrator_dependencies"
_GUARDRAILS_REPAIRED_STATUS = "repaired"

if TYPE_CHECKING:
    from ..execution.control_center_actions import ControlCenterActions
    from ..ports.repository_engine_supervisor import SupervisorOps


@dataclass(frozen=True)
class ControlApiOrchestratorDependencies:
    """Dependency hooks needed by Control Center orchestrator routes."""

    get_supervisor: Callable[[], SupervisorOps]
    get_control_actions: Callable[[], ControlCenterActions]
    validate_repo_root: Callable[[str | None], Path | None]
    track_launched_pids: Callable[[Mapping[str, object]], None]
    coerce_graceful_timeout_seconds: Callable[[object, int], int]
    global_shutdown_in_progress: Callable[[], bool]
    begin_engine_shutdown_operation: Callable[[Path, bool, bool, int], None]
    finish_engine_shutdown_operation: Callable[[Path], None]


def install_control_api_orchestrator_dependencies(
    app: FastAPI,
    deps: ControlApiOrchestratorDependencies,
) -> None:
    """Install shared dependencies for Control Center orchestrator routes."""
    setattr(app.state, _ORCHESTRATOR_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_orchestrator_dependencies(
    request: Request,
) -> ControlApiOrchestratorDependencies:
    """Resolve router dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _ORCHESTRATOR_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center orchestrator dependencies not configured")
    return deps


ControlApiOrchestratorDependency = Annotated[
    ControlApiOrchestratorDependencies,
    Depends(get_control_api_orchestrator_dependencies),
]


class GuardrailsRepairPayload(TypedDict, total=False):
    status: str
    repo_root: str
    hooks_path: str
    hooks_dir: str
    pre_push_hook: str
    verify_script: str
    helper_script: str
    installed_files: list[str]
    preserved_files: list[str]
    agent_hook_files: dict[str, list[str]]
    config_name: str
    mode: str
    message: str


class EngineStopStatus(StrEnum):
    STOPPED = "stopped"
    NOT_RUNNING = "not_running"


ENGINE_STILL_RUNNING_ERROR = "engine_still_running"


class EngineStopPayload(TypedDict):
    status: EngineStopStatus
    repo_root: str
    stopped_count: int


class EngineNotRunningPayload(TypedDict):
    status: EngineStopStatus
    repo_root: str


class RunningEnginePayload(TypedDict):
    instance_id: str | None
    pid: int | None
    port: int | None


class ReconcileRunningEnginePayload(RunningEnginePayload):
    """One engine a reconcile sweep left running, and the stop that did it."""

    repo_root: str
    outcome: StopOutcome


class EnginesLeftRunningPayload(TypedDict):
    """The engines a sweep left running and the reason it may state."""

    still_running: list[ReconcileRunningEnginePayload]
    still_running_detail: str | None


class EngineStillRunningPayload(TypedDict):
    error: str
    detail: str
    repo_root: str
    stopped_count: int
    still_running: list[RunningEnginePayload]


class EnginePortMismatchPayload(TypedDict):
    error: str
    detail: str


class OrphanedEnginePayload(TypedDict):
    repo_root: str
    port: int | None
    active_selection: Any


@dataclass(frozen=True)
class EngineStopResponse:
    """The one truthful answer a stop request is allowed to return."""

    payload: (
        EngineStopPayload
        | EngineNotRunningPayload
        | EngineStillRunningPayload
        | EnginePortMismatchPayload
    )
    status_code: int


@dataclass(frozen=True)
class EngineStopRequest:
    """One parsed Control Center stop request, ready for the supervisor."""

    repo_root: Path
    reason: str
    actor: str
    force: bool
    force_if_timeout: bool
    graceful_timeout_seconds: int
    port_override: int | None

    @property
    def escalation_authorized(self) -> bool:
        """Whether the operator authorized escalating past the budget.

        Both stop paths read this. The port branch used to drop it,
        so the same request body escalated when the engine happened to
        hold a lock and silently did not when it did not (#326).
        """
        return self.force_if_timeout or self.force


def _repo_relative_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def serialize_guardrails_result(
    result: RepoGuardrailsInstallResult,
) -> GuardrailsRepairPayload:
    """Serialize a guardrail repair result with repository-relative paths."""
    repo_root = result.repo_root
    return {
        "status": _GUARDRAILS_REPAIRED_STATUS,
        "repo_root": str(repo_root),
        "hooks_path": result.hooks_path_config,
        "hooks_dir": _repo_relative_path(repo_root, result.hooks_dir),
        "pre_push_hook": _repo_relative_path(repo_root, result.pre_push_hook),
        "verify_script": _repo_relative_path(repo_root, result.verify_script),
        "helper_script": _repo_relative_path(repo_root, result.helper_script),
        "installed_files": [
            _repo_relative_path(repo_root, path) for path in result.installed_files
        ],
        "preserved_files": [
            _repo_relative_path(repo_root, path) for path in result.preserved_files
        ],
        "agent_hook_files": {
            agent_type: [_repo_relative_path(repo_root, path) for path in paths]
            for agent_type, paths in result.agent_hook_files.items()
        },
    }


def stopped_engine_payload(repo_root: Path, stopped_count: int) -> EngineStopPayload:
    """Build the typed successful engine-stop response."""
    return {
        "status": EngineStopStatus.STOPPED,
        "repo_root": str(repo_root),
        "stopped_count": stopped_count,
    }


def _running_engine_payload(engine: RunningEngine) -> RunningEnginePayload:
    """Serialize one still-running engine for the stop response."""
    return {
        "instance_id": engine.instance_id,
        "pid": engine.pid,
        "port": engine.port,
    }


_STILL_RUNNING_DETAILS: Mapping[StopOutcome, str] = {
    StopOutcome.TIMED_OUT: (
        "{count} repository engine(s) left running. The graceful timeout "
        "expired while they were still alive. No force escalation was "
        "authorized, so no signal was sent. Stop again with force to "
        "terminate."
    ),
    StopOutcome.FORCE_FAILED: (
        "{count} repository engine(s) left running. Force escalation was "
        "authorized and a kill signal was sent, but they survived it. "
        "Stopping again with force is unlikely to help; inspect the "
        "processes directly."
    ),
}


def still_running_detail(outcome: StopOutcome, count: int) -> str:
    """Explain still-running engines from what the stop actually did.

    One hard-coded sentence claimed "no force escalation was
    authorized" for every still-running answer, which is false the
    moment force — or the Control Center's default force-on-timeout —
    was authorized and the escalation itself failed (#326).

    Every surface that has to say *why* an engine is still up reads
    this one mapping. Restating the reason anywhere else — a second
    Python call site, a literal in the Control Center's JS — is the
    cross-path drift that produced the false sentence in the first
    place.
    """
    detail = _STILL_RUNNING_DETAILS.get(outcome)
    if detail is None:
        raise ValueError(
            f"outcome {outcome} cannot describe an engine that is still "
            "running; the disposition contradicts its own evidence",
        )
    return detail.format(count=count)


@dataclass(frozen=True)
class EngineLeftRunning:
    """One engine a sweep asked to stop and could not confirm gone.

    It carries the outcome of the stop that left it there, because
    the engine's identity alone cannot say why it is still up.
    """

    repo_root: str
    engine: RunningEngine
    outcome: StopOutcome

    def to_payload(self) -> ReconcileRunningEnginePayload:
        return {
            "repo_root": self.repo_root,
            "outcome": self.outcome,
            **_running_engine_payload(self.engine),
        }


def engines_left_running_payload(
    engines: Sequence[EngineLeftRunning],
) -> EnginesLeftRunningPayload:
    """State what a sweep left running, and the one reason it may give.

    The reconcile surface used to name the reason itself — "because no
    force escalation was authorized" — which is false for every sweep
    that *did* escalate and lost. The sweep's worst outcome and the
    sentence for it both come from the owners that already hold them
    (#326).
    """
    if not engines:
        return {"still_running": [], "still_running_detail": None}
    return {
        "still_running": [engine.to_payload() for engine in engines],
        "still_running_detail": still_running_detail(
            StopOutcome.worst(engine.outcome for engine in engines),
            len(engines),
        ),
    }


def resolve_engine_stop_response(
    *,
    repo_root: Path,
    disposition: EngineStopDisposition,
) -> EngineStopResponse:
    """Answer a stop request from the owner's own statement of the stop.

    A stop that left the engine running must not be presented as a
    clean stop, and must not be presented as "already stopped"
    either: #324 reported ``status=stopped, stopped_count=1`` for a
    retirement the process evidence contradicted. The supervisor is
    the component that watched the target, so its disposition — not a
    second observation made here — decides the answer (#326).

    The question every caller is really asking is
    ``disposition.stopped``: may this be presented as a clean stop?
    Branching on the still-running list instead would answer 200 for a
    disposition that reached a non-clean outcome yet named no engine —
    presentation outrunning the evidence, which is the shape of the
    bug.
    """
    if not disposition.stopped:
        payload: EngineStillRunningPayload = {
            "error": ENGINE_STILL_RUNNING_ERROR,
            "detail": still_running_detail(
                disposition.outcome, len(disposition.still_running)
            ),
            "repo_root": str(repo_root),
            "stopped_count": disposition.stopped_count,
            "still_running": [
                _running_engine_payload(engine)
                for engine in disposition.still_running
            ],
        }
        return EngineStopResponse(payload=payload, status_code=409)
    if disposition.stopped_count > 0:
        return EngineStopResponse(
            payload=stopped_engine_payload(repo_root, disposition.stopped_count),
            status_code=200,
        )
    # Unreachable while ``EngineStopDisposition.already_stopped`` reports a
    # count of 1 for a repository with no lock; see its docstring for why
    # that count and this branch are one deferred change.
    not_running: EngineNotRunningPayload = {
        "status": EngineStopStatus.NOT_RUNNING,
        "repo_root": str(repo_root),
    }
    return EngineStopResponse(payload=not_running, status_code=200)


def _perform_engine_stop(
    sv: SupervisorOps,
    request: EngineStopRequest,
    *,
    confirm_port: Callable[[Path, int], bool],
) -> EngineStopResponse:
    """Run one blocking engine stop and answer from its disposition.

    Private on purpose: an engine stop spends a graceful budget
    watching the target, so it is only ever reached through
    ``run_engine_stop``, which owns the worker-thread hand-off. Making
    it callable from a route is what let two of the four stop paths
    drift back onto the Control Center's event loop (#326).
    """
    status_info = sv.status(request.repo_root)
    if (
        status_info.state != RUNNING_SUPERVISOR_STATE
        and request.port_override is not None
    ):
        if not confirm_port(request.repo_root, request.port_override):
            mismatch: EnginePortMismatchPayload = {
                "error": "port_mismatch",
                "detail": "No matching orchestrator found on the provided port.",
            }
            return EngineStopResponse(payload=mismatch, status_code=409)
        disposition = sv.stop_by_port(
            request.port_override,
            force=request.force,
            reason=request.reason,
            actor=request.actor,
            graceful_timeout_seconds=request.graceful_timeout_seconds,
            force_if_graceful_fails=request.escalation_authorized,
        )
    else:
        disposition = sv.stop_all_instances(
            request.repo_root,
            force=request.force,
            reason=request.reason,
            actor=request.actor,
            graceful_timeout_seconds=request.graceful_timeout_seconds,
            force_if_graceful_fails=request.escalation_authorized,
        )
    return resolve_engine_stop_response(
        repo_root=request.repo_root,
        disposition=disposition,
    )


async def run_engine_stop(
    sv: SupervisorOps,
    request: EngineStopRequest,
    *,
    confirm_port: Callable[[Path, int], bool],
) -> EngineStopResponse:
    """Stop a repository's engines off the loop and answer the request.

    A graceful budget is minutes of blocking supervisor work — since
    an unconfirmed request no longer shortcuts to a signal, the worst
    case is the whole budget (#326). Running that inline would freeze
    status polling, SSE and the spinner showing the operator this very
    stop, so the hand-off belongs to the one owner every stop route
    goes through rather than to each route's own discipline.
    """
    return await asyncio.to_thread(
        _perform_engine_stop, sv, request, confirm_port=confirm_port
    )


async def run_supervisor_stop(
    sv: SupervisorOps,
    repo_root: Path,
    *,
    reason: str,
    actor: str,
    force: bool = False,
    force_if_graceful_fails: bool = False,
) -> EngineStopDisposition:
    """Stop one repository's tracked engine off the Control Center loop.

    The narrow form of :func:`run_engine_stop` for the routes that
    need only the disposition and have no port fallback to confirm.
    They get the same off-loop guarantee from the same owner, so no
    stop route has to remember to arrange it (#326).
    """

    def stop_engine() -> EngineStopDisposition:
        return sv.stop(
            repo_root,
            force=force,
            reason=reason,
            actor=actor,
            force_if_graceful_fails=force_if_graceful_fails,
        )

    return await asyncio.to_thread(stop_engine)


def read_last_n_lines(log_path: Path, n: int) -> tuple[list[str], int]:
    """Read the last N lines of a log file plus its total line count."""
    with open(log_path, "rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        lines: list[str] = []
        chunk_size = 8192
        remaining = file_size
        while len(lines) < n + 1 and remaining > 0:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            handle.seek(remaining)
            chunk = handle.read(read_size).decode("utf-8", errors="replace")
            chunk_lines = chunk.split("\n")
            if lines:
                lines[0] = chunk_lines[-1] + lines[0]
                chunk_lines = chunk_lines[:-1]
            lines = chunk_lines + lines
        lines = lines[-n:] if len(lines) > n else lines
        handle.seek(0)
        total_lines = sum(1 for _ in handle)
    return lines, total_lines


__all__ = [
    "ENGINE_STILL_RUNNING_ERROR",
    "ControlApiOrchestratorDependencies",
    "ControlApiOrchestratorDependency",
    "EngineLeftRunning",
    "EngineStopRequest",
    "EngineStopResponse",
    "EnginesLeftRunningPayload",
    "OrphanedEnginePayload",
    "engines_left_running_payload",
    "get_control_api_orchestrator_dependencies",
    "install_control_api_orchestrator_dependencies",
    "read_last_n_lines",
    "resolve_engine_stop_response",
    "run_engine_stop",
    "run_supervisor_stop",
    "serialize_guardrails_result",
    "still_running_detail",
    "stopped_engine_payload",
]
