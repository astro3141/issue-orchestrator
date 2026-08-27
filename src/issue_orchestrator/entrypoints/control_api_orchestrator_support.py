"""Dependency wiring for Control Center orchestrator route modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable, Mapping, TypedDict

from fastapi import Depends, FastAPI, Request

from ..infra.repo_guardrails import RepoGuardrailsInstallResult
from ..ports.repository_engine_supervisor import RUNNING_SUPERVISOR_STATE

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


class EngineStillRunningPayload(TypedDict):
    error: str
    detail: str
    repo_root: str
    stopped_count: int
    still_running: list[RunningEnginePayload]


@dataclass(frozen=True)
class RunningEngine:
    """One Repository Engine observed alive after a stop attempt."""

    instance_id: str | None
    pid: int | None
    port: int | None

    def to_payload(self) -> RunningEnginePayload:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "port": self.port,
        }


@dataclass(frozen=True)
class EngineStopResponse:
    """The one truthful answer a stop request is allowed to return."""

    payload: EngineStopPayload | EngineNotRunningPayload | EngineStillRunningPayload
    status_code: int


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


def observe_running_engines(
    sv: SupervisorOps,
    repo_root: Path,
) -> tuple[RunningEngine, ...]:
    """Observe which tracked engines for ``repo_root`` are still alive."""
    multi_status = sv.status_all_instances(repo_root)
    return tuple(
        RunningEngine(
            instance_id=instance.instance_id,
            pid=instance.pid,
            port=instance.port,
        )
        for instance in multi_status.instances
        if instance.state == RUNNING_SUPERVISOR_STATE
    )


def port_stop_evidence(*, port: int, stopped: bool) -> tuple[RunningEngine, ...]:
    """Evidence left by a port-only stop, which no lock can corroborate.

    A stop by port targets an engine the supervisor does not track, so
    the only observation available is the one the stop itself made of
    that port. An unconfirmed stop therefore means "still running",
    never "was not running".
    """
    if stopped:
        return ()
    return (RunningEngine(instance_id=None, pid=None, port=port),)


def resolve_engine_stop_response(
    *,
    repo_root: Path,
    stopped_count: int,
    still_running: tuple[RunningEngine, ...],
) -> EngineStopResponse:
    """Answer a stop request from observed evidence, not from intent.

    A stop that left the engine running must not be presented as a
    clean stop, and must not be presented as "already stopped"
    either: #324 reported ``status=stopped, stopped_count=1`` for a
    retirement the process evidence contradicted. Process and lock
    evidence outranks what the stop attempt hoped for (#326).
    """
    if still_running:
        payload: EngineStillRunningPayload = {
            "error": ENGINE_STILL_RUNNING_ERROR,
            "detail": (
                "The repository engine did not stop within the graceful "
                "timeout and is still running. No force escalation was "
                "authorized, so it was left running and no signal was sent. "
                "Stop it again with force to terminate it."
            ),
            "repo_root": str(repo_root),
            "stopped_count": stopped_count,
            "still_running": [engine.to_payload() for engine in still_running],
        }
        return EngineStopResponse(payload=payload, status_code=409)
    if stopped_count > 0:
        return EngineStopResponse(
            payload=stopped_engine_payload(repo_root, stopped_count),
            status_code=200,
        )
    not_running: EngineNotRunningPayload = {
        "status": EngineStopStatus.NOT_RUNNING,
        "repo_root": str(repo_root),
    }
    return EngineStopResponse(payload=not_running, status_code=200)


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
    "EngineStopResponse",
    "RunningEngine",
    "get_control_api_orchestrator_dependencies",
    "install_control_api_orchestrator_dependencies",
    "observe_running_engines",
    "port_stop_evidence",
    "read_last_n_lines",
    "resolve_engine_stop_response",
    "serialize_guardrails_result",
    "stopped_engine_payload",
]
