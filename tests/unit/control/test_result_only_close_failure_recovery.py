"""A refused result-only close is bounded into the NEXT planning cycle (#337 F1-R).

Round 3 built the effect boundary: a ``ResultOnlyCloseIssueAction`` that FAILS
withholds the success-only remainder, so ``RemoveLabelAction(in-progress)`` no
longer commits after it. That fixed the same-batch defect and is not re-proved
here.

What round 3 then CLAIMED is what these tests exist to falsify:

    close FAIL -> issue OPEN + in-progress -> out of scheduler selection
               -> recoverable by the ordinary stale-claim path

None of the three arrows held. ``Scheduler`` blocks ``in-progress`` only while
an ACTIVE session also exists, and this session has just terminalized, so the
label is stale and explicitly still eligible. What actually kept the issue out
of the queue was the unreleased session-history claim — process-local, and
carrying status ``failed``, which is deliberately NOT one of
``ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES``. So the ordinary stale cleanup
removes ``in-progress`` and releases nothing, and a restart starts from an open,
unlabelled, finished issue: the unbounded relaunch, one cycle later.

The fix is the one ``domain.models`` already names for a ``failed`` completion —
"plant a BLOCKING label, so the scheduler refuses the issue whether or not any
in-memory gate is retired". These tests start from a repository host that
refuses the close, apply the REAL planned actions through the REAL
``ActionApplier`` and the REAL shared-block owner, and then carry the resulting
issue into a fresh planning cycle through the real ``QueueCache``, ``Scheduler``
and stale-cleanup planner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.abandoned_candidates import AbandonedCandidates
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import Action, RemoveLabelAction
from issue_orchestrator.control.completion_effect_gate import (
    apply_completion_actions_gated,
    completion_gate_outcome_after_apply,
    effective_terminal_status,
    evaluate_completion_gate_outcome,
    finalize_completion_gate_history,
)
from issue_orchestrator.control.completion_gate_surfaces import (
    build_completion_gate_failure_actions,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import (
    NeedsHumanBlock,
    NeedsHumanCause,
)
from issue_orchestrator.control.queue_cache import QueueCache
from issue_orchestrator.control.result_only_completion import (
    CLOSE_COMMENT,
    ResultOnlyCloseIssueAction,
)
from issue_orchestrator.control.scheduler import AvailabilityReason, Scheduler
from issue_orchestrator.control.stale_cleanup_planning import (
    plan_stale_in_progress_actions,
)
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
    SessionStatus,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.config import Config
from tests.conftest import MockEventSink

REPO = "test/repo"
AGENT = "agent:backend"
# The evidence issue: a run that measured something, posted its RESULT comment,
# and produced no code to merge.
ISSUE = 337
SESSION_ID = "issue-337"


class _LiveIssue:
    """The issue as GitHub would hold it, mutated by the real action owners."""

    def __init__(self, labels: set[str]) -> None:
        self.labels = labels
        self.state = "open"
        self.comments: list[str] = []
        self.refuse_close = False

    # -- the label port ActionApplier writes through ----------------------
    def add_label(self, issue_number: int, label: str) -> None:
        assert issue_number == ISSUE
        self.labels.add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        assert issue_number == ISSUE
        self.labels.discard(label)

    def has_label(self, issue_number: int, label: str) -> bool:
        assert issue_number == ISSUE
        return label in self.labels

    # -- the repository host it closes and comments through ---------------
    def update_issue_state(self, issue_number: int, state: str) -> None:
        assert issue_number == ISSUE
        if self.refuse_close:
            raise RuntimeError("GitHub refused the close")
        self.state = state

    def add_comment(self, issue_number: int, comment: str) -> None:
        assert issue_number == ISSUE
        self.comments.append(comment)

    # -- what the next planning cycle reads -------------------------------
    def as_scheduler_issue(self) -> Issue:
        return Issue(
            number=ISSUE,
            title="R27 candidate measurement",
            labels=sorted(self.labels),
            repo=REPO,
            state=self.state,
        )


def _config(tmp_path: Path) -> Config:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    config = Config(repo=REPO, repo_root=tmp_path, max_concurrent_sessions=4)
    config.agents = {AGENT: AgentConfig(prompt_path=prompt)}
    return config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _config(tmp_path)


@pytest.fixture
def label_manager(config: Config) -> LabelManager:
    return LabelManager(config)


@pytest.fixture
def issue(label_manager: LabelManager) -> _LiveIssue:
    """The claimed issue of a run that is completing right now."""
    return _LiveIssue({AGENT, label_manager.in_progress})


@pytest.fixture
def applier(
    issue: _LiveIssue, label_manager: LabelManager, tmp_path: Path
) -> ActionApplier:
    """The real applier, with the real owner of the shared needs-human block.

    The block owner is wired rather than left as the null object: the label this
    surface plants is a GOVERNED one, and routing it through its owner is what
    the production path does.
    """
    return ActionApplier(
        labels=issue,
        sessions=MagicMock(),
        events=MockEventSink(),
        repository_host=issue,
        label_manager=label_manager,
        needs_human_block=NeedsHumanBlock(
            needs_human_label=label_manager.needs_human,
            tech_lead_marker=label_manager.tech_lead_needs_human,
            labels=issue,
            read_labels=lambda _number: sorted(issue.labels),
            quarantined_issue_numbers=frozenset,
            causes=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        ),
    )


def _planned_completion_actions(label_manager: LabelManager) -> list[Action]:
    """What the completion planner emits for a settled result-only run."""
    return [
        ResultOnlyCloseIssueAction(
            issue_number=ISSUE,
            comment=CLOSE_COMMENT,
            reason="Completed run delivered its result with no code to merge",
        ),
        RemoveLabelAction(
            issue_number=ISSUE,
            label=label_manager.in_progress,
            reason="Session completed successfully",
        ),
    ]


def _complete(
    applier: ActionApplier, config: Config, label_manager: LabelManager
) -> SessionHistoryEntry:
    """Run the completion's whole apply-and-surface phase, as production does.

    The gated apply, the effective terminal status, the history row, and the
    durable surface for whichever gate failed — in the same order and from the
    same verdicts ``session_completion.handle_session_completion`` uses.
    """
    applied, apply_error = apply_completion_actions_gated(
        applier, _planned_completion_actions(label_manager), issue_number=ISSUE
    )
    gate_outcome = completion_gate_outcome_after_apply(applied, apply_error)
    entry = finalize_completion_gate_history(
        SessionHistoryEntry(
            issue_number=ISSUE,
            title="R27 candidate measurement",
            agent_type=AGENT,
            status="completed",
            runtime_minutes=12.0,
        ),
        gate_outcome,
    )
    if effective_terminal_status(SessionStatus.COMPLETED, gate_outcome) in (
        SessionStatus.FAILED,
        SessionStatus.TIMED_OUT,
    ):
        surface = build_completion_gate_failure_actions(
            evaluate_completion_gate_outcome(applied),
            issue_number=ISSUE,
            needs_human_label=config.get_label_needs_human(),
            session_id=SESSION_ID,
            runtime_minutes=12.0,
        )
        if surface:
            applier.apply_all(surface)
    return entry


def _next_cycle_decision(
    config: Config,
    label_manager: LabelManager,
    issue: _LiveIssue,
    *,
    history: list[SessionHistoryEntry],
):
    """What the next planning cycle decides about this issue.

    Both gates, in the order a live tick consults them: the process-local queue
    owner, then the scheduler reading the issue's real labels. ``history`` empty
    is the RESTART case — the only state that survives a process boundary is
    what is on the issue itself.

    Returns the rebuilt state, its queue owner, and the scheduler's verdict.
    """
    state = OrchestratorState()
    state.session_history.extend(history)
    cache = QueueCache(config, state)
    scheduler_issue = issue.as_scheduler_issue()
    cache.replace_from_refresh([scheduler_issue])
    decisions = Scheduler(
        config=config, label_manager=label_manager
    ).evaluate_issues([scheduler_issue], check_dependencies=False, active_sessions=[])
    return state, cache, decisions[0]


def _failed_history(entry: SessionHistoryEntry) -> list[SessionHistoryEntry]:
    return [entry]


class TestARefusedCloseLeavesADurableStop:
    """The state a refused close commits, not the state it declines to leave."""

    def test_the_refused_close_withholds_the_release_and_plants_the_block(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        issue.refuse_close = True

        entry = _complete(applier, config, label_manager)

        assert issue.state == "open"
        assert label_manager.in_progress in issue.labels
        assert label_manager.needs_human in issue.labels
        assert entry.status == "failed"

    def test_the_operator_is_told_what_happened_and_what_to_do(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        """A blocking label with no explanation is an unreadable stop."""
        issue.refuse_close = True

        _complete(applier, config, label_manager)

        posted = "\n".join(issue.comments)
        assert "Result-Only Close Did Not Complete" in posted
        assert "GitHub refused the close" in posted
        assert label_manager.needs_human in posted
        # The close comment itself must NOT have been posted: it claims the
        # orchestrator closed the issue, and it did not.
        assert CLOSE_COMMENT not in posted

    def test_a_committed_close_plants_no_block(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        """The falsification control: the surface is the FAILURE direction only."""
        entry = _complete(applier, config, label_manager)

        assert issue.state == "closed"
        assert label_manager.needs_human not in issue.labels
        assert label_manager.in_progress not in issue.labels
        assert entry.status == "completed"
        assert CLOSE_COMMENT in "\n".join(issue.comments)


class TestTheFinishedIssueCannotRelaunchAsFreshWork:
    """Direction 1 of the required proof, followed into the next cycle."""

    def test_the_scheduler_refuses_it_in_the_same_process(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        issue.refuse_close = True
        entry = _complete(applier, config, label_manager)

        _state, _cache, decision = _next_cycle_decision(
            config, label_manager, issue, history=_failed_history(entry)
        )

        assert not decision.available
        assert decision.reason is AvailabilityReason.BLOCKED_LABEL

    def test_the_scheduler_refuses_it_after_a_RESTART(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        """The boundary must not be the process-local history claim.

        A fresh process starts with an empty ``session_history``, so the queue
        owner has nothing to exclude on — the issue IS in the cached queue. Only
        what is written on the issue itself can hold it, which is exactly why
        withholding the release was never enough (#337 F1-R).
        """
        issue.refuse_close = True
        _complete(applier, config, label_manager)

        state, _cache, decision = _next_cycle_decision(
            config, label_manager, issue, history=[]
        )

        assert [i.number for i in state.cached_queue_issues] == [ISSUE]
        assert not decision.available
        assert decision.reason is AvailabilityReason.BLOCKED_LABEL

    def test_the_ordinary_stale_cleanup_does_not_restore_runnability(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        """The path round 3 named as the recovery owner, followed for real.

        ``failed`` is not an abandoned candidate, so the planner plans the
        ORDINARY stale removal: ``in-progress`` comes off and no claim is
        released. Before the durable block that left an open, unlabelled,
        finished issue — indistinguishable from work never started.
        """
        issue.refuse_close = True
        entry = _complete(applier, config, label_manager)
        _state, cache, _decision = _next_cycle_decision(
            config, label_manager, issue, history=_failed_history(entry)
        )
        assert cache.abandoned_candidates().verdict(ISSUE) is None

        applier.apply_all(
            plan_stale_in_progress_actions(
                stale_issues=[issue.as_scheduler_issue()],
                abandoned=AbandonedCandidates(),
                labels=label_manager,
            )
        )

        assert label_manager.in_progress not in issue.labels
        _state, _cache, decision = _next_cycle_decision(
            config, label_manager, issue, history=[]
        )
        assert not decision.available
        assert decision.reason is AvailabilityReason.BLOCKED_LABEL

    def test_without_the_durable_block_that_same_state_relaunches(
        self, config: Config, label_manager: LabelManager, issue: _LiveIssue,
    ) -> None:
        """The finding itself, asserted — so the fix above is load-bearing.

        Exactly the state round 3 claimed was safe: closed refused, release
        withheld, nothing else written. One stale-cleanup pass later the
        scheduler calls the finished evidence issue AVAILABLE.
        """
        applier_without_surface = ActionApplier(
            labels=issue,
            sessions=MagicMock(),
            events=MockEventSink(),
            repository_host=issue,
            label_manager=label_manager,
        )
        issue.refuse_close = True
        apply_completion_actions_gated(
            applier_without_surface,
            _planned_completion_actions(label_manager),
            issue_number=ISSUE,
        )

        applier_without_surface.apply_all(
            plan_stale_in_progress_actions(
                stale_issues=[issue.as_scheduler_issue()],
                abandoned=AbandonedCandidates(),
                labels=label_manager,
            )
        )

        _state, _cache, decision = _next_cycle_decision(
            config, label_manager, issue, history=[]
        )
        assert issue.state == "open"
        assert decision.available


class TestTheEscalationIsReachableAndBounded:
    """Direction 2 of the required proof: the stop ends, and only one way."""

    def test_the_block_is_the_shared_needs_human_one_a_human_clears(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        """Bounded: it ends by a human act, not by a countdown or a retry.

        The same terminus every other ``SESSION_LIFECYCLE`` escalation reaches,
        and the same bounded stop the pre-#337 publish failure gave this run.
        """
        issue.refuse_close = True
        _complete(applier, config, label_manager)

        assert label_manager.is_blocking_any(sorted(issue.labels))
        assert label_manager.needs_human in label_manager.get_blocking(
            sorted(issue.labels)
        )

    def test_clearing_the_block_returns_the_issue_to_the_queue(
        self, applier: ActionApplier, config: Config, label_manager: LabelManager,
        issue: _LiveIssue,
    ) -> None:
        """Reachable: nothing else about the state keeps the issue stuck.

        An operator who reads the comment and decides the work should be redone
        removes the label, and the issue is ordinary schedulable work again — so
        the escalation is a stop, not a dead end.
        """
        issue.refuse_close = True
        _complete(applier, config, label_manager)

        applier.apply_all([
            RemoveLabelAction(
                issue_number=ISSUE,
                label=label_manager.needs_human,
                reason="operator cleared the block",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            ),
            RemoveLabelAction(
                issue_number=ISSUE,
                label=label_manager.in_progress,
                reason="operator cleared the stale claim",
            ),
        ])

        _state, _cache, decision = _next_cycle_decision(
            config, label_manager, issue, history=[]
        )
        assert decision.available
        assert decision.reason is AvailabilityReason.AVAILABLE
