"""Provider-block recovery sees issues ordinary scheduling excludes (issue #46).

The incident: a provider circuit opened, the provider-impact owner correctly
blocked an issue, and then the circuit recovered — but the block was never
retired. Nothing was wrong with the clear path itself. The reconciliation owner
simply never saw the issue:

    startup rehydrates a pr-pending awaiting-merge presentation record
      -> QueueCache.evaluate_issue -> REJECTED_EXCLUDED (duplicate-launch guard)
        -> cached_queue_issues drops the issue
          -> snapshot.issues (the SCHEDULING set) drops it
            -> plan_provider_impact iterated snapshot.issues -> no subject
              -> no ApplyProviderImpactAction(CLEARED) is ever planned

The exclusion is recreated on every startup, so no restart could clear it
either: a self-maintaining deadlock. These tests pin the separation that fixes
it — scheduling eligibility and reconciliation visibility are different sets —
in both directions:

  * the queue-cache owner names the excluded-but-in-scope issues (producer);
  * the snapshot carries them without making them launchable (payload);
  * the provider-impact owner reconciles them, and ONLY reconciles them
    (consumer): a reconcile-only subject may be CLEARED, never newly BLOCKED.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ActionType, LaunchSessionAction
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.provider_impact import ProviderImpactTransition
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryProviderCircuitStore
from tests.conftest import MockGitHubAdapter
from tests.unit.continuation_helpers import inert_control_continuation

PROVIDER = "claude-code"
AGENT = "agent:backend"
# The awaiting-merge issue from the incident: in scope, pr-pending, provider
# blocked, and excluded from the queue by the duplicate-launch guard.
STUCK = 40
# An ordinary schedulable issue, used to prove the tick is not simply inert.
OPEN_WORK = 47


def _config(tmp_path: Path) -> Config:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    config = Config(repo="test/repo", repo_root=tmp_path, max_concurrent_sessions=4)
    config.agents = {AGENT: AgentConfig(prompt_path=prompt, provider=PROVIDER)}
    # No fetch during a tick: the seeded cache IS the queue, so what the tick
    # sees is exactly what the queue-cache owner derived.
    config.fetch_layer_network_sync_seconds = 3600
    return config


def _manager(*, events=None) -> ProviderResilienceManager:
    from issue_orchestrator.infra.config_models import (
        ProviderCircuitBreakerConfig,
        ProviderResilienceConfig,
    )

    return ProviderResilienceManager(
        config=ProviderResilienceConfig(
            circuit_breaker=ProviderCircuitBreakerConfig(auth_failure_threshold=1)
        ),
        store=InMemoryProviderCircuitStore(),
        events=events or MagicMock(),
    )


def _issue(number: int, *, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        labels=[AGENT, *(labels or [])],
    )


def _awaiting_merge_record(issue: Issue) -> SessionHistoryEntry:
    """The record `StartupManager._recover_pr_pending_history` rehydrates.

    Created for dashboard awaiting-merge visibility, NOT by a session
    completing in this run — which is precisely why it must not be consumable
    as execution authority.
    """
    return SessionHistoryEntry(
        issue_number=issue.number,
        title=issue.title,
        agent_type=AGENT,
        status="completed",
        runtime_minutes=0,
        pr_url="https://example.test/pr/41",
        status_reason="Recovered awaiting merge state on startup",
    )


# ---------------------------------------------------------------------------
# 1. The producer: the queue-cache owner names the excluded in-scope issues
# ---------------------------------------------------------------------------


class TestTheQueueOwnerSeparatesTheTwoQuestions:
    def test_the_awaiting_merge_record_excludes_the_issue_from_the_queue(
        self, tmp_path: Path
    ) -> None:
        """The duplicate-launch guard is untouched: this is the bug's premise."""
        config = _config(tmp_path)
        issue = _issue(STUCK, labels=["pr-pending"])
        state = OrchestratorState()
        state.session_history.append(_awaiting_merge_record(issue))
        cache = QueueCache(config, state)

        assert cache.evaluate_issue(issue) is QueueMutationStatus.REJECTED_EXCLUDED
        assert cache.replace_from_refresh([issue]) == []
        assert [i.number for i in state.cached_scope_issues] == [STUCK]

    def test_the_excluded_issue_is_still_offered_to_reconciliation(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        issue = _issue(STUCK, labels=["pr-pending"])
        state = OrchestratorState()
        state.session_history.append(_awaiting_merge_record(issue))
        cache = QueueCache(config, state)
        cache.replace_from_refresh([issue])

        assert [i.number for i in cache.reconciliation_only_issues()] == [STUCK]

    def test_an_issue_with_a_running_session_is_also_offered(
        self, tmp_path: Path
    ) -> None:
        """Active work excludes from scheduling for the same reason it must not
        hide the issue from reconciliation."""
        from tests.unit.test_planner import make_session

        config = _config(tmp_path)
        issue = _issue(STUCK)
        state = OrchestratorState()
        state.active_sessions.append(make_session(issue))
        cache = QueueCache(config, state)
        cache.replace_from_refresh([issue])

        assert state.cached_queue_issues == []
        assert [i.number for i in cache.reconciliation_only_issues()] == [STUCK]

    def test_a_queued_issue_is_never_offered_twice(self, tmp_path: Path) -> None:
        """Queue and reconcile-only are disjoint, so consumers need no dedupe."""
        config = _config(tmp_path)
        state = OrchestratorState()
        cache = QueueCache(config, state)
        cache.replace_from_refresh([_issue(OPEN_WORK)])

        assert [i.number for i in state.cached_queue_issues] == [OPEN_WORK]
        assert cache.reconciliation_only_issues() == []

    def test_an_operator_narrowed_run_keeps_its_narrow_blast_radius(
        self, tmp_path: Path
    ) -> None:
        """``--issue N`` rejects OUT_OF_SCOPE, not EXCLUDED.

        Reconciliation visibility widens by exactly the duplicate-launch guard
        and no further: an operator who scoped the engine to one issue must not
        get provider-label mutations on every other in-scope issue.
        """
        config = _config(tmp_path)
        config.filtering.issue = OPEN_WORK
        state = OrchestratorState()
        state.session_history.append(_awaiting_merge_record(_issue(STUCK)))
        cache = QueueCache(config, state)
        cache.replace_from_refresh([_issue(STUCK, labels=["pr-pending"]), _issue(OPEN_WORK)])

        assert [i.number for i in state.cached_queue_issues] == [OPEN_WORK]
        assert cache.reconciliation_only_issues() == []


# ---------------------------------------------------------------------------
# 2. The payload: the snapshot carries visibility without carrying authority
# ---------------------------------------------------------------------------


class TestTheSnapshotCarriesBothSets:
    def test_the_gatherer_carries_the_reconcile_only_set(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        issue = _issue(STUCK, labels=["pr-pending"])
        state = OrchestratorState()
        state.session_history.append(_awaiting_merge_record(issue))
        cache = QueueCache(config, state)
        cache.replace_from_refresh([issue, _issue(OPEN_WORK)])

        snapshot = FactGatherer(
            config=config, repository_host=MockGitHubAdapter()
        ).create_snapshot(
            state,
            state.cached_queue_issues,
            reconcile_only_issues=cache.reconciliation_only_issues(),
        )

        assert [i.number for i in snapshot.issues] == [OPEN_WORK]
        assert [i.number for i in snapshot.reconcile_only_issues] == [STUCK]

    def test_reconciliation_subjects_mark_where_authority_stops(self) -> None:
        scheduled = _issue(OPEN_WORK)
        excluded = _issue(STUCK)
        snapshot = OrchestratorSnapshot(
            issues=(scheduled,),
            active_sessions=(),
            pending_reviews=(),
            pending_reworks=(),
            pending_tech_lead=(),
            paused=False,
            reconcile_only_issues=(excluded,),
        )

        authority = {
            subject.issue.number: subject.may_originate_block
            for subject in snapshot.reconciliation_subjects
        }
        assert authority == {OPEN_WORK: True, STUCK: False}


# ---------------------------------------------------------------------------
# 3. The consumer: the provider-impact owner, planned by the real Planner
# ---------------------------------------------------------------------------


def _plan_actions(config: Config, manager: ProviderResilienceManager, snapshot):
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        provider_resilience=manager,
        label_manager=LabelManager(config),
    )
    return planner.plan(snapshot).actions


def _provider_impact(actions, transition: ProviderImpactTransition):
    return [
        action
        for action in actions
        if getattr(action, "action_type", None) == ActionType.APPLY_PROVIDER_IMPACT
        and action.transition is transition
    ]


def _snapshot(*, issues=(), reconcile_only=()):
    from tests.unit.test_planner import make_snapshot

    return make_snapshot(
        issues=list(issues), reconcile_only_issues=tuple(reconcile_only)
    )


class TestReconciliationReachesTheExcludedIssue:
    def test_the_old_coupling_leaves_the_block_stuck_after_recovery(
        self, tmp_path: Path
    ) -> None:
        """Failure direction (#46 constraint 7).

        This is the pre-fix data flow reconstructed exactly: reconciliation is
        offered ONLY the session-history-filtered scheduling set. The circuit has
        recovered and the issue still carries the block, yet nothing is planned —
        the label can never be retired by its owner.
        """
        config = _config(tmp_path)
        blocked_label = LabelManager(config).provider_unavailable
        manager = _manager()  # every circuit closed: the provider has recovered
        issue = _issue(STUCK, labels=["pr-pending", blocked_label])

        actions = _plan_actions(config, manager, _snapshot(issues=(), reconcile_only=()))

        assert _provider_impact(actions, ProviderImpactTransition.CLEARED) == []
        assert blocked_label in issue.labels  # still stuck

    def test_reconciliation_visibility_lets_the_owner_clear_the_block(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        blocked_label = LabelManager(config).provider_unavailable
        manager = _manager()
        issue = _issue(STUCK, labels=["pr-pending", blocked_label])

        actions = _plan_actions(
            config, manager, _snapshot(issues=(), reconcile_only=(issue,))
        )

        (cleared,) = _provider_impact(actions, ProviderImpactTransition.CLEARED)
        assert cleared.issue_number == STUCK
        assert cleared.label == blocked_label
        # The owner command carries the label mutation; nothing plans a bare
        # label removal around it (#5980 F1 / #46 constraint 4).
        assert not [
            action
            for action in actions
            if getattr(action, "action_type", None) == ActionType.REMOVE_LABEL
            and getattr(action, "label", "") == blocked_label
        ]

    def test_an_open_circuit_is_not_cleared_by_the_wider_visibility(
        self, tmp_path: Path
    ) -> None:
        """Constraint 8: visibility widened, not the release condition."""
        config = _config(tmp_path)
        blocked_label = LabelManager(config).provider_unavailable
        manager = _manager()
        manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )
        assert manager.is_open(PROVIDER)
        issue = _issue(STUCK, labels=["pr-pending", blocked_label])

        actions = _plan_actions(
            config, manager, _snapshot(issues=(), reconcile_only=(issue,))
        )

        assert _provider_impact(actions, ProviderImpactTransition.CLEARED) == []

    def test_a_reconcile_only_subject_never_originates_a_new_block(
        self, tmp_path: Path
    ) -> None:
        """Visibility is not authority (#46 implementation directive).

        Nothing refused work on this issue's behalf — ordinary scheduling is not
        even considering it — so an outage must not start labelling it. That is
        also what keeps ordinary outage behaviour byte-for-byte unchanged for
        awaiting-merge and actively-running issues (constraint 10).
        """
        config = _config(tmp_path)
        manager = _manager()
        manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )

        actions = _plan_actions(
            config,
            manager,
            _snapshot(issues=(), reconcile_only=(_issue(STUCK, labels=["pr-pending"]),)),
        )

        assert _provider_impact(actions, ProviderImpactTransition.BLOCKED) == []

    def test_an_ordinary_scheduling_subject_is_still_blocked_by_an_outage(
        self, tmp_path: Path
    ) -> None:
        """Constraint 10: the scheduling lane's behaviour is unchanged."""
        config = _config(tmp_path)
        manager = _manager()
        manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )

        actions = _plan_actions(
            config, manager, _snapshot(issues=(_issue(OPEN_WORK),), reconcile_only=())
        )

        (blocked,) = _provider_impact(actions, ProviderImpactTransition.BLOCKED)
        assert blocked.issue_number == OPEN_WORK
        assert blocked.providers == (PROVIDER,)


# ---------------------------------------------------------------------------
# 4. The production tick: state -> run_planning_cycle -> applier -> GitHub
#
# Everything above tests a seam. This is the real chain, on the incident's
# shape, so a regression that derives the subject set but never wires it into
# the tick cannot pass.
# ---------------------------------------------------------------------------


class _RecoveryTick:
    """One real planning cycle over the stuck awaiting-merge issue."""

    def __init__(self, tmp_path: Path) -> None:
        from tests.unit.test_provider_readiness_boundary import RecordingEvents

        self.config = _config(tmp_path)
        self.labels = LabelManager(self.config)
        self.events = RecordingEvents()
        self.manager = _manager(events=self.events)

        self.github = MockGitHubAdapter()
        stuck = _issue(STUCK, labels=["pr-pending", self.labels.provider_unavailable])
        open_work = _issue(OPEN_WORK)
        self.github.issues = [stuck, open_work]
        for issue in self.github.issues:
            self.github.labels[issue.number] = set(issue.labels)

        self.state = OrchestratorState()
        # Exactly what startup leaves behind for an awaiting-merge issue with an
        # open PR, then the first refresh: in scope, out of the queue.
        self.state.session_history.append(_awaiting_merge_record(stuck))
        QueueCache(self.config, self.state).replace_from_refresh(self.github.issues)

        self.applier = ActionApplier(
            labels=self.github,
            sessions=MagicMock(),
            events=self.events,
            repository_host=self.github,
            label_manager=self.labels,
            # No terminal is spawned here; the launch INTENT is what this
            # harness asserts on (see ``planned_launches``).
            session_launcher=lambda _type, _number: None,
        )
        self.fact_gatherer = FactGatherer(
            config=self.config, repository_host=self.github, events=self.events
        )
        self.planner = Planner(
            config=self.config,
            scheduler=Scheduler(self.config),
            provider_resilience=self.manager,
            label_manager=self.labels,
        )
        self.planned_launches: list[int] = []

    def tick(self) -> None:
        from issue_orchestrator.control.orchestrator_support import (
            IssueFetchResilience,
            run_planning_cycle,
        )

        run_planning_cycle(
            config=self.config,
            events=self.events,
            event_context=Mock(enrich=lambda payload: payload),
            state=self.state,
            fact_gatherer=self.fact_gatherer,
            planner=self.planner,
            repository_host=self.github,
            scheduler=Mock(),
            github_workflow=Mock(),
            apply_plan_fn=self._apply,
            clear_discovered_facts_fn=Mock(),
            last_network_sync=time.time(),
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            control_continuation=inert_control_continuation(),
        )

    def _apply(self, plan) -> None:
        self.planned_launches.extend(
            action.number
            for action in plan.actions
            if isinstance(action, LaunchSessionAction)
        )
        for action in plan.actions:
            self.applier.apply(action)

    def issue_labels(self, number: int) -> set[str]:
        return set(self.github.get_issue_labels(number))

    def event_names(self) -> list[str]:
        return self.events.names()


class TestTheProductionTickRecoversTheStuckIssue:
    def test_the_stuck_issue_is_absent_from_the_scheduling_queue(
        self, tmp_path: Path
    ) -> None:
        """The premise, measured on the real state: in scope, not in the queue."""
        tick = _RecoveryTick(tmp_path)

        assert [i.number for i in tick.state.cached_queue_issues] == [OPEN_WORK]
        assert sorted(i.number for i in tick.state.cached_scope_issues) == [
            STUCK,
            OPEN_WORK,
        ]

    def test_recovery_clears_the_block_through_its_owner(self, tmp_path: Path) -> None:
        """Constraints 2, 4, 5: the owner clears the label and records it once."""
        tick = _RecoveryTick(tmp_path)
        blocked_label = tick.labels.provider_unavailable
        tick.manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )
        assert tick.manager.is_open(PROVIDER)

        # The operator re-authenticates; the circuit owner confirms recovery.
        tick.manager.clear_auth_failures(PROVIDER)
        assert not tick.manager.is_open(PROVIDER)

        tick.tick()

        assert blocked_label not in tick.issue_labels(STUCK)
        assert tick.event_names().count(EventName.PROVIDER_ISSUE_UNBLOCKED.value) == 1
        # The block is gone from the predicate the rework/PR scan gates on, so
        # the issue's legitimate next step is reachable without a human
        # reconstructing state (constraint 6).
        assert not tick.labels.is_blocking_any(sorted(tick.issue_labels(STUCK)))

    def test_a_second_tick_does_not_re_announce_the_unblock(
        self, tmp_path: Path
    ) -> None:
        """Constraint 5: exactly once, even though the issue stays visible."""
        tick = _RecoveryTick(tmp_path)
        tick.manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )
        tick.manager.clear_auth_failures(PROVIDER)

        tick.tick()
        tick.tick()

        names = tick.event_names()
        # Both ticks really ran, so "exactly once" is not satisfied by inaction.
        assert names.count(EventName.PLAN_COMPUTED.value) == 2
        assert names.count(EventName.PROVIDER_ISSUE_UNBLOCKED.value) == 1

    def test_the_recovered_issue_is_still_not_relaunched(self, tmp_path: Path) -> None:
        """Constraints 1 and 9: reconciliation saw it; scheduling still will not.

        The same tick launches the ordinary queued issue, so the absence here is
        the duplicate-launch guard and not an inert tick.
        """
        tick = _RecoveryTick(tmp_path)
        tick.manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )
        tick.manager.clear_auth_failures(PROVIDER)

        tick.tick()

        assert STUCK not in tick.planned_launches
        assert OPEN_WORK in tick.planned_launches

    def test_an_open_circuit_keeps_the_block_in_place(self, tmp_path: Path) -> None:
        """Constraint 8, end to end: no clear while the outage is real."""
        tick = _RecoveryTick(tmp_path)
        blocked_label = tick.labels.provider_unavailable
        tick.manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage-1"
        )

        tick.tick()

        assert blocked_label in tick.issue_labels(STUCK)
        assert EventName.PROVIDER_ISSUE_UNBLOCKED.value not in tick.event_names()
