"""Process exit must not outrun shutdown-manager cleanup (#330).

The #328 lane engine retired with exit code 0, a free lock and no signal
of any kind — and still failed a clean lifecycle. It printed ``Exiting
with code 0`` **twice** and never printed the shutdown manager's
``Lock released`` / ``Cleanup complete`` tail:

    Shutdown requested: API /api/shutdown
    Exiting with code 0
    Running cleanup...
    [SHUTDOWN] Terminating agent runtime owners
    [web] Shutdown complete, exiting via shutdown_manager
    Exiting with code 0          <- second exit, cleanup never finished

The defect was in the state machine, not in any one caller. ``exit()``
set ``EXITED`` and then called ``cleanup()``, which set
``SHUTTING_DOWN`` — so the state regressed *while the exit was still in
flight*. A second ``exit()`` arriving during that window no longer saw
``EXITED``, was admitted as a fresh exit owner, and reached ``os._exit``
with the first owner's cleanup still running.

A second caller is the normal case, not a rare one: ``/api/shutdown``
tears the dashboard server down *and* schedules a process exit, and the
server coming down runs the web entrypoint's own exit path. Both
pressures are deliberate, and #277 fixed the ordering that puts the
acknowledgment on the wire before either of them. What was missing is a
terminal protocol that admits exactly one of them as the exit owner.

So these tests state the repaired invariant rather than the timing that
exposed it: exactly one owner runs cleanup to completion, and no other
caller terminates the process ahead of it.

``os._exit`` is the one thing that must not really happen in a test
process, so it is recorded instead. That makes it return, which a real
``os._exit`` never does — both callers therefore reach it here, where a
live process would already be gone at the first. "Exactly one process
death" is a real-process property and is proved as one, in
``tests/integration/test_engine_exit_completes_cleanup.py``. What is
proved here is the part that survives the substitution: no termination
is *reached* before cleanup has finished.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pytest

from issue_orchestrator.control.shutdown_manager import (
    FollowerOutcome,
    ShutdownManager,
    ShutdownState,
    TerminalExit,
)
from tests.unit.threading_helpers import join_or_fail, run_in_thread, wait_for_event

CLEANUP_STARTED = "cleanup.started"
CLEANUP_FINISHED = "cleanup.finished"
TERMINATED = "process.terminated"

SHUTDOWN_MANAGER_LOGGER = "issue_orchestrator.control.shutdown_manager"
TERMINAL_EXIT_LOG = "Exiting with code"

# Bounded waits. The positive ones are generous — they only decide how
# long a broken run takes to fail. The negative one is the window in
# which a second caller admitted as a fresh exit owner would have
# reached ``os._exit``; it is the whole cost of proving it does not.
CLEANUP_HOLD_TIMEOUT_SECONDS = 10.0
JOIN_TIMEOUT_SECONDS = 10.0
FOLLOWER_SETTLE_SECONDS = 0.25
CONCURRENT_EXIT_CALLERS = 8


@dataclass
class ExitTimeline:
    """Everything that happened, in the order it finished happening."""

    entries: list[str] = field(default_factory=list)
    terminated: threading.Event = field(default_factory=threading.Event)
    guard: threading.Lock = field(default_factory=threading.Lock)

    def record(self, entry: str) -> None:
        with self.guard:
            self.entries.append(entry)
        if entry == TERMINATED:
            self.terminated.set()

    def index_of(self, entry: str) -> int:
        assert entry in self.entries, f"{entry!r} never happened: {self.entries}"
        return self.entries.index(entry)

    def count(self, entry: str) -> int:
        return self.entries.count(entry)


@pytest.fixture
def manager() -> Iterator[ShutdownManager]:
    """The process singleton, returned to a pristine state around the test."""
    instance = ShutdownManager()
    instance.reset()
    yield instance
    instance.reset()


@pytest.fixture
def manager_with_an_expired_follower_bound(
    manager: ShutdownManager,
) -> ShutdownManager:
    """A manager whose follower gives up the instant it starts waiting.

    A zero-length bound is how "the owner was still inside cleanup when
    the bound expired" becomes an answer rather than a race. The follower
    reads a flag the test provably has not let the owner set yet, so the
    expiry is deterministic, costs no wall-clock time, and puts the
    follower past its bound in the one state that matters: cleanup
    unfinished.
    """
    manager.reset(follower_wait_seconds=0.0)
    return manager


@pytest.fixture
def recorded_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ExitTimeline]:
    """Record process termination onto a timeline instead of taking it."""
    timeline = ExitTimeline()

    def record_exit(code: int) -> None:
        timeline.record(TERMINATED)

    monkeypatch.setattr(os, "_exit", record_exit)
    yield timeline


class TestTheTerminalExitProtocol:
    """:class:`TerminalExit` alone: who owns the exit, and who waits."""

    def test_only_one_caller_can_ever_own_the_terminal_exit(self) -> None:
        """The claim is taken once for the life of the process."""
        terminal_exit = TerminalExit()
        start = threading.Barrier(CONCURRENT_EXIT_CALLERS)
        claims: list[bool] = []
        claims_lock = threading.Lock()

        def claim() -> None:
            start.wait(timeout=JOIN_TIMEOUT_SECONDS)
            won = terminal_exit.claim(0)
            with claims_lock:
                claims.append(won)

        threads = [run_in_thread(claim) for _ in range(CONCURRENT_EXIT_CALLERS)]
        for index, (thread, result) in enumerate(threads):
            join_or_fail(thread, JOIN_TIMEOUT_SECONDS, label=f"claimer {index}")
            result.unwrap()

        assert claims.count(True) == 1
        assert claims.count(False) == CONCURRENT_EXIT_CALLERS - 1
        assert terminal_exit.claimed is True

    def test_a_follower_is_not_released_until_cleanup_is_reported(self) -> None:
        """The release is the owner's report, not the passage of time.

        A zero-length bound states that as an answer rather than a race:
        before the owner reports, the wait can only expire; afterwards it
        can only find the report.
        """
        terminal_exit = TerminalExit(follower_wait_seconds=0.0)
        assert terminal_exit.claim(0) is True

        assert (
            terminal_exit.await_owner_cleanup()
            is FollowerOutcome.OWNER_CLEANUP_UNFINISHED
        )

        terminal_exit.report_cleanup_finished()

        assert (
            terminal_exit.await_owner_cleanup()
            is FollowerOutcome.OWNER_CLEANUP_FINISHED
        )

    def test_a_follower_stops_waiting_but_is_told_what_it_stopped_on(self) -> None:
        """The bound ends the waiting, and says nothing about the cleanup.

        A follower that gives up learns that the owner's cleanup is
        *unfinished* — which is the one thing that decides what it may do
        next. The bound is stated as a value the caller can see and an
        outcome the caller is told about, so nothing downstream has to
        infer "finished" from "stopped waiting".
        """
        terminal_exit = TerminalExit(follower_wait_seconds=0.0)
        terminal_exit.claim(0)

        assert terminal_exit.follower_wait_seconds == 0.0
        assert (
            terminal_exit.await_owner_cleanup()
            is FollowerOutcome.OWNER_CLEANUP_UNFINISHED
        )
        assert terminal_exit.cleanup_reported is False

    def test_a_follower_exits_with_the_owners_code(self) -> None:
        """One exit sequence means one exit code, the owner's."""
        terminal_exit = TerminalExit()

        assert terminal_exit.claim(3) is True
        assert terminal_exit.claim(0) is False
        assert terminal_exit.exit_code == 3

    def test_the_owner_thread_is_told_apart_from_every_other_caller(self) -> None:
        """Only the owner's own thread may re-enter without waiting."""
        terminal_exit = TerminalExit()
        terminal_exit.claim(0)
        seen_from_another_thread: list[bool] = []

        def look() -> None:
            seen_from_another_thread.append(terminal_exit.owned_by_current_thread)

        thread, result = run_in_thread(look)
        join_or_fail(thread, JOIN_TIMEOUT_SECONDS, label="other thread")
        result.unwrap()

        assert terminal_exit.owned_by_current_thread is True
        assert seen_from_another_thread == [False]

    def test_the_owner_re_enters_only_while_its_cleanup_is_unfinished(self) -> None:
        """Re-entrancy is a property of the cleanup, not of the thread.

        Once the owner has reported, a later call on the same thread is
        an ordinary caller again — nothing is left for it to wait on, so
        ``exit()`` stays terminal for it.
        """
        terminal_exit = TerminalExit()
        terminal_exit.claim(0)

        assert terminal_exit.reentered_by_owner is True

        terminal_exit.report_cleanup_finished()

        assert terminal_exit.reentered_by_owner is False
        assert terminal_exit.cleanup_reported is True


class TestExactlyOneExitOwner:
    """The manager as the engine uses it: two pressures, one exit."""

    def test_a_second_exit_cannot_terminate_ahead_of_the_first_cleanup(
        self,
        manager: ShutdownManager,
        recorded_termination: ExitTimeline,
    ) -> None:
        """The measured #328 failure, stated as a rule.

        The first exit is held inside cleanup for as long as the test
        wants it there. A second exit is issued into exactly that
        window — the window the live engine hit — and the timeline says
        what it managed to do: nothing, until cleanup finished.
        """
        timeline = recorded_termination
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        state_during_cleanup: list[ShutdownState] = []

        def held_cleanup() -> None:
            timeline.record(CLEANUP_STARTED)
            state_during_cleanup.append(manager.state)
            cleanup_started.set()
            assert release_cleanup.wait(CLEANUP_HOLD_TIMEOUT_SECONDS), (
                "the exit owner's cleanup was never released"
            )
            timeline.record(CLEANUP_FINISHED)

        manager.add_cleanup_callback(held_cleanup)
        manager.request_shutdown(reason="API /api/shutdown")

        owner, owner_result = run_in_thread(manager.exit, 0)
        wait_for_event(
            cleanup_started, JOIN_TIMEOUT_SECONDS, label="the exit owner's cleanup"
        )

        follower, follower_result = run_in_thread(manager.exit, 0)
        # A second caller admitted as a fresh exit owner runs a handful
        # of instructions before ``os._exit``; this is the window it
        # gets. The repaired follower is parked on the owner's cleanup
        # report and records nothing here, which is the only wall-clock
        # cost of proving it.
        terminated_during_cleanup = timeline.terminated.wait(FOLLOWER_SETTLE_SECONDS)

        release_cleanup.set()
        join_or_fail(owner, JOIN_TIMEOUT_SECONDS, label="exit owner")
        join_or_fail(follower, JOIN_TIMEOUT_SECONDS, label="second exit caller")
        owner_result.unwrap()
        follower_result.unwrap()

        assert not terminated_during_cleanup, (
            "a second exit() terminated the process while the exit owner's "
            f"cleanup was still running: {timeline.entries}"
        )
        assert timeline.count(CLEANUP_STARTED) == 1, (
            f"cleanup ran more than once: {timeline.entries}"
        )
        assert timeline.index_of(CLEANUP_FINISHED) < timeline.index_of(TERMINATED), (
            f"the process was terminated before cleanup finished: {timeline.entries}"
        )
        # Requirement 4: the in-flight state says "cleanup/exit in
        # progress", and is not EXITED overwritten by SHUTTING_DOWN.
        assert state_during_cleanup == [ShutdownState.SHUTTING_DOWN]
        assert manager.state is ShutdownState.EXITED

    def test_a_follower_that_gives_up_waiting_still_does_not_terminate(
        self,
        manager_with_an_expired_follower_bound: ShutdownManager,
        recorded_termination: ExitTimeline,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The bound ends the waiting. It does not confer exit authority.

        The follower's wait is a liveness mechanism — it stops a caller
        being parked forever on a cleanup that never finishes. It is not
        a timer after which a competing exit request may end the process
        anyway: that would be the #330 defect back again, delayed rather
        than removed, and #326 already settled that being unable to
        complete a graceful retirement is not authority to manufacture a
        forced one.

        So the owner is held inside cleanup for the whole of the
        follower's bound and past it, the follower is let time out, and
        the termination count has to stay at zero until the owner itself
        finishes. If a cleanup genuinely never finishes, the truthful
        outcome is a shutdown that failed, reported at ERROR — not a
        process death a non-owner invented.
        """
        caplog.set_level(logging.ERROR, logger=SHUTDOWN_MANAGER_LOGGER)
        manager = manager_with_an_expired_follower_bound
        timeline = recorded_termination
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()

        def held_cleanup() -> None:
            timeline.record(CLEANUP_STARTED)
            cleanup_started.set()
            assert release_cleanup.wait(CLEANUP_HOLD_TIMEOUT_SECONDS), (
                "the exit owner's cleanup was never released"
            )
            timeline.record(CLEANUP_FINISHED)

        manager.add_cleanup_callback(held_cleanup)
        manager.request_shutdown(reason="API /api/shutdown")

        owner, owner_result = run_in_thread(manager.exit, 0)
        wait_for_event(
            cleanup_started, JOIN_TIMEOUT_SECONDS, label="the exit owner's cleanup"
        )

        follower, follower_result = run_in_thread(manager.exit, 0)
        # Not a settling window: the follower's bound is already spent
        # when it starts waiting, so joining it means it has done
        # everything exit() gives it to do — with the owner demonstrably
        # still inside cleanup, because only this test releases it.
        join_or_fail(follower, JOIN_TIMEOUT_SECONDS, label="second exit caller")
        follower_result.unwrap()

        assert timeline.count(TERMINATED) == 0, (
            "a follower whose wait expired terminated the process while the "
            f"exit owner's cleanup was still running: {timeline.entries}"
        )
        # And it left the shutdown truthfully mid-flight rather than
        # advancing the machine to a terminal state it did not reach.
        assert manager.state is ShutdownState.SHUTTING_DOWN
        assert any(
            "returning without terminating" in record.getMessage()
            for record in caplog.records
        ), "the follower did not report the shutdown as incomplete"

        release_cleanup.set()
        join_or_fail(owner, JOIN_TIMEOUT_SECONDS, label="exit owner")
        owner_result.unwrap()

        # The process dies once, at the hand of the owner, after its
        # cleanup finished.
        assert timeline.entries == [CLEANUP_STARTED, CLEANUP_FINISHED, TERMINATED]
        assert manager.state is ShutdownState.EXITED

    def test_the_shutdown_state_only_ever_moves_forward(
        self,
        manager: ShutdownManager,
        recorded_termination: ExitTimeline,
    ) -> None:
        """No later caller can re-open an earlier state.

        This is the single-threaded statement of the same defect: the
        old ``exit()`` left the manager in ``SHUTTING_DOWN`` once its
        own ``cleanup()`` had overwritten ``EXITED``, which is precisely
        what re-admitted the second caller.
        """
        assert manager.state is ShutdownState.RUNNING

        assert manager.request_shutdown(reason="API /api/shutdown") is True
        assert manager.state is ShutdownState.SHUTDOWN_REQUESTED

        manager.exit(0)
        assert manager.state is ShutdownState.EXITED

        assert manager.request_shutdown(reason="web server stopped") is False
        assert manager.cleanup() is False
        manager.exit(0)
        assert manager.state is ShutdownState.EXITED
        # A later caller still gets a terminal exit(); what it does not
        # get is a second cleanup or a second announcement.
        assert recorded_termination.count(TERMINATED) == 2

    def test_concurrent_exits_run_one_cleanup_and_one_lock_release(
        self,
        manager: ShutdownManager,
        recorded_termination: ExitTimeline,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """One cleanup sequence, one lock release, one exit announcement.

        The repository lock is the artifact the live failure lost: the
        engine's flock was free only because the process died, not
        because the shutdown manager released it.
        """
        caplog.set_level(logging.INFO, logger=SHUTDOWN_MANAGER_LOGGER)
        released: list[str] = []
        monkeypatch.setattr(
            "issue_orchestrator.infra.repo_lock.release_lock",
            lambda repo_root: released.append(str(repo_root)),
        )
        cleanup_calls: list[int] = []
        manager.initialize(tmp_path)
        manager.add_cleanup_callback(lambda: cleanup_calls.append(1))
        manager.request_shutdown(reason="API /api/shutdown")

        start = threading.Barrier(CONCURRENT_EXIT_CALLERS)

        def exit_together() -> None:
            start.wait(timeout=JOIN_TIMEOUT_SECONDS)
            manager.exit(0)

        threads = [
            run_in_thread(exit_together) for _ in range(CONCURRENT_EXIT_CALLERS)
        ]
        for index, (thread, result) in enumerate(threads):
            join_or_fail(thread, JOIN_TIMEOUT_SECONDS, label=f"exit caller {index}")
            result.unwrap()

        assert cleanup_calls == [1]
        assert released == [str(tmp_path)]
        announcements = [
            record
            for record in caplog.records
            if TERMINAL_EXIT_LOG in record.getMessage()
        ]
        assert len(announcements) == 1, (
            "more than one caller ran the terminal exit sequence: "
            f"{[record.getMessage() for record in announcements]}"
        )

    def test_exit_reentered_from_cleanup_leaves_termination_to_the_owner(
        self,
        manager: ShutdownManager,
        recorded_termination: ExitTimeline,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The owner must not be made to wait for itself.

        A cleanup callback that calls ``exit()`` is a caller that can
        never be released, because the cleanup it would be waiting for
        is the one below it on its own stack. It is refused loudly, and
        the in-flight sequence finishes and terminates as usual.
        """
        caplog.set_level(logging.ERROR, logger=SHUTDOWN_MANAGER_LOGGER)
        timeline = recorded_termination

        def reentrant_cleanup() -> None:
            timeline.record(CLEANUP_STARTED)
            manager.exit(0)
            timeline.record(CLEANUP_FINISHED)

        manager.add_cleanup_callback(reentrant_cleanup)
        manager.request_shutdown(reason="API /api/shutdown")

        manager.exit(0)

        assert timeline.entries == [CLEANUP_STARTED, CLEANUP_FINISHED, TERMINATED]
        assert any(
            "re-entered" in record.getMessage() for record in caplog.records
        ), "the refused re-entrant exit was not reported"
