"""A paused engine disposes what finished, and starts nothing (#167).

The live shape this suite is built from: an ordinary Actor was already running
when the operator paused. It finished — with a publication failure, no PR and no
reviewer — reached terminal, and the completion handoff filed the cleanup it
earned. Nothing then ran that cleanup, because cleanup is planner-owned and
planning does not run while paused, so the Actor's worktree stayed on disk with
no way to dispose of it short of resuming (which reopens continuation execution)
or hand-editing state.

Every test here drives ``run_tick`` over the production ``OrchestratorSupport``,
``FactGatherer``, ``Planner`` and ``ActionApplier``, substituting only the ports
at the edge of the process — the checkout, the terminal and the repository host
— because a unit test may not create git worktrees or open tmux windows. That
matters: the claim is about which phase of a real tick disposes and which
withholds, and a hand-wired disposal owner cannot tell a tick that wires the
phase from a tick that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    Action,
    ActionType,
    CleanupSessionAction,
)
from issue_orchestrator.control.control_operation_ownership import (
    ControlOperationOwnership,
)
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.health_gate import HealthGate
from issue_orchestrator.control.orchestrator_support import (
    OrchestratorSupport,
    run_tick,
)
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.attempt import StoredIssueKey
from issue_orchestrator.domain.control_operation import (
    ControlOperationKey,
    ControlOperationKind,
)
from issue_orchestrator.domain.models import (
    DiscoveredReview,
    ImmediateCleanup,
    OrchestratorState,
    PendingCleanup,
)
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.continuation_helpers import (
    InMemoryControlOperationOwnershipStore,
    inert_control_continuation,
)

ISSUE = 165
WORKTREE = "/checkouts/issue-165"
TERMINAL = "issue-165"
REVIEWED_PR = 900


# ======================================================================
# Ports at the edge of the process
# ======================================================================


@dataclass
class FakeWorktrees:
    """The checkout port, recording exactly how each removal was asked for."""

    removed: list[tuple[str, bool]] = field(default_factory=list)
    removed_with_branch: list[tuple[str, bool]] = field(default_factory=list)
    fails: bool = False

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        if self.fails:
            raise RuntimeError("checkout busy")
        self.removed.append((str(worktree_path), force))

    def remove_checkout_and_branch(
        self, worktree_path: Path, *, force: bool = False
    ) -> None:
        if self.fails:
            raise RuntimeError("checkout busy")
        self.removed_with_branch.append((str(worktree_path), force))

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.removed] + [
            path for path, _ in self.removed_with_branch
        ]


@dataclass
class FakeSessions:
    """The terminal port: every session named here is open until stopped."""

    open_sessions: set[str] = field(default_factory=set)
    stopped: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)

    def exists(self, ref: Any) -> bool:
        return ref.name in self.open_sessions

    def stop(self, ref: Any) -> bool:
        self.open_sessions.discard(ref.name)
        self.stopped.append(ref.name)
        return True

    def start(self, *args: Any, **kwargs: Any) -> bool:
        self.started.append("start")
        return True


@dataclass
class FakePairRegistry:
    """The persistent coder/reviewer pair registry, as the applier sees it."""

    active: set[int] = field(default_factory=set)
    released: list[tuple[int, str]] = field(default_factory=list)

    def has_active_pair(self, issue_key: Any) -> bool:
        return issue_key in self.active

    def release(self, issue_key: Any, *, reason: str) -> None:
        self.active.discard(issue_key)
        self.released.append((issue_key, reason))

    def shutdown_all(self, *, reason: str) -> None:
        self.active.clear()


@dataclass
class JournalledApplier:
    """The production applier, plus a journal of everything it was asked to do.

    The journal is what proves the negative: a paused tick that ran anything at
    all beyond the disposal would say so here, whatever that something was.
    """

    inner: ActionApplier
    journal: list[str] = field(default_factory=list)

    def apply(self, action: Action) -> Any:
        self.journal.append(f"plan:{action.action_type.value}")
        return self.inner.apply(action)

    def dispose_terminal_session(self, action: CleanupSessionAction) -> Any:
        self.journal.append(f"dispose:{action.issue_number}")
        return self.inner.dispose_terminal_session(action)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# ======================================================================
# The engine under test
# ======================================================================


class Engine:
    """One engine's tick, assembled from the production control types."""

    def __init__(self, tmp_path: Path, *, close_tabs: bool = True) -> None:
        self.config = Config()
        self.config.repo = "owner/repo"
        self.config.repo_root = tmp_path
        self.config.cleanup.without_tech_lead.remove_worktrees = True
        self.config.cleanup.without_tech_lead.close_ai_session_tabs = close_tabs
        self.config.code_review_agent = "agent:reviewer"
        self.config.code_reviewed_label = "code-reviewed"

        self.state = OrchestratorState()
        self.events = InMemoryEventSink()
        self.github = MagicMock()
        self.github.get_prs_with_label.return_value = []
        self.worktrees = FakeWorktrees()
        self.sessions = FakeSessions()
        self.pairs = FakePairRegistry()
        self.supervisor = MagicMock()
        self.supervisor.has_matching.return_value = False
        self.supervisor.cancel_matching.return_value = []

        self.applier = JournalledApplier(
            ActionApplier(
                labels=MagicMock(),
                sessions=self.sessions,  # type: ignore[arg-type]
                events=self.events,
                repository_host=self.github,
                worktree_manager=self.worktrees,  # type: ignore[arg-type]
                pair_registry=self.pairs,  # type: ignore[arg-type]
                background_job_supervisor=self.supervisor,
            )
        )
        self.gatherer = FactGatherer(config=self.config, repository_host=self.github)
        self.planner = Planner(config=self.config, scheduler=Scheduler(self.config))
        self.ownership_store = InMemoryControlOperationOwnershipStore()
        self.ownership = ControlOperationOwnership(self.state, self.ownership_store)
        self.support = OrchestratorSupport(
            config=self.config,
            events=self.events,
            repository_host=self.github,
            state=self.state,
            event_context=EventContext(run_id="run", tick_id=0),
            session_manager=self.sessions,  # type: ignore[arg-type]
            action_applier=self.applier,  # type: ignore[arg-type]
            fact_gatherer=self.gatherer,
            planner=self.planner,
            worktree_manager=self.worktrees,  # type: ignore[arg-type]
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=MagicMock(),
            kill_session=MagicMock(),
            control_continuation=inert_control_continuation(self.state),
        )
        self._iteration = 0

    # -- the live #165 preconditions ----------------------------------

    def actor_finished_while_paused(self, *, scratch: bool = False) -> None:
        """The Actor reached terminal: its tab is open, its checkout on disk."""
        self.sessions.open_sessions.add(TERMINAL)
        self.state.immediate_cleanups.append(
            ImmediateCleanup(
                issue_number=ISSUE,
                terminal_id=TERMINAL,
                worktree_path=WORKTREE,
                reason="failed",
                scratch_worktree=scratch,
            )
        )

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False

    # -- the tick -----------------------------------------------------

    def tick(self, *, shutdown_requested: bool = False) -> None:
        self._iteration, _ = run_tick(
            loop_iteration=self._iteration,
            event_context=EventContext(run_id="run", tick_id=self._iteration),
            inflight_stable_ids={},
            state=self.state,
            events=self.events,
            shutdown_requested=shutdown_requested,
            process_active_sessions_fn=lambda: None,
            check_health_fn=lambda: HealthGate().check(paused=self.state.paused),
            run_planning_cycle_fn=self._plan_and_apply,
            dispose_terminal_sessions_fn=(
                self.support.dispose_terminal_sessions_while_paused
            ),
            emit_heartbeat_fn=lambda: None,
        )

    def _plan_and_apply(self) -> None:
        """The planning phase, minus the network fetch that precedes it."""
        snapshot = self.gatherer.create_snapshot(
            self.state, self.state.cached_queue_issues
        )
        self.support.apply_plan(self.planner.plan(snapshot), MagicMock())
        self.support.clear_discovered_facts(snapshot)

    # -- observations -------------------------------------------------

    @property
    def cleanup_issue_numbers(self) -> list[int]:
        return [c.issue_number for c in self.state.immediate_cleanups]


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(tmp_path)


def _live_operation() -> ControlOperationKey:
    return ControlOperationKey(
        _issue_key(),
        "a" * 40,
        ControlOperationKind.PUBLICATION_REVALIDATION_CONTINUATION,
    )


def _issue_key() -> StoredIssueKey:
    return StoredIssueKey(f"owner/repo#{ISSUE}", "owner/repo")


# ======================================================================
# 1-2. The live #165 shape, reached without a resume
# ======================================================================


class TestTheFinishedActorIsDisposed:
    def test_the_worktree_is_removed_while_the_pause_still_stands(
        self, engine: Engine
    ) -> None:
        """The acceptance: ``Actor worktree absent`` with pause unchanged."""
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert engine.state.paused is True

    def test_the_terminal_tab_is_closed_under_the_configured_policy(
        self, engine: Engine
    ) -> None:
        """Disposal is the whole cleanup contract, not just the checkout."""
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.sessions.stopped == [TERMINAL]

    def test_the_configured_policy_still_decides(self, tmp_path: Path) -> None:
        """An operator who keeps tabs open keeps them open while paused too."""
        engine = Engine(tmp_path, close_tabs=False)
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.sessions.stopped == []
        assert engine.worktrees.paths == [WORKTREE]

    def test_nothing_ever_cleared_the_pause_to_obtain_the_disposal(
        self, engine: Engine
    ) -> None:
        """No resume, at any point: the flag reads paused on every observation."""
        engine.actor_finished_while_paused()
        engine.pause()

        observed = [engine.state.paused]
        engine.tick()
        observed.append(engine.state.paused)
        engine.tick()
        observed.append(engine.state.paused)

        assert observed == [True, True, True]
        assert engine.worktrees.paths == [WORKTREE]

    def test_the_disposal_happens_once(self, engine: Engine) -> None:
        """The fact is consumed, so a second paused tick has nothing to do.

        A paused tick clears no facts of its own, so an unconsumed cleanup
        would be re-disposed on every tick until the engine resumed.
        """
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()
        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert engine.cleanup_issue_numbers == []

    def test_a_failed_removal_keeps_the_fact_for_the_next_tick(
        self, engine: Engine
    ) -> None:
        """Disposal that did not happen is not recorded as if it had."""
        engine.actor_finished_while_paused()
        engine.pause()
        engine.worktrees.fails = True

        engine.tick()

        assert engine.worktrees.paths == []
        assert engine.cleanup_issue_numbers == [ISSUE]

        engine.worktrees.fails = False
        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]


# ======================================================================
# 3-4. Nothing starts, and nothing else is applied
# ======================================================================


class TestThePausedTickStartsNothing:
    def test_the_only_thing_applied_is_the_disposal(self, engine: Engine) -> None:
        """The journal of everything the applier was asked to do, in full."""
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.applier.journal == [f"dispose:{ISSUE}"]

    def test_no_session_of_any_kind_is_started(self, engine: Engine) -> None:
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.sessions.started == []

    def test_queued_planner_work_stays_withheld(self, engine: Engine) -> None:
        """A review the observer discovered is not queued by the paused tick.

        Queue population costs no worker capacity, so it is the planner work
        most likely to leak through a disposal path that reached the planner —
        and it is withheld, along with the label and PR work behind it.
        """
        engine.actor_finished_while_paused()
        engine.state.discovered_reviews.append(
            DiscoveredReview(
                issue_number=ISSUE,
                pr_number=901,
                pr_url="https://example.test/pr/901",
                branch_name="issue-165",
                agent_label="agent:developer",
            )
        )
        engine.pause()

        engine.tick()

        assert engine.state.pending_reviews == []
        assert engine.applier.journal == [f"dispose:{ISSUE}"]
        # Withheld, not discarded: the fact survives for the resumed tick.
        assert len(engine.state.discovered_reviews) == 1

    def test_the_withheld_work_runs_after_a_resume(self, engine: Engine) -> None:
        engine.actor_finished_while_paused()
        engine.state.discovered_reviews.append(
            DiscoveredReview(
                issue_number=ISSUE,
                pr_number=901,
                pr_url="https://example.test/pr/901",
                branch_name="issue-165",
                agent_label="agent:developer",
            )
        )
        engine.pause()
        engine.tick()

        engine.resume()
        engine.tick()

        assert [r.pr_number for r in engine.state.pending_reviews] == [901]

    def test_no_control_operation_is_reserved(self, engine: Engine) -> None:
        """Nothing was admitted: the ownership ledger gained no row.

        A #139 revalidation or a #149 continuation run reserves before it
        starts, so an empty ledger after the disposal is the reservation
        counter reading zero.
        """
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.ownership_store.list_control_operation_ownership().rows == ()

    def test_a_paused_refresh_still_disposes_and_still_starts_nothing(
        self, engine: Engine
    ) -> None:
        """#161's barrier is unchanged: a refreshed paused tick plans nothing.

        The refresh path is the one way planning runs while paused, so it is
        also the one way a disposal could be double-applied — by the disposal
        phase and then by the planner in the same tick.
        """
        engine.actor_finished_while_paused()
        engine.pause()
        engine.state.queue_refresh_requested = True

        engine.tick()

        assert engine.applier.journal == [f"dispose:{ISSUE}"]
        assert engine.worktrees.paths == [WORKTREE]


# ======================================================================
# 5. Ownership and exclusion survive the disposal
# ======================================================================


class TestOwnershipIsNotReleasedByDisposal:
    def test_a_live_continuation_stays_owned_and_excluded(
        self, engine: Engine
    ) -> None:
        """The old Actor checkout is gone; the operation holding the issue is not.

        #146 ownership is what keeps ordinary rework off an issue a control
        operation is still running. Disposing the Actor's worktree must not be
        read as that operation settling.
        """
        key = _live_operation()
        engine.ownership.claim(key)
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert engine.ownership.owns(key) is True
        assert engine.ownership.exclusions.excludes_issue(_issue_key()) is True
        rows = engine.ownership_store.list_control_operation_ownership().rows
        assert [row.key for row in rows] == [key]


# ======================================================================
# 6. Normal worktree safety
# ======================================================================


class TestWorktreeRemovalPolicyIsUnchanged:
    def test_an_ordinary_coding_worktree_is_never_force_removed(
        self, engine: Engine
    ) -> None:
        """Uncommitted candidate work is not silently discarded by a pause."""
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.worktrees.removed == [(WORKTREE, False)]
        assert engine.worktrees.removed_with_branch == []

    def test_a_disposable_scratch_worktree_is_still_force_removed(
        self, engine: Engine
    ) -> None:
        """The one identity that may be forced keeps being forced (#6824 F8)."""
        engine.actor_finished_while_paused(scratch=True)
        engine.pause()

        engine.tick()

        assert engine.worktrees.removed == []
        assert engine.worktrees.removed_with_branch == [(WORKTREE, True)]


# ======================================================================
# 7. In-flight work that predates the pause is not cancelled
# ======================================================================


class TestPauseIsNotCancellation:
    def test_a_live_persistent_pair_withholds_the_disposal(
        self, engine: Engine
    ) -> None:
        """Cleanup cancels the issue's review exchange on its way past.

        On a running tick that is correct — the plan that produced the cleanup
        owns everything else happening to the issue. While paused, nothing new
        may start, so live exchange work predates the pause and is finishing on
        its own terms. It is left alone.
        """
        engine.actor_finished_while_paused()
        engine.pairs.active.add(ISSUE)
        engine.pause()

        engine.tick()

        assert engine.pairs.released == []
        assert engine.worktrees.paths == []
        assert engine.cleanup_issue_numbers == [ISSUE]

    def test_a_running_exchange_job_withholds_the_disposal(
        self, engine: Engine
    ) -> None:
        engine.actor_finished_while_paused()
        engine.supervisor.has_matching.return_value = True
        engine.pause()

        engine.tick()

        engine.supervisor.cancel_matching.assert_not_called()
        assert engine.worktrees.paths == []

    def test_the_disposal_follows_once_that_work_reaches_terminal(
        self, engine: Engine
    ) -> None:
        """Withholding defers the disposal, it does not cancel it."""
        engine.actor_finished_while_paused()
        engine.pairs.active.add(ISSUE)
        engine.pause()
        engine.tick()

        engine.pairs.active.discard(ISSUE)
        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert engine.state.paused is True

    def test_a_settled_exchange_is_still_torn_down_by_the_disposal(
        self, engine: Engine
    ) -> None:
        """The existing terminal-cleanup contract is intact for dead work.

        Nothing is live, so the cleanup's own lifecycle release still runs —
        the withholding above is about live work, not about skipping teardown.
        """
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.pairs.released == [(ISSUE, "session-cleanup")]

    def test_an_unverifiable_owner_withholds_rather_than_tearing_down(
        self, engine: Engine
    ) -> None:
        """Fail-safe: a probe that raises reads as live."""
        engine.actor_finished_while_paused()
        engine.supervisor.has_matching.side_effect = RuntimeError("supervisor down")
        engine.pause()

        engine.tick()

        assert engine.worktrees.paths == []
        assert engine.cleanup_issue_numbers == [ISSUE]


# ======================================================================
# 8-9. The held and deferred cleanup policies are unchanged
# ======================================================================


class TestHeldCleanupPolicyIsUnchanged:
    def test_a_tech_lead_artifact_hold_still_withholds_the_disposal(
        self, tmp_path: Path
    ) -> None:
        """A failure investigation reads the failed session's run assets.

        Disposing while it is queued deletes the artifact hints it was queued to
        read — true whether or not the engine is paused.
        """
        engine = Engine(tmp_path)
        engine.config.tech_lead_review_on_failure = True
        engine.config.tech_lead_review_agent = "agent:tech-lead"
        engine.actor_finished_while_paused()
        engine.state.discovered_failures.append(
            MagicMock(issue_number=ISSUE)
        )
        engine.pause()

        engine.tick()

        assert engine.worktrees.paths == []
        assert engine.cleanup_issue_numbers == [ISSUE]


class TestDeferredCleanupPolicyIsUnchanged:
    def _defer_a_reviewed_pr(self, engine: Engine) -> None:
        engine.state.pending_cleanups.append(
            PendingCleanup(
                issue=MagicMock(number=166),
                pr_number=REVIEWED_PR,
                pr_url=f"https://example.test/pr/{REVIEWED_PR}",
                branch_name="issue-166",
                terminal_id="issue-166",
                worktree_path=Path("/checkouts/issue-166"),
            )
        )
        engine.github.get_prs_with_label.return_value = [
            MagicMock(number=REVIEWED_PR)
        ]

    def test_a_reviewed_pr_cleanup_is_not_disposed_while_paused(
        self, engine: Engine
    ) -> None:
        """Deferred cleanup asks a live review-workflow question.

        "Has this PR been reviewed yet?" is not disposal a finished session
        already earned, so a pause does not broaden into it.
        """
        self._defer_a_reviewed_pr(engine)
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert len(engine.state.pending_cleanups) == 1

    def test_the_paused_tick_asks_the_repository_nothing(
        self, engine: Engine
    ) -> None:
        """The deferred question needs a network read; the paused pass has none."""
        self._defer_a_reviewed_pr(engine)
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick()

        engine.github.get_prs_with_label.assert_not_called()

    def test_the_deferred_cleanup_runs_normally_after_a_resume(
        self, engine: Engine
    ) -> None:
        self._defer_a_reviewed_pr(engine)
        engine.actor_finished_while_paused()
        engine.pause()
        engine.tick()

        engine.resume()
        engine.tick()

        assert "/checkouts/issue-166" in engine.worktrees.paths


# ======================================================================
# 10. Shutdown is not the disposal path
# ======================================================================


class TestShutdownSemanticsAreUnchanged:
    def test_disposal_happens_on_an_ordinary_paused_tick(
        self, engine: Engine
    ) -> None:
        """No shutdown was requested, and none is needed to reach the boundary."""
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick(shutdown_requested=False)

        assert engine.worktrees.paths == [WORKTREE]

    def test_a_shutdown_tick_disposes_nothing(self, engine: Engine) -> None:
        """A shutting-down tick returns before any phase runs, as it always did."""
        engine.actor_finished_while_paused()
        engine.pause()

        engine.tick(shutdown_requested=True)

        assert engine.worktrees.paths == []
        assert engine.applier.journal == []


# ======================================================================
# 12. Resume is unchanged
# ======================================================================


class TestResumeIsUnchanged:
    def test_a_disposed_cleanup_does_not_reappear_after_a_resume(
        self, engine: Engine
    ) -> None:
        """No duplicate cleanup action, and no second removal."""
        engine.actor_finished_while_paused()
        engine.pause()
        engine.tick()

        engine.resume()
        engine.tick()
        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert engine.applier.journal == [f"dispose:{ISSUE}"]

    def test_ordinary_planning_still_owns_disposal_when_never_paused(
        self, engine: Engine
    ) -> None:
        """The unpaused path is untouched: the planner disposes, as before."""
        engine.actor_finished_while_paused()

        engine.tick()

        assert engine.worktrees.paths == [WORKTREE]
        assert engine.applier.journal == [
            f"plan:{ActionType.CLEANUP_SESSION.value}"
        ]
