"""Shared, interruptible timing policy for Repository Engine shutdown."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import os
import time
from typing import Callable, Protocol

from ..ports.repository_engine_supervisor import StopOutcome

logger = logging.getLogger(__name__)

DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS = 120
DEFAULT_STOP_POLL_INTERVAL_SECONDS = 0.1
_FORCE_SIGNAL_WAIT_SECONDS = 3.0


class StopAction(Enum):
    """Action selected by one checkpoint of a Repository Engine stop."""

    WAIT = "wait"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    FORCE = "force"
    ABORT = "abort"


@dataclass(frozen=True)
class StopPolicySnapshot:
    """Current operator policy for an in-flight Repository Engine stop."""

    graceful_timeout_seconds: float
    force: bool = False
    abort: bool = False


class StopPolicy(Protocol):
    """Behavior-level source of live stop policy."""

    def snapshot(self) -> StopPolicySnapshot:
        """Return the policy that should govern the next wait checkpoint."""
        ...


@dataclass(frozen=True)
class StaticStopPolicy:
    """Fixed policy used by ordinary CLI and single-engine stop calls."""

    graceful_timeout_seconds: float
    force: bool = False

    def snapshot(self) -> StopPolicySnapshot:
        return StopPolicySnapshot(
            graceful_timeout_seconds=self.graceful_timeout_seconds,
            force=self.force,
        )


@dataclass(frozen=True)
class StopBudgetCheckpoint:
    """Decision and remaining shared graceful budget at one instant."""

    action: StopAction
    remaining_seconds: float


def process_is_alive(pid: int) -> bool:
    """Return whether a process still accepts signal-zero probes."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InterruptibleStopBudget:
    """Own one elapsed-time budget while observing live policy updates."""

    def __init__(
        self,
        policy: StopPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = DEFAULT_STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._sleeper = sleeper
        self._poll_interval_seconds = poll_interval_seconds
        self._started_at = clock()

    def checkpoint(self) -> StopBudgetCheckpoint:
        policy = self._policy.snapshot()
        elapsed = self._clock() - self._started_at
        remaining = max(0.0, policy.graceful_timeout_seconds - elapsed)
        if policy.abort:
            action = StopAction.ABORT
        elif policy.force:
            action = StopAction.FORCE
        elif remaining <= 0:
            action = StopAction.TIMED_OUT
        else:
            action = StopAction.WAIT
        return StopBudgetCheckpoint(action=action, remaining_seconds=remaining)

    def wait_for_exit(self, target_alive: Callable[[], bool]) -> StopAction:
        """Wait until exit or the live policy interrupts the graceful budget."""
        while target_alive():
            checkpoint = self.checkpoint()
            if checkpoint.action is not StopAction.WAIT:
                return checkpoint.action
            self._sleeper(
                min(self._poll_interval_seconds, checkpoint.remaining_seconds)
            )
        return StopAction.EXITED


class InterruptibleStopController:
    """Own the complete graceful wait and its force/abort transitions.

    This is the single disposition owner for every Repository Engine
    stop, whichever way the target is identified (tracked pid, or the
    port it still holds). One fail-closed rule governs it:

        **Failure to confirm a graceful shutdown request is not
        authority to signal the engine.**

    So an unconfirmed request buys nothing: the target is observed
    over the same graceful budget either way, and only an explicit
    ``force_requested``, an explicitly-authorized ``force_on_timeout``
    after the budget expires, or a live policy that turns force on may
    reach ``force_stop``. When none of those hold and the budget runs
    out, the stop reports ``StopOutcome.TIMED_OUT`` and leaves the
    engine running rather than signalling it (#326).
    """

    def __init__(
        self,
        policy: StopPolicy,
        *,
        target_alive: Callable[[], bool],
        force_requested: bool,
        force_on_timeout: bool,
        request_graceful: Callable[[], bool],
        force_stop: Callable[[], bool],
        on_stopped: Callable[[], object],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = DEFAULT_STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._budget = InterruptibleStopBudget(
            policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=poll_interval_seconds,
        )
        self._target_alive = target_alive
        self._force_requested = force_requested
        self._force_on_timeout = force_on_timeout
        self._request_graceful = request_graceful
        self._force_stop = force_stop
        self._on_stopped = on_stopped

    def stop(self) -> StopOutcome:
        """Execute one interruptible stop using one elapsed-time budget."""
        initial_action = self._budget.checkpoint().action
        if initial_action is StopAction.ABORT:
            raise StopAborted("Stop aborted by operator policy")
        if self._force_requested or initial_action is StopAction.FORCE:
            return self._forced()

        if not self._request_graceful():
            logger.warning(
                "Graceful shutdown request was not confirmed; observing the "
                "target over the remaining graceful budget. An unconfirmed "
                "request is not authority to signal the engine.",
            )

        wait_result = self._budget.wait_for_exit(self._target_alive)
        if wait_result is StopAction.EXITED:
            self._on_stopped()
            return StopOutcome.STOPPED
        if wait_result is StopAction.ABORT:
            raise StopAborted("Stop aborted by operator policy")
        if wait_result is StopAction.FORCE or (
            wait_result is StopAction.TIMED_OUT and self._force_on_timeout
        ):
            return self._forced()
        logger.warning(
            "Graceful budget expired with the target still alive and no force "
            "escalation authorized; leaving the engine running.",
        )
        return StopOutcome.TIMED_OUT

    def _forced(self) -> StopOutcome:
        return StopOutcome.STOPPED if self._force_stop() else StopOutcome.FORCE_FAILED


class StopAborted(RuntimeError):
    """Raised when an operator aborts the stop currently being attempted."""


def signal_exit_poll_iterations(
    *, force: bool, grace_seconds: float
) -> int:
    """Return 100ms poll iterations before supervisor escalation."""
    wait_seconds = {
        True: _FORCE_SIGNAL_WAIT_SECONDS,
        False: grace_seconds,
    }[force]
    return max(1, int(wait_seconds * 10))
