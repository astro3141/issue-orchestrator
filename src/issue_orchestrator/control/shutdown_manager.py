"""Centralized shutdown manager for orchestrator processes.

This module provides a single source of truth for shutdown state and cleanup.
All exit paths should go through the ShutdownManager to ensure proper cleanup.

Why this exists:
- os._exit() skips atexit handlers, so we need explicit cleanup
- Multiple exit paths (API shutdown, signal handlers, errors) need coordination
- Race conditions between timers and exit paths caused stale locks
- A centralized manager ensures cleanup happens exactly once, and — because
  more than one termination pressure converges on every operator shutdown —
  that the process is not terminated before that cleanup finishes

Usage:
    from ..control.shutdown_manager import shutdown_manager

    # At startup
    shutdown_manager.initialize(repo_root="/path/to/repo")

    # When shutdown is requested
    shutdown_manager.request_shutdown(reason="API request")

    # Actually exit (releases lock, calls os._exit)
    shutdown_manager.exit()
"""

import atexit
import logging
import os
import signal
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ShutdownState(Enum):
    """Shutdown state machine states.

    The machine is **monotonic**: it only ever moves forward through the
    order below, so a state can never be read as "earlier" than something
    that has already happened.

    - ``RUNNING`` - nothing has asked for shutdown.
    - ``SHUTDOWN_REQUESTED`` - shutdown was asked for, nothing destructive yet.
    - ``SHUTTING_DOWN`` - the terminal exit sequence owns the process and its
      cleanup is running or has run.
    - ``EXITED`` - process termination has been actuated.
    """
    RUNNING = "running"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    SHUTTING_DOWN = "shutting_down"
    EXITED = "exited"


_STATE_ORDER: dict[ShutdownState, int] = {
    ShutdownState.RUNNING: 0,
    ShutdownState.SHUTDOWN_REQUESTED: 1,
    ShutdownState.SHUTTING_DOWN: 2,
    ShutdownState.EXITED: 3,
}

# How long a second exit() caller waits for the exit owner's cleanup before
# terminating the process anyway. Chosen to sit between the two real numbers
# around it: a cleanup that is still making progress may legitimately take up
# to the 60 s background-job drain in ``Orchestrator._drain_background_jobs``,
# and a cleanup that has hung must still leave a dead process inside the
# supervisor's 120 s graceful budget
# (``infra.shutdown_timing.DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS``) — a
# non-force stop is not authority to signal the engine (#326), so an engine
# that never exits on its own is an engine the operator cannot stop.
DEFAULT_FOLLOWER_CLEANUP_WAIT_SECONDS = 90.0


class TerminalExit:
    """Exactly-once ownership of the process's terminal exit sequence.

    Two independent termination pressures converge on every operator
    shutdown: ``/api/shutdown`` tears the dashboard server down *and*
    schedules a process exit, and the server coming down runs the web
    entrypoint's own exit path. Whichever arrives first must run cleanup
    to completion; whichever arrives second must not be admitted as a
    fresh exit owner (#330).

    So the claim is taken once, for the life of the process:

    - exactly one caller wins :meth:`claim` and owns cleanup + termination;
    - every other caller loses it and calls :meth:`wait_for_cleanup`, which
      blocks until the owner reports cleanup finished. It can therefore never
      terminate the process ahead of that cleanup;
    - the wait is bounded, so a cleanup that never finishes cannot make the
      process unkillable. Expiry is reported to the caller rather than
      swallowed.
    """

    def __init__(
        self,
        *,
        follower_wait_seconds: float = DEFAULT_FOLLOWER_CLEANUP_WAIT_SECONDS,
    ) -> None:
        self._follower_wait_seconds = follower_wait_seconds
        self._claim_lock = threading.Lock()
        self._owner_thread: Optional[int] = None
        self._exit_code = 0
        self._cleanup_finished = threading.Event()

    @property
    def follower_wait_seconds(self) -> float:
        """Bound on how long a non-owner waits for the owner's cleanup."""
        return self._follower_wait_seconds

    @property
    def exit_code(self) -> int:
        """The code the exit owner claimed the process with."""
        return self._exit_code

    @property
    def claimed(self) -> bool:
        """Whether the terminal exit sequence already has an owner."""
        with self._claim_lock:
            return self._owner_thread is not None

    @property
    def owned_by_current_thread(self) -> bool:
        """Whether the calling thread is the exit owner itself."""
        with self._claim_lock:
            return self._owner_thread == threading.get_ident()

    @property
    def cleanup_reported(self) -> bool:
        """Whether the owner has already reported its cleanup finished."""
        return self._cleanup_finished.is_set()

    @property
    def reentered_by_owner(self) -> bool:
        """Whether the caller is the owner with its own cleanup unfinished.

        True only for an ``exit()`` raised from inside the owner's own
        cleanup. Such a caller can neither wait — the cleanup it would be
        waiting for is below it on its own stack — nor terminate, because
        that cleanup is unfinished.
        """
        return not self.cleanup_reported and self.owned_by_current_thread

    def claim(self, code: int) -> bool:
        """Try to become the single owner of the terminal exit sequence.

        Returns True for exactly one caller across the process lifetime.
        """
        with self._claim_lock:
            if self._owner_thread is not None:
                return False
            self._owner_thread = threading.get_ident()
            self._exit_code = code
            return True

    def report_cleanup_finished(self) -> None:
        """Announce that the owner's cleanup has run to completion."""
        self._cleanup_finished.set()

    def wait_for_cleanup(self) -> bool:
        """Block until the owner reports cleanup finished.

        Returns:
            True if cleanup finished, False if the bound expired first.
        """
        return self._cleanup_finished.wait(self._follower_wait_seconds)


@dataclass
class ShutdownManager:
    """Centralized manager for orchestrator shutdown.

    Thread-safe singleton that coordinates all shutdown activities.
    Ensures lock cleanup happens exactly once, regardless of exit path.
    """

    _instance: Optional["ShutdownManager"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ShutdownManager":
        """Singleton pattern - only one shutdown manager per process."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        """Initialize shutdown manager (idempotent)."""
        if getattr(self, "_initialized", False):
            return

        self._state = ShutdownState.RUNNING
        # Re-entrant: state writes go through _advance_state, which is called
        # both on its own and from inside larger state-lock sections.
        self._state_lock = threading.RLock()
        self._repo_root: Optional[str] = None
        self._cleanup_done = False
        self._shutdown_reason: Optional[str] = None
        self._callbacks: list[Callable[[], None]] = []
        self._terminal_exit = TerminalExit()
        self._initialized = True

        # Register atexit handler as fallback (won't run with os._exit, but helps with sys.exit)
        atexit.register(self._atexit_cleanup)

        logger.debug("[shutdown] ShutdownManager initialized")

    def initialize(self, repo_root: str | Path) -> None:
        """Initialize with repo root for lock cleanup.

        Must be called during orchestrator startup.

        Args:
            repo_root: Path to the repository root (for lock file cleanup)
        """
        self._repo_root = str(repo_root) if repo_root else None
        logger.info("[shutdown] Initialized for repo: %s", self._repo_root)

    @property
    def state(self) -> ShutdownState:
        """Current shutdown state."""
        return self._state

    @property
    def shutdown_requested(self) -> bool:
        """Whether shutdown has been requested."""
        return self._state != ShutdownState.RUNNING

    @property
    def repo_root(self) -> Optional[str]:
        """Repo root path for lock cleanup."""
        return self._repo_root

    def _advance_state(self, target: ShutdownState) -> None:
        """Move the shutdown state forward, never backwards.

        Every state write goes through here. That is what makes
        "cleanup/exit in progress" a state a second caller cannot undo by
        arriving late (#330).
        """
        with self._state_lock:
            if _STATE_ORDER[target] > _STATE_ORDER[self._state]:
                self._state = target

    def add_cleanup_callback(self, callback: Callable[[], None]) -> None:
        """Add a callback to run during cleanup.

        Callbacks are run in LIFO order (last added, first called).
        Exceptions in callbacks are logged but don't prevent cleanup.
        """
        self._callbacks.append(callback)

    def request_shutdown(self, reason: str = "unknown") -> bool:
        """Request graceful shutdown.

        Args:
            reason: Why shutdown was requested (for logging)

        Returns:
            True if this was the first request, False if already shutting down
        """
        with self._state_lock:
            if self._state != ShutdownState.RUNNING:
                logger.debug("[shutdown] Already shutting down, ignoring request: %s", reason)
                return False

            self._advance_state(ShutdownState.SHUTDOWN_REQUESTED)
            self._shutdown_reason = reason
            logger.info("[shutdown] Shutdown requested: %s", reason)
            return True

    def _run_cleanup_callbacks(self) -> None:
        """Run registered cleanup callbacks."""
        for callback in reversed(self._callbacks):
            try:
                callback()
            except Exception as e:
                logger.warning("[shutdown] Cleanup callback failed: %s", e)

    def _release_lock(self) -> None:
        """Release the repository lock if we have one."""
        if not self._repo_root:
            logger.debug("[shutdown] No repo_root set, skipping lock release")
            return

        try:
            from ..infra.repo_lock import release_lock
            release_lock(self._repo_root)
            logger.info("[shutdown] Lock released for %s", self._repo_root)
        except Exception as e:
            logger.warning("[shutdown] Failed to release lock: %s", e)

    def cleanup(self) -> bool:
        """Run cleanup (lock release, callbacks). Idempotent.

        Returns:
            True if cleanup was performed, False if already done
        """
        with self._state_lock:
            if self._cleanup_done:
                logger.debug("[shutdown] Cleanup already done")
                return False

            self._advance_state(ShutdownState.SHUTTING_DOWN)
            self._cleanup_done = True

        logger.info("[shutdown] Running cleanup...")

        # Run callbacks first (in case they need the lock)
        self._run_cleanup_callbacks()

        # Release the lock
        self._release_lock()

        logger.info("[shutdown] Cleanup complete")
        return True

    def exit(self, code: int = 0) -> None:
        """Exit the process after cleanup.

        This is the single exit point for the orchestrator.
        Always use this instead of os._exit() or sys.exit().

        Exactly one caller owns the terminal sequence — logging the exit,
        running cleanup to completion, then terminating. A concurrent caller
        never becomes a second owner: it waits for that cleanup and only then
        terminates, so cleanup can never be cut short by a competing exit
        (#330).

        Args:
            code: Exit code (default 0)
        """
        if self._terminal_exit.claim(code):
            self._own_the_terminal_exit(code)
        elif self._terminal_exit.reentered_by_owner:
            # Re-entered from inside the owner's own cleanup. Waiting would
            # deadlock on this thread and terminating would abandon the
            # cleanup still on the stack below; the in-flight sequence is
            # already going to terminate the process.
            logger.error(
                "[shutdown] exit(%d) re-entered from inside the exit owner's "
                "own cleanup; leaving termination to the in-flight sequence",
                code,
            )
        else:
            self._follow_the_terminal_exit()

    def _own_the_terminal_exit(self, code: int) -> None:
        """Run the one terminal sequence: cleanup to completion, then exit."""
        self._advance_state(ShutdownState.SHUTTING_DOWN)

        logger.info("[shutdown] Exiting with code %d (reason: %s)",
                   code, self._shutdown_reason or "unknown")

        try:
            self.cleanup()
        finally:
            # Released here rather than on the success path only: a cleanup
            # that raised is finished too, and followers must not be stranded
            # on it for the whole bound.
            self._terminal_exit.report_cleanup_finished()

        self._terminate(code)

    def _follow_the_terminal_exit(self) -> None:
        """Second exit request: wait out the owner's cleanup, then terminate."""
        if not self._terminal_exit.wait_for_cleanup():
            logger.error(
                "[shutdown] The exit owner did not finish cleanup within "
                "%.0fs; terminating anyway so the process cannot become "
                "unkillable",
                self._terminal_exit.follower_wait_seconds,
            )
        self._terminate(self._terminal_exit.exit_code)

    def _terminate(self, code: int) -> None:
        """Actuate process termination.

        os._exit skips atexit handlers, which is why cleanup is explicit and
        has already run by the time any caller reaches here.
        """
        self._advance_state(ShutdownState.EXITED)
        os._exit(code)

    def _atexit_cleanup(self) -> None:
        """Atexit handler as fallback cleanup.

        This runs if the process exits via sys.exit() or normal termination.
        Won't run with os._exit(), but we call cleanup() explicitly there.
        """
        if not self._cleanup_done:
            logger.debug("[shutdown] Running atexit cleanup")
            self.cleanup()

    def install_signal_handlers(self) -> None:
        """Install signal handlers for graceful shutdown.

        Handles SIGTERM and SIGINT to trigger graceful shutdown.
        """
        def signal_handler(signum: int, frame: FrameType | None) -> None:
            sig_name = signal.Signals(signum).name
            logger.info("[shutdown] Received signal %s", sig_name)
            self.request_shutdown(reason=f"signal {sig_name}")
            # Don't exit here - let the main loop handle the shutdown

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        logger.debug("[shutdown] Signal handlers installed")

    def reset(self) -> None:
        """Reset state for testing. DO NOT use in production.

        This is the one place the state machine goes backwards, and the one
        place the terminal exit claim is given up — both only because a test
        process outlives the shutdowns it exercises.
        """
        with self._state_lock:
            self._state = ShutdownState.RUNNING
            self._cleanup_done = False
            self._shutdown_reason = None
            self._callbacks.clear()
            self._repo_root = None
            self._terminal_exit = TerminalExit()


# Global singleton instance
shutdown_manager = ShutdownManager()
