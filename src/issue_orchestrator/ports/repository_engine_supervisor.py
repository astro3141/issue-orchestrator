"""Behavior port for Repository Engine supervisor operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol, Sequence, runtime_checkable

RUNNING_SUPERVISOR_STATE = "running"


class StopOutcome(StrEnum):
    """Truthful disposition of one completed Repository Engine stop.

    ``TIMED_OUT`` is the outcome a non-force stop reaches when it
    spent everything it was authorized to spend and the target is
    still alive: the graceful budget expired with no force escalation
    authorized, or the supervisor refused the request outright because
    the tracked identity did not match the engine holding the lock.
    Either way it is *not* a failure to act — it is the recorded fact
    that the engine was left running, and callers must not present it
    as a clean stop.

    ``FORCE_FAILED`` is the opposite case: an escalation *was*
    authorized and *did* run, and the engine survived it. Telling that
    operator to "stop it again with force" would be false (#326).
    """

    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    FORCE_FAILED = "force_failed"

    @classmethod
    def worst(cls, outcomes: Iterable[StopOutcome]) -> StopOutcome:
        """The outcome a set of stops has to be answered as.

        Severity order lives here because every surface that
        summarises more than one stop — a multi-instance stop, a
        reconcile sweep — has to rank the same way or the two will
        eventually disagree about what a mixed sweep means (#326).
        """
        collected = set(outcomes)
        if cls.FORCE_FAILED in collected:
            return cls.FORCE_FAILED
        if cls.TIMED_OUT in collected:
            return cls.TIMED_OUT
        return cls.STOPPED


@dataclass(frozen=True)
class RunningEngine:
    """One Repository Engine observed alive after a stop attempt."""

    instance_id: str | None
    pid: int | None
    port: int | None


@dataclass(frozen=True)
class EngineStopDisposition:
    """What one stop request did, stated by the owner that watched it.

    The supervisor is the only component that observes the target
    across a stop, so it is the only component that can say what
    happened to it. Collapsing that to a ``bool``/``int`` at the port
    forced entrypoints to re-observe the engine afterwards and then
    guess at the reason — a second, race-prone observation of a fact
    the owner already held (#326).
    """

    outcome: StopOutcome
    stopped_count: int
    still_running: tuple[RunningEngine, ...] = ()

    @property
    def stopped(self) -> bool:
        """Whether every engine this stop targeted is confirmed gone."""
        return self.outcome is StopOutcome.STOPPED and not self.still_running

    @classmethod
    def for_engine(
        cls, outcome: StopOutcome, engine: RunningEngine
    ) -> EngineStopDisposition:
        """State the disposition of the one engine the owner watched."""
        if outcome is StopOutcome.STOPPED:
            return cls(outcome=outcome, stopped_count=1)
        return cls(outcome=outcome, stopped_count=0, still_running=(engine,))

    @classmethod
    def already_stopped(cls) -> EngineStopDisposition:
        """No live engine was found, so the caller's goal already holds.

        The count of 1 here is the pre-existing "count that nothing
        corroborates" behaviour of the tracked path (a repo with no
        lock reports ``stopped_count=1``). It is carried forward
        deliberately rather than changed under #326; correcting it is
        the same family of work as #324 and belongs in its own change.

        It is also why the stop endpoint's ``not_running`` answer is
        unreachable today: every production disposition contributes
        either this count of 1 or a still-running engine, so nothing
        can produce "zero stopped and nothing left running". The
        branch is kept rather than deleted because it becomes the
        correct answer the moment this count is corrected — the two
        are one change, not two.
        """
        return cls(outcome=StopOutcome.STOPPED, stopped_count=1)

    @classmethod
    def combined(
        cls, parts: Sequence[EngineStopDisposition]
    ) -> EngineStopDisposition:
        """Aggregate per-instance dispositions without losing the worst."""
        return cls(
            outcome=StopOutcome.worst(part.outcome for part in parts),
            stopped_count=sum(part.stopped_count for part in parts),
            still_running=tuple(
                engine for part in parts for engine in part.still_running
            ),
        )


class RepositoryEngineLock(Protocol):
    """Published runtime identity returned by a successful start."""

    pid: int
    http_port: int | None
    instance_id: str | None
    configuration_mode: str
    config_name: str
    config_fingerprint: str


@dataclass
class SupervisorStatus:
    """Observable state returned by a supervisor status query."""

    state: Literal["running", "stopped", "failed", "unknown"]
    pid: int | None = None
    port: int | None = None
    started_at: str | None = None
    recovered: bool = False
    error: str | None = None
    instance_id: str | None = None
    configuration_mode: str = "default"
    config_name: str = "default.yaml"
    config_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "recovered": self.recovered,
            "error": self.error,
            "configuration_mode": self.configuration_mode,
            "config_name": self.config_name,
            "config_fingerprint": self.config_fingerprint,
        }
        if self.instance_id is not None:
            result["instance_id"] = self.instance_id
        return result


@dataclass
class MultiInstanceStatus:
    """Observable state for every named instance of one repository."""

    repo_root: str
    instances: list[SupervisorStatus] = field(default_factory=list)
    expected_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "instances": [status.to_dict() for status in self.instances],
            "expected_count": self.expected_count,
            "running_count": sum(
                1
                for status in self.instances
                if status.state == RUNNING_SUPERVISOR_STATE
            ),
        }


class RepositoryEngineStopPolicy(Protocol):
    """Live policy source accepted by graceful supervisor shutdown."""

    def snapshot(self) -> Any: ...


@runtime_checkable
class SupervisorOps(Protocol):
    """Behavior required by launch and Control Center owners.

    Every stop on this port takes ``force_if_graceful_fails``, and it
    means one thing everywhere: whether the caller *authorized* an
    escalation past the graceful budget. It defaults to ``False`` on
    all three stop methods because a default kwarg is nobody's
    authorization — a caller that inherits it never said "kill this".
    The tracked path used to default it to ``True`` while the port
    path defaulted it to ``False``, so the same ``force=false``
    request escalated to SIGKILL when the engine happened to hold a
    lock and left it running when it did not (#326). Callers that
    genuinely want the escalation say so at the call site.
    """

    def start(
        self,
        repo_root: Path | str,
        config_name: str = "default.yaml",
        instance_id: str | None = None,
        port: int | None = None,
        expected_identity: dict[str, Any] | None = None,
        start_paused: bool = False,
        log_level: str | None = None,
        *,
        mode: str = "default",
        expected_config_fingerprint: str | None = None,
    ) -> RepositoryEngineLock: ...

    def stop(
        self,
        repo_root: Path | str,
        force: bool = False,
        instance_id: str | None = None,
        *,
        reason: str,
        actor: str = "supervisor.stop",
        graceful_timeout_seconds: float = 120,
        force_if_graceful_fails: bool = False,
        stop_policy: RepositoryEngineStopPolicy | None = None,
    ) -> EngineStopDisposition: ...

    def stop_tracked_instance(
        self,
        repo_root: Path | str,
        tracked: SupervisorStatus,
        *,
        reason: str,
        actor: str,
    ) -> bool:
        """Stop only the exact PID/instance represented by ``tracked``."""
        ...

    def stop_by_port(
        self,
        port: int,
        *,
        reason: str,
        actor: str = "supervisor.stop_by_port",
        force: bool = False,
        graceful_timeout_seconds: float = 120,
        force_if_graceful_fails: bool = False,
    ) -> EngineStopDisposition: ...

    def status(
        self,
        repo_root: Path | str,
        instance_id: str | None = None,
    ) -> SupervisorStatus: ...

    def start_instances(
        self,
        repo_root: Path | str,
        config_name: str = "default.yaml",
        count: int | None = None,
        expected_identity: dict[str, Any] | None = None,
        start_paused: bool = False,
        log_level: str | None = None,
        *,
        mode: str = "default",
        expected_config_fingerprint: str | None = None,
    ) -> Sequence[RepositoryEngineLock]: ...

    def stop_all_instances(
        self,
        repo_root: Path | str,
        force: bool = False,
        *,
        reason: str,
        actor: str = "supervisor.stop_all_instances",
        graceful_timeout_seconds: float = 120,
        force_if_graceful_fails: bool = False,
        stop_policy: RepositoryEngineStopPolicy | None = None,
    ) -> EngineStopDisposition: ...

    def status_all_instances(
        self,
        repo_root: Path | str,
        config_name: str = "default.yaml",
        *,
        mode: str = "default",
    ) -> MultiInstanceStatus: ...


__all__ = [
    "EngineStopDisposition",
    "MultiInstanceStatus",
    "RUNNING_SUPERVISOR_STATE",
    "RepositoryEngineLock",
    "RunningEngine",
    "StopOutcome",
    "SupervisorOps",
    "SupervisorStatus",
]
