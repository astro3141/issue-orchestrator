"""A validation_failed candidate reaches its next attempt without a restart (#195).

The incident, reproduced five times (#146, #173, #178, #193, #194), is one
chain:

    session ends validation_failed
      -> session_history gains the entry (IN-MEMORY, per process)
        -> QueueCache.evaluate_issue -> REJECTED_EXCLUDED
          -> cached_queue_issues drops the issue
            -> detect_stale_in_progress(issues=cached_queue_issues) never sees it
              -> _plan_stale_cleanup is never handed it, in-progress never clears
                -> nothing in THIS process can ever consider the issue again

A restart worked for two structural reasons, and both are process-local:
``session_history`` starts empty, and startup sweeps the local label store for
in-progress issues the cached queue omitted. Neither has a live-tick
equivalent, so the transition needed an operator.

The fix separates the two jobs a history entry was doing at once: it is the
operator's RECORD of what a session did, and it is the session-derived half of
the duplicate-launch CLAIM. Releasing an abandoned candidate retires the claim
and keeps the record.

These tests pin the fix's failure directions, in the order the issue states
them. The central proof — one engine process, next tick reaches the next
attempt — lives with the lifecycle harness in
``tests/simulated_scenarios/test_abandoned_candidate_recovery.py``; what is
proved here is the discrimination that makes it safe, and the invariants it
must not break.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    ActionType,
    ReleaseAbandonedIssueAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.control_operation_ownership import (
    ControlOperationOwnership,
)
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NeedsHumanCause
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.control.stale_detection import detect_stale_in_progress
from issue_orchestrator.domain.control_operation import (
    ControlOperationKey,
    ControlOperationKind,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.events import EventContext
from issue_orchestrator.execution.control_operation_ownership_store import (
    SqliteControlOperationOwnershipStore,
)
from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
from issue_orchestrator.infra.config import Config
from issue_orchestrator.observation.observer import SessionObserver
from tests.conftest import MockEventSink, MockGitHubAdapter

REPO = "test/repo"
AGENT = "agent:backend"
# The stranded candidate: publication refused, terminal disposed, worktree gone.
ABANDONED = 194
# Ordinary schedulable work, so no test can pass by the engine being inert.
OPEN_WORK = 47
IN_PROGRESS = "in-progress"
SHA_A = "1" * 40


def _config(tmp_path: Path) -> Config:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    config = Config(repo=REPO, repo_root=tmp_path, max_concurrent_sessions=4)
    config.agents = {AGENT: AgentConfig(prompt_path=prompt)}
    return config


def _issue(number: int, *, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        labels=[AGENT, *(labels or [])],
        repo=REPO,
    )


def _history(number: int, status: str, *, pr_url: str | None = None) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=number,
        title=f"Issue {number}",
        agent_type=AGENT,
        status=status,  # type: ignore[arg-type]
        runtime_minutes=3,
        pr_url=pr_url,
    )


def _awaiting_merge_record(number: int) -> SessionHistoryEntry:
    """The record ``PrPendingHistoryRecovery`` rehydrates at startup.

    Status ``completed`` with a PR URL: owned by the awaiting-merge reconciler,
    never abandoned, and the exact shape #46's docstring names as one of the
    ``REJECTED_EXCLUDED`` cases that must keep answering as it does today.
    """
    return _history(number, "completed", pr_url="https://example.test/pr/318")


def _cache(config: Config, state: OrchestratorState, *issues: Issue) -> QueueCache:
    cache = QueueCache(config, state)
    cache.replace_from_refresh(list(issues))
    return cache


# ---------------------------------------------------------------------------
# The discrimination inside REJECTED_EXCLUDED
# ---------------------------------------------------------------------------


class TestTheQueueOwnerNamesOnlyTheOwnerlessIssues:
    """Directions 3 and 4: running sessions and awaiting-merge are untouched."""

    def test_the_abandoned_candidate_is_named(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        cache = _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS]))

        assert state.cached_queue_issues == []
        assert [i.number for i in cache.abandoned_candidates().issues] == [
            ABANDONED
        ]

    def test_a_running_session_is_never_named(self, tmp_path: Path) -> None:
        """A terminal owns the issue; the exclusion means "busy", not "gone"."""
        from tests.unit.test_planner import make_session

        config = _config(tmp_path)
        issue = _issue(ABANDONED, labels=[IN_PROGRESS])
        state = OrchestratorState()
        # Both halves at once: a prior attempt's history row AND a session
        # running right now. The running owner wins.
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        state.active_sessions.append(make_session(issue))
        cache = _cache(config, state, issue)

        assert [i.number for i in cache.reconciliation_only_issues()] == [ABANDONED]
        assert cache.abandoned_candidates().issues == ()

    def test_the_awaiting_merge_presentation_record_is_never_named(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_awaiting_merge_record(ABANDONED))
        cache = _cache(config, state, _issue(ABANDONED, labels=["pr-pending"]))

        assert [i.number for i in cache.reconciliation_only_issues()] == [ABANDONED]
        assert cache.abandoned_candidates().issues == ()

    def test_a_later_completion_retires_an_earlier_refusal(
        self, tmp_path: Path
    ) -> None:
        """The LATEST entry decides, so a candidate that later landed is owned."""
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        state.session_history.append(_awaiting_merge_record(ABANDONED))
        cache = _cache(config, state, _issue(ABANDONED, labels=["pr-pending"]))

        assert cache.abandoned_candidates().issues == ()

    def test_a_live_control_operation_is_never_named(self, tmp_path: Path) -> None:
        """#146's terminal-less owner still owns the issue."""
        config = _config(tmp_path)
        issue = _issue(ABANDONED, labels=[IN_PROGRESS])
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        ControlOperationOwnership(
            state, SqliteControlOperationOwnershipStore(tmp_path / STORE_FILENAME)
        ).claim(
            ControlOperationKey(
                GitHubIssueKey(repo=REPO, external_id=str(ABANDONED)),
                SHA_A,
                ControlOperationKind.PUBLICATION_REVALIDATION_CONTINUATION,
            )
        )
        cache = _cache(config, state, issue)

        assert [i.number for i in cache.reconciliation_only_issues()] == [ABANDONED]
        assert cache.abandoned_candidates().issues == ()

    @pytest.mark.parametrize(
        "status", ["blocked", "needs_human", "failed", "timed_out", "completed"]
    )
    def test_no_other_completion_path_is_named(
        self, tmp_path: Path, status: str
    ) -> None:
        """Direction 7: every other terminal outcome keeps an owner."""
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, status))
        cache = _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS]))

        assert cache.abandoned_candidates().issues == ()

    def test_a_queued_issue_is_never_named(self, tmp_path: Path) -> None:
        """A subset of ``reconciliation_only_issues``, so still queue-disjoint."""
        config = _config(tmp_path)
        state = OrchestratorState()
        cache = _cache(config, state, _issue(OPEN_WORK))

        assert [i.number for i in state.cached_queue_issues] == [OPEN_WORK]
        assert cache.abandoned_candidates().issues == ()

    def test_an_operator_narrowed_run_keeps_its_narrow_blast_radius(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        config.filtering.issue = OPEN_WORK
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        cache = _cache(
            config, state, _issue(ABANDONED, labels=[IN_PROGRESS]), _issue(OPEN_WORK)
        )

        assert cache.abandoned_candidates().issues == ()

    def test_the_history_owner_answers_from_the_latest_entry_only(self) -> None:
        history = [
            _history(ABANDONED, "validation_failed"),
            _history(OPEN_WORK, "completed"),
            _history(ABANDONED, "completed", pr_url="https://example.test/pr/1"),
            _history(OPEN_WORK, "validation_failed"),
        ]

        assert SessionHistoryOwner(
            history
        ).abandoned_after_completion_issue_numbers() == frozenset({OPEN_WORK})


# ---------------------------------------------------------------------------
# The staleness question is asked of the reconciliation-visible set
# ---------------------------------------------------------------------------


class TestTheLiveTickSeesTheStrandedIssue:
    def _observer(self, config: Config) -> SessionObserver:
        return SessionObserver(
            config=config,
            events=MockEventSink(),
            session_runner=MagicMock(),
            repository_host=MockGitHubAdapter(),
            session_output=MagicMock(),
        )

    def test_an_abandoned_issue_outside_the_queue_reads_stale(
        self, tmp_path: Path
    ) -> None:
        """The bug: this returned [] because the queue had already dropped it."""
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        cache = _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS]))

        stale = detect_stale_in_progress(
            self._observer(config),
            state,
            MockEventSink(),
            EventContext(tick_id=1),
            cache.abandoned_candidates().issues,
        )

        assert [i.number for i in stale] == [ABANDONED]

    def test_an_issue_still_in_the_queue_is_answered_exactly_as_before(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        state = OrchestratorState()
        cache = _cache(config, state, _issue(OPEN_WORK, labels=[IN_PROGRESS]))

        stale = detect_stale_in_progress(
            self._observer(config),
            state,
            MockEventSink(),
            EventContext(tick_id=1),
            cache.abandoned_candidates().issues,
        )

        assert [i.number for i in stale] == [OPEN_WORK]

    def test_the_awaiting_merge_record_is_not_offered_to_the_detector(
        self, tmp_path: Path
    ) -> None:
        """Direction 4, at the seam: it never even reaches the staleness test."""
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_awaiting_merge_record(ABANDONED))
        cache = _cache(
            config, state, _issue(ABANDONED, labels=["pr-pending", IN_PROGRESS])
        )

        stale = detect_stale_in_progress(
            self._observer(config),
            state,
            MockEventSink(),
            EventContext(tick_id=1),
            cache.abandoned_candidates().issues,
        )

        assert stale == []

    def test_each_issue_is_offered_at_most_once(self, tmp_path: Path) -> None:
        """Queue and abandoned set are disjoint, so no duplicate stale event."""
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        cache = _cache(
            config,
            state,
            _issue(ABANDONED, labels=[IN_PROGRESS]),
            _issue(OPEN_WORK, labels=[IN_PROGRESS]),
        )
        events = MockEventSink()

        stale = detect_stale_in_progress(
            self._observer(config),
            state,
            events,
            EventContext(tick_id=1),
            cache.abandoned_candidates().issues,
        )

        assert sorted(i.number for i in stale) == sorted([ABANDONED, OPEN_WORK])
        assert len(stale) == 2


# ---------------------------------------------------------------------------
# What the planner does with the two kinds of stale issue
# ---------------------------------------------------------------------------


class TestThePlannerSeparatesTheTwoStaleKinds:
    def _planner(self, config: Config) -> Planner:
        return Planner(config=config, scheduler=Scheduler(config=config))

    def _snapshot(self, config: Config, state: OrchestratorState, cache: QueueCache):
        return FactGatherer(
            config=config, repository_host=MockGitHubAdapter()
        ).create_snapshot(
            state,
            state.cached_queue_issues,
            stale_in_progress_issues=[
                *state.cached_queue_issues,
                *cache.abandoned_candidates().issues,
            ],
            reconcile_only_issues=cache.reconciliation_only_issues(),
            abandoned_candidates=cache.abandoned_candidates(),
        )

    def _stale_actions(
        self, config: Config, state: OrchestratorState, cache: QueueCache
    ) -> list:
        """Every planned action that mutates a stale ``in-progress`` label."""
        plan = self._planner(config).plan(self._snapshot(config, state, cache))
        return [
            action
            for action in plan.actions
            if action.action_type
            in {ActionType.RELEASE_ABANDONED_ISSUE, ActionType.REMOVE_LABEL}
        ]

    def test_an_abandoned_issue_gets_the_release_command(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        cache = _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS]))

        actions = self._stale_actions(config, state, cache)

        assert [a.action_type for a in actions] == [
            ActionType.RELEASE_ABANDONED_ISSUE
        ]
        release = actions[0]
        assert isinstance(release, ReleaseAbandonedIssueAction)
        assert release.issue_number == ABANDONED
        assert release.label == LabelManager(config).in_progress

    def test_an_ordinary_stale_issue_still_gets_a_plain_removal(
        self, tmp_path: Path
    ) -> None:
        """Direction 8 at the plan level: the pre-existing path is untouched."""
        config = _config(tmp_path)
        state = OrchestratorState()
        cache = _cache(config, state, _issue(OPEN_WORK, labels=[IN_PROGRESS]))

        actions = self._stale_actions(config, state, cache)

        assert [a.action_type for a in actions] == [ActionType.REMOVE_LABEL]
        removal = actions[0]
        assert isinstance(removal, RemoveLabelAction)
        assert removal.issue_number == OPEN_WORK
        assert removal.reason == "stale - no running session"

    def test_the_release_sheds_exactly_the_in_progress_label(
        self, tmp_path: Path
    ) -> None:
        """Direction 8: visibility changes, authority does not.

        The command's only GitHub write is the same removal the stale path has
        always planned — no marker is added, and no other label is touched.
        """
        config = _config(tmp_path)
        release = ReleaseAbandonedIssueAction(
            issue_number=ABANDONED,
            label=LabelManager(config).in_progress,
            reason="abandoned after completion - no owner, no running session",
        )

        removal = release.label_removal()

        assert removal.action_type is ActionType.REMOVE_LABEL
        assert removal.issue_number == ABANDONED
        assert removal.label == IN_PROGRESS


# ---------------------------------------------------------------------------
# What the release does, and what it refuses to do
# ---------------------------------------------------------------------------


class TestTheReleaseTouchesNothingDurable:
    def _applier(
        self, state: OrchestratorState
    ) -> tuple[ActionApplier, MagicMock]:
        labels = MagicMock()
        labels.get_labels.return_value = [AGENT, IN_PROGRESS]
        return (
            ActionApplier(
                labels=labels,
                sessions=MagicMock(),
                events=MockEventSink(),
                history_owner=SessionHistoryOwner(lambda: state.session_history),
            ),
            labels,
        )

    def _release(self) -> ReleaseAbandonedIssueAction:
        return ReleaseAbandonedIssueAction(
            issue_number=ABANDONED, label=IN_PROGRESS, reason="abandoned"
        )

    def _stranded_state(self) -> OrchestratorState:
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        return state

    def test_the_label_removal_is_the_only_github_write(self) -> None:
        state = self._stranded_state()
        applier, labels = self._applier(state)

        result = applier.apply(self._release())

        assert result.success
        labels.remove_label.assert_called_once_with(ABANDONED, IN_PROGRESS)
        labels.add_label.assert_not_called()

    def test_a_failed_removal_releases_no_claim(self) -> None:
        """The ordering invariant: nothing is handed back on a failed shed."""
        state = self._stranded_state()
        applier, labels = self._applier(state)
        labels.remove_label.side_effect = RuntimeError("GitHub said no")

        result = applier.apply(self._release())

        assert not result.success
        assert not state.session_history[0].claim_released

    def test_an_unwired_history_owner_fails_loudly(self) -> None:
        """No silent half-release: the shed does not happen either."""
        labels = MagicMock()
        applier = ActionApplier(
            labels=labels, sessions=MagicMock(), events=MockEventSink()
        )

        result = applier.apply(self._release())

        assert not result.success
        labels.remove_label.assert_not_called()

    def test_the_failed_session_stays_in_the_operator_s_history(self) -> None:
        """Direction 6: no fact a later attempt is judged by is discarded.

        The release retires the CLAIM. The record — status, reason, PR URL,
        worktree path — is what the dashboard's history lane and
        ``session_failure_diagnosis`` read to explain the failure, and it
        survives untouched.
        """
        state = self._stranded_state()
        entry = state.session_history[0]
        entry.status_reason = "Validation failed after session completion"
        applier, _labels = self._applier(state)

        assert applier.apply(self._release()).success

        assert len(state.session_history) == 1
        assert entry.status == "validation_failed"
        assert entry.status_reason == "Validation failed after session completion"
        assert entry.claim_released is True

    def test_only_the_named_issue_is_released(self) -> None:
        state = self._stranded_state()
        state.session_history.append(_history(OPEN_WORK, "validation_failed"))
        applier, _labels = self._applier(state)

        assert applier.apply(self._release()).success

        released = {
            e.issue_number: e.claim_released for e in state.session_history
        }
        assert released == {ABANDONED: True, OPEN_WORK: False}

    def test_a_released_issue_re_enters_the_queue_on_the_next_refresh(
        self, tmp_path: Path
    ) -> None:
        """Direction 1's mechanism: the duplicate-launch guard stops rejecting."""
        config = _config(tmp_path)
        issue = _issue(ABANDONED, labels=[IN_PROGRESS])
        state = self._stranded_state()
        cache = _cache(config, state, issue)

        assert cache.evaluate_issue(issue) is QueueMutationStatus.REJECTED_EXCLUDED

        SessionHistoryOwner(state.session_history).release_claim(ABANDONED)

        assert cache.evaluate_issue(issue) is QueueMutationStatus.ACCEPTED
        assert [i.number for i in cache.replace_from_refresh([issue])] == [ABANDONED]

    def test_a_released_issue_is_not_released_again(self, tmp_path: Path) -> None:
        """The release is a one-shot per entry, so no tick loop can form."""
        config = _config(tmp_path)
        state = self._stranded_state()
        cache = _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS]))

        SessionHistoryOwner(state.session_history).release_claim(ABANDONED)

        assert cache.abandoned_candidates().issues == ()

    def test_the_planner_stops_skipping_a_released_issue(
        self, tmp_path: Path
    ) -> None:
        """The other half of the gate: the planner's own history exclusion."""
        config = _config(tmp_path)
        state = self._stranded_state()
        gatherer = FactGatherer(config=config, repository_host=MockGitHubAdapter())

        assert gatherer.create_snapshot(
            state, []
        ).session_history_issue_numbers == frozenset({ABANDONED})

        SessionHistoryOwner(state.session_history).release_claim(ABANDONED)

        assert gatherer.create_snapshot(state, []).session_history_issue_numbers == (
            frozenset()
        )

    @pytest.mark.parametrize(
        "blocking_label", ["blocked-failed", "publish-failed", "blocked:pr-closed"]
    )
    def test_a_blocking_label_still_refuses_the_released_issue(
        self, tmp_path: Path, blocking_label: str
    ) -> None:
        """Direction 2: no allowance is created. The budgets still bind.

        The release makes the issue *considerable* again; whether another
        attempt is legitimate stays entirely with the durable records the
        scheduler reads, exactly as after a restart. A blocking label the issue
        already carries — including the one the publish-failure budget plants
        once ``max_consecutive_publish_failures`` is reached — refuses it.
        """
        config = _config(tmp_path)
        state = self._stranded_state()
        SessionHistoryOwner(state.session_history).release_claim(ABANDONED)

        decision = Scheduler(config=config).evaluate_issues(
            [_issue(ABANDONED, labels=[IN_PROGRESS, blocking_label])]
        )[0]

        assert not decision.available

    def test_the_durable_failure_record_survives_the_release(self) -> None:
        """Direction 2 again: nothing is refunded.

        The publication refusal marker and the publish-failure counter are the
        records the next attempt is judged by. The release must leave both
        exactly where it found them — it removes ONE label, and mutates no
        counter at all.
        """
        state = self._stranded_state()
        applier, labels = self._applier(state)
        labels.get_labels.return_value = [
            AGENT,
            IN_PROGRESS,
            "validation-failed",
            "publish-fail-count-2",
        ]

        result = applier.apply(self._release())

        assert result.success
        assert labels.remove_label.call_args_list == [
            ((ABANDONED, IN_PROGRESS), {})
        ]
        labels.add_label.assert_not_called()


# ---------------------------------------------------------------------------
# The bound on relaunch, and the escalation that makes it terminal
# ---------------------------------------------------------------------------


def _released(entry: SessionHistoryEntry) -> SessionHistoryEntry:
    entry.claim_released = True
    return entry


class TestTheReleaseBudgetBoundsRelaunch:
    """The release retires the only gate on relaunch, so it needs its own.

    ``session_history_issue_numbers`` is the sole member of the planner's launch
    filter that can hold a ``validation_failed`` issue with no PR and no queued
    review, and every other budget in the system is per-session or reached from
    a different completion path. Without a ceiling here, a deterministically
    failing validation command relaunches for the life of the process.
    """

    def _cache_with(
        self, tmp_path: Path, *, granted: int, max_releases: int
    ) -> tuple[QueueCache, OrchestratorState]:
        config = _config(tmp_path)
        config.retry.max_abandoned_releases = max_releases
        state = OrchestratorState()
        for _ in range(granted):
            state.session_history.append(
                _released(_history(ABANDONED, "validation_failed"))
            )
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        return _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS])), state

    def test_a_first_attempt_is_within_budget(self, tmp_path: Path) -> None:
        cache, _state = self._cache_with(tmp_path, granted=0, max_releases=2)

        verdict = cache.abandoned_candidates().verdict(ABANDONED)

        assert verdict is not None
        assert verdict.releases_granted == 0
        assert verdict.max_releases == 2
        assert not verdict.exhausted

    def test_the_ceiling_is_reached_after_the_configured_releases(
        self, tmp_path: Path
    ) -> None:
        cache, _state = self._cache_with(tmp_path, granted=2, max_releases=2)

        verdict = cache.abandoned_candidates().verdict(ABANDONED)

        assert verdict is not None
        assert verdict.releases_granted == 2
        assert verdict.exhausted

    def test_the_ceiling_comes_from_config(self, tmp_path: Path) -> None:
        """An operator who wants more automatic attempts gets them."""
        cache, _state = self._cache_with(tmp_path, granted=2, max_releases=5)

        verdict = cache.abandoned_candidates().verdict(ABANDONED)

        assert verdict is not None
        assert not verdict.exhausted

    def test_an_exhausted_candidate_is_still_named(self, tmp_path: Path) -> None:
        """It has to be: its stale label is shed and its escalation planned here.

        Filtering it out of the abandoned set would leave the issue wearing an
        ``in-progress`` label nothing ever removes and say nothing to the
        operator about why the attempts stopped — the exact silent stranding
        #195 exists to remove.
        """
        cache, _state = self._cache_with(tmp_path, granted=9, max_releases=2)

        assert [i.number for i in cache.abandoned_candidates().issues] == [ABANDONED]

    def test_a_release_for_another_reason_does_not_spend_the_budget(self) -> None:
        """Only released ABANDONED entries count, so a future release path
        cannot silently consume this ceiling."""
        state = OrchestratorState()
        state.session_history.append(_released(_history(ABANDONED, "completed")))
        state.session_history.append(_history(ABANDONED, "validation_failed"))

        owner = SessionHistoryOwner(state.session_history)

        assert owner.abandoned_releases_granted(ABANDONED) == 0

    def test_the_release_reports_the_counter_it_advanced(self) -> None:
        state = OrchestratorState()
        state.session_history.append(
            _released(_history(ABANDONED, "validation_failed"))
        )
        state.session_history.append(_history(ABANDONED, "validation_failed"))

        result = SessionHistoryOwner(state.session_history).release_claim(ABANDONED)

        assert result.released_entries == 1
        assert result.releases_granted == 2


class TestTheExhaustedCandidateEscalatesInsteadOfRelaunching:
    def _planner(self, config: Config) -> Planner:
        return Planner(config=config, scheduler=Scheduler(config=config))

    def _plan(self, tmp_path: Path, *, granted: int, max_releases: int = 2) -> list:
        config = _config(tmp_path)
        config.retry.max_abandoned_releases = max_releases
        state = OrchestratorState()
        for _ in range(granted):
            state.session_history.append(
                _released(_history(ABANDONED, "validation_failed"))
            )
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        cache = _cache(config, state, _issue(ABANDONED, labels=[IN_PROGRESS]))
        snapshot = FactGatherer(
            config=config, repository_host=MockGitHubAdapter()
        ).create_snapshot(
            state,
            state.cached_queue_issues,
            stale_in_progress_issues=list(cache.abandoned_candidates().issues),
            reconcile_only_issues=cache.reconciliation_only_issues(),
            abandoned_candidates=cache.abandoned_candidates(),
        )
        return list(self._planner(config).plan(snapshot).actions)

    def test_within_budget_the_release_carries_no_escalation(
        self, tmp_path: Path
    ) -> None:
        releases = [
            a
            for a in self._plan(tmp_path, granted=1)
            if a.action_type is ActionType.RELEASE_ABANDONED_ISSUE
        ]

        assert len(releases) == 1
        assert releases[0].escalation_label == ""
        assert releases[0].escalation() is None

    def test_the_exhausting_release_plants_the_blocking_label(
        self, tmp_path: Path
    ) -> None:
        """The bound is enforced by a label the scheduler already refuses —
        the same shape ``max_consecutive_publish_failures`` escalates with."""
        config = _config(tmp_path)
        actions = self._plan(tmp_path, granted=2)
        releases = [
            a for a in actions if a.action_type is ActionType.RELEASE_ABANDONED_ISSUE
        ]

        assert len(releases) == 1
        escalation = releases[0].escalation()
        assert escalation is not None
        assert escalation.label == LabelManager(config).needs_human
        assert escalation.needs_human_cause is NeedsHumanCause.SESSION_LIFECYCLE

    def test_the_escalation_explains_itself_to_the_operator(
        self, tmp_path: Path
    ) -> None:
        comments = [
            a
            for a in self._plan(tmp_path, granted=2)
            if a.action_type is ActionType.ADD_COMMENT
        ]

        assert len(comments) == 1
        assert comments[0].number == ABANDONED
        assert "max_abandoned_releases" in comments[0].comment
        assert "2" in comments[0].comment

    def test_no_comment_is_posted_while_the_budget_holds(
        self, tmp_path: Path
    ) -> None:
        """The escalation is a one-off, not a per-tick announcement."""
        assert [
            a
            for a in self._plan(tmp_path, granted=0)
            if a.action_type is ActionType.ADD_COMMENT
        ] == []

    def test_the_escalated_issue_is_refused_by_the_scheduler(
        self, tmp_path: Path
    ) -> None:
        """The loop terminates because the label the escalation plants blocks.

        Without it the released issue is available on the very next pass: no
        active session, no ``in-progress``, and ``validation-failed`` is a
        LIFECYCLE label the scheduler does not refuse.
        """
        config = _config(tmp_path)
        scheduler = Scheduler(config=config)
        needs_human = LabelManager(config).needs_human

        assert scheduler.evaluate_issues([_issue(ABANDONED)])[0].available
        assert not scheduler.evaluate_issues(
            [_issue(ABANDONED, labels=[needs_human])]
        )[0].available


class TestTheApplierOrdersTheEscalatedRelease:
    def _applier(self, state: OrchestratorState) -> tuple[ActionApplier, MagicMock]:
        labels = MagicMock()
        labels.get_labels.return_value = [AGENT, IN_PROGRESS]
        labels.has_label.return_value = False
        return (
            ActionApplier(
                labels=labels,
                sessions=MagicMock(),
                events=MockEventSink(),
                history_owner=SessionHistoryOwner(lambda: state.session_history),
            ),
            labels,
        )

    def _escalated(self) -> ReleaseAbandonedIssueAction:
        return ReleaseAbandonedIssueAction(
            issue_number=ABANDONED,
            label=IN_PROGRESS,
            reason="abandoned",
            escalation_label="needs-human",
            escalation_reason="budget spent",
        )

    def _state(self) -> OrchestratorState:
        state = OrchestratorState()
        state.session_history.append(
            _released(_history(ABANDONED, "validation_failed"))
        )
        state.session_history.append(
            _released(_history(ABANDONED, "validation_failed"))
        )
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        return state

    def test_the_block_lands_before_the_issue_is_handed_back(self) -> None:
        state = self._state()
        applier, labels = self._applier(state)
        order: list[str] = []
        labels.add_label.side_effect = lambda *_a, **_k: order.append("add")
        labels.remove_label.side_effect = lambda *_a, **_k: order.append("remove")

        result = applier.apply(self._escalated())

        assert result.success
        assert order == ["add", "remove"]
        labels.add_label.assert_called_once_with(ABANDONED, "needs-human")
        assert state.session_history[-1].claim_released is True

    def test_a_failed_escalation_hands_nothing_back(self) -> None:
        """No unbounded attempt slips through a failed block: the shed does not
        happen either, so the next tick sees the same stale label and re-plans."""
        state = self._state()
        applier, labels = self._applier(state)
        labels.add_label.side_effect = RuntimeError("GitHub said no")

        result = applier.apply(self._escalated())

        assert not result.success
        labels.remove_label.assert_not_called()
        assert state.session_history[-1].claim_released is False

    def test_the_unexhausted_release_still_writes_only_the_removal(self) -> None:
        """Direction 8 is unchanged for every release inside the budget."""
        state = OrchestratorState()
        state.session_history.append(_history(ABANDONED, "validation_failed"))
        applier, labels = self._applier(state)

        result = applier.apply(
            ReleaseAbandonedIssueAction(
                issue_number=ABANDONED, label=IN_PROGRESS, reason="abandoned"
            )
        )

        assert result.success
        labels.add_label.assert_not_called()
        labels.remove_label.assert_called_once_with(ABANDONED, IN_PROGRESS)
