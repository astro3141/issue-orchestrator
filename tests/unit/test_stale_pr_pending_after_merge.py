"""A merged PR must not leave its still-open issue parked on pr-pending (#113).

The defect: two owners held one fact and were not wired together. Startup
detected that a locally ``pr-pending`` issue had no open PR and used that fact
only to *skip*; the awaiting-merge reconciler could already shed the label but
never saw the issue, because startup is what rehydrates its candidate history.
A human-merged, still-open issue therefore parked on ``reason=pr_pending``
forever.

These tests walk the wired path end to end through the real owners —
reconciler → planner → applier → scheduler — and pin the authority direction
twice over:

1. a KNOWN-empty closing linkage and an UNKNOWN one must never collapse into
   the same value; and
2. a continuation merge must not inherit the terminal recovery's authority.
   The merge establishes exactly one thing — ``pr-pending`` is stale. The issue
   is intentionally still OPEN, so unrelated failure/blocking labels describe
   its current condition and must survive, still gating selection.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ActionType, RecoverTerminalIssueAction
from issue_orchestrator.control.awaiting_merge_reconciler import (
    AwaitingMergeReconciler,
)
from issue_orchestrator.control.close_on_merge import close_on_merge_comment
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.domain.models import (
    DiscoveredAwaitingMergeReconciliation,
    Issue,
    MergedIssueDisposition,
    OrchestratorState,
    SessionHistoryEntry,
    TerminalRecoveryLabelScope,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import (
    ClosingIssueReferencesRead,
    PRInfo,
)

_MERGED_AT = "2026-08-03T13:52:09Z"
_PR_URL = "https://github.com/owner/repo/pull/49"

# An unrelated failure/blocking label the continuation merge says nothing
# about: the issue was blocked before the merge and is still blocked after it.
_UNRELATED_BLOCK = "blocked:claim-lost"
_UNRELATED_FAILURE = "publish-failed"


def _config() -> Config:
    return Config(repo="owner/repo", max_concurrent_sessions=3)


def _issue(*, labels: list[str] | None = None, state: str = "open") -> Issue:
    return Issue(
        number=45,
        title="Partial work landed by a non-closing merge",
        labels=["agent:backend"] + (labels if labels is not None else ["pr-pending"]),
        state=state,
    )


def _history_entry() -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=45,
        title="Partial work landed by a non-closing merge",
        agent_type="agent:backend",
        status="completed",
        runtime_minutes=0,
        pr_url=_PR_URL,
        status_reason="Recovered awaiting merge state on startup",
    )


def _merged_pr() -> PRInfo:
    return PRInfo(
        number=49,
        title="Land partial work (Refs #45)",
        url=_PR_URL,
        branch="45-partial",
        body="Refs #45",
        state="merged",
        labels=[],
        merged_at=_MERGED_AT,
    )


def _host(*, closes: tuple[int, ...] | None) -> MagicMock:
    """A repository host whose merged PR registers ``closes`` (None = UNKNOWN)."""
    host = MagicMock()
    host.get_pr.return_value = _merged_pr()
    host.get_issue.return_value = _issue()
    host.issue_closed_on_or_after.return_value = False
    host.read_pr_closing_issue_references.return_value = (
        ClosingIssueReferencesRead.unknown()
        if closes is None
        else ClosingIssueReferencesRead.known(closes)
    )
    return host


def _plan_for(host: MagicMock):
    """Run the real reconciler and planner over one rehydrated entry."""
    state = OrchestratorState(session_history=[_history_entry()])
    result = AwaitingMergeReconciler(host, clock=lambda: 1234.5).discover(state)
    config = _config()
    planner = Planner(config=config, scheduler=Scheduler(config))
    plan = planner.plan(
        OrchestratorSnapshot(
            issues=(),
            active_sessions=(),
            pending_reviews=(),
            pending_reworks=(),
            pending_tech_lead=(),
            paused=False,
            discovered_awaiting_merge_reconciliations=result.reconciliations,
            discovered_awaiting_merge_drifts=result.drifts,
        )
    )
    return result, plan


def test_known_no_link_sheds_pr_pending_without_closing_or_blocking() -> None:
    """Acceptance 1: the stale label is shed, the issue stays open, no blocked
    label is added — and the recovery carries only the narrow authority."""
    host = _host(closes=())

    result, plan = _plan_for(host)

    assert result.discovered == 1
    assert (
        result.reconciliations[0].merged_disposition
        is MergedIssueDisposition.CONTINUE
    )
    actions = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, RecoverTerminalIssueAction)
    assert action.issue_number == 45
    # The close stays off, and the shed is bounded to the one label this merge
    # actually proves stale.
    assert action.close_issue is False
    assert action.label_scope is TerminalRecoveryLabelScope.STALE_PR_PENDING
    # No blocked:pr-closed drift: the PR merged, it did not close unmerged.
    assert plan.actions_of_type(ActionType.SYNC_LABELS) == []
    host.update_issue_state.assert_not_called()


def test_shed_issue_is_eligible_for_ordinary_selection_again() -> None:
    """Acceptance 2: with pr-pending gone the same OPEN issue is available —
    no longer decision=skip reason=pr_pending."""
    scheduler = Scheduler(_config())

    parked = scheduler.evaluate_issues(
        [_issue(labels=["pr-pending"])], check_dependencies=False,
    )[0]
    assert parked.available is False
    assert parked.reason.value == "pr_pending"

    shed = scheduler.evaluate_issues(
        [_issue(labels=[])], check_dependencies=False,
    )[0]
    assert shed.available is True


def test_known_linked_merge_still_closes_the_issue() -> None:
    """Acceptance 3: the existing close-on-merge fallback is unchanged for an
    issue GitHub registered as closed by the merge."""
    host = _host(closes=(45,))

    result, plan = _plan_for(host)

    assert (
        result.reconciliations[0].merged_disposition
        is MergedIssueDisposition.CLOSE_AND_RECOVER
    )
    action = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)[0]
    assert isinstance(action, RecoverTerminalIssueAction)
    assert action.close_issue is True
    assert action.merged_at == _MERGED_AT
    # A registered link means the work HAS landed: the full terminal shed is
    # unchanged for this branch.
    assert action.label_scope is TerminalRecoveryLabelScope.RECOVERED_WORKFLOW


def test_close_on_merge_comment_states_the_surviving_trigger() -> None:
    """The comment is orchestrator-authored text on a public issue, so it must
    describe the only condition that now reaches it.

    Before #113 the close fired when the PR registered NO closing reference,
    and the comment said so. #113 inverted that: the close now fires only when
    the PR DID register one. The old sentence would assert, on a PR whose own
    sidebar shows the linkage, that no linkage exists — actively misdirecting
    whoever investigates. Pinned here so it cannot drift back.
    """
    body = close_on_merge_comment(_PR_URL, 49)

    assert _PR_URL in body
    assert "registered this issue as a closing reference" in body
    assert "auto-close did not fire" in body
    assert "no closing reference" not in body


def test_close_on_merge_comment_falls_back_to_the_pr_number() -> None:
    """An empty ``pr_url`` must still name the PR, not render a bare colon."""
    body = close_on_merge_comment("", 49)

    assert "PR #49" in body


def test_known_linked_but_reopened_issue_is_not_reclosed() -> None:
    """Acceptance 4: a close event at/after the merge proves the auto-close
    fired, so the open state is a deliberate reopen. Protection holds."""
    host = _host(closes=(45,))
    host.issue_closed_on_or_after.return_value = True

    result, plan = _plan_for(host)

    action = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)[0]
    assert isinstance(action, RecoverTerminalIssueAction)
    assert action.close_issue is False
    assert (
        result.reconciliations[0].merged_disposition
        is MergedIssueDisposition.RECOVER
    )
    # A deliberate reopen after a successful auto-close is not a continuation
    # merge: the work landed, so the full shed still applies.
    assert action.label_scope is TerminalRecoveryLabelScope.RECOVERED_WORKFLOW


def test_unknown_linkage_neither_closes_nor_sheds() -> None:
    """Acceptance 5: fail closed. Nothing is planned at all, so the entry
    stays reconcilable for a later pass to retry."""
    host = _host(closes=None)

    result, plan = _plan_for(host)

    assert result.discovered == 0
    assert result.skipped == 1
    assert plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE) == []
    assert plan.actions_of_type(ActionType.SYNC_LABELS) == []


def test_closed_without_merge_still_takes_the_drift_path() -> None:
    """Acceptance 6: the closed-unmerged path is untouched — it still adds
    blocked:pr-closed and removes pr-pending, and never reads the linkage."""
    host = _host(closes=())
    closed_pr = PRInfo(
        number=49,
        title="Abandoned",
        url=_PR_URL,
        branch="45-partial",
        body="",
        state="closed",
        labels=[],
    )
    host.get_pr.return_value = closed_pr

    state = OrchestratorState(session_history=[_history_entry()])
    result = AwaitingMergeReconciler(
        host, label_manager=LabelManager(_config()), clock=lambda: 1234.5,
    ).discover(state)

    assert len(result.drifts) == 1
    assert result.drifts[0].issue_number == 45
    host.read_pr_closing_issue_references.assert_not_called()


def test_merged_and_closed_issue_keeps_its_terminal_behaviour() -> None:
    """Acceptance 7: the ordinary merged+auto-closed path is unchanged, and
    pays no linkage read at all."""
    host = _host(closes=())
    host.get_issue.return_value = _issue(state="closed")

    result, plan = _plan_for(host)

    assert result.discovered == 1
    assert (
        result.reconciliations[0].merged_disposition
        is MergedIssueDisposition.RECOVER
    )
    action = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)[0]
    assert isinstance(action, RecoverTerminalIssueAction)
    assert action.close_issue is False
    assert action.label_scope is TerminalRecoveryLabelScope.RECOVERED_WORKFLOW
    host.read_pr_closing_issue_references.assert_not_called()


# ---------------------------------------------------------------------------
# Acceptance 9 — mutation proof, authority direction
# ---------------------------------------------------------------------------


def test_unknown_linkage_can_never_be_read_as_an_empty_set() -> None:
    """Weakening ``unknown`` into ``known(())`` must fail HERE, loudly.

    An UNKNOWN read carries no issue numbers and refuses to answer whether it
    closes anything; only that refusal keeps "we could not read the relation"
    from silently becoming "GitHub says this PR closes nothing" — the value
    that sheds a queue-gating label.
    """
    unknown = ClosingIssueReferencesRead.unknown()

    assert unknown.is_known is False
    assert unknown.issue_numbers == ()
    with pytest.raises(ValueError):
        unknown.closes(45)

    known_empty = ClosingIssueReferencesRead.known(())
    assert known_empty.is_known is True
    assert known_empty.closes(45) is False
    assert known_empty != unknown


def test_unknown_read_cannot_be_constructed_carrying_issue_numbers() -> None:
    """The two states cannot be blurred from the construction side either."""
    with pytest.raises(ValueError):
        ClosingIssueReferencesRead("UNKNOWN", (45,))


def test_an_unreadable_disposition_can_never_become_a_fact() -> None:
    """UNREADABLE means "mutate nothing", so it must not survive as a fact.

    The reconciler skips such an entry, but the invariant is enforced at the
    fact itself: a future caller that forgets the skip fails loudly here rather
    than planning a recovery off an unreadable linkage.
    """
    with pytest.raises(ValueError):
        DiscoveredAwaitingMergeReconciliation(
            issue_number=45,
            pr_number=49,
            pr_url=_PR_URL,
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
            merged_disposition=MergedIssueDisposition.UNREADABLE,
        )


# ---------------------------------------------------------------------------
# A continuation merge's authority stops at pr-pending (#113 review round 2)
# ---------------------------------------------------------------------------

_CONTINUING_ISSUE_LABELS = [
    "agent:backend",
    "pr-pending",
    _UNRELATED_FAILURE,
    "publish-fail-count-2",
    _UNRELATED_BLOCK,
]


def _applier_for(host: MagicMock, entry: SessionHistoryEntry) -> ActionApplier:
    """A real ActionApplier over mocked ports, reading the issue's live labels."""
    labels_port = MagicMock()
    fresh_reader = MagicMock()
    fresh_reader.read_issue_labels.return_value = list(_CONTINUING_ISSUE_LABELS)
    applier = ActionApplier(
        labels=labels_port,
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=host,
        fresh_issue_reader=fresh_reader,
        label_manager=LabelManager(_config()),
        reconcile=False,
    )
    applier.history_owner = SessionHistoryOwner([entry])
    return applier


def _apply_and_report(
    action: RecoverTerminalIssueAction,
) -> tuple[set[str], list[str], MagicMock]:
    """Apply *action* against the continuing issue.

    Returns (labels surviving on the issue, labels removed, repository host).
    """
    host = _host(closes=())
    host.get_issue.return_value = _issue(labels=_CONTINUING_ISSUE_LABELS[1:])
    entry = _history_entry()
    applier = _applier_for(host, entry)

    result = applier.apply(action)
    assert result.success, result.error

    removed = [
        call.args[1] for call in applier.labels.remove_label.call_args_list
    ]
    surviving = set(_CONTINUING_ISSUE_LABELS) - set(removed)
    return surviving, removed, host


def test_continuation_merge_removes_pr_pending_and_nothing_else() -> None:
    """The required regression: the issue starts with pr-pending PLUS unrelated
    failure/blocking state.

    After reconciliation ``pr-pending`` is gone, every other label survives,
    the issue is never closed, and the scheduler still refuses to schedule it —
    now for the blocking label that was always the real reason, not for a stale
    pr-pending. A continuation merge proves that one label is stale and says
    nothing whatsoever about why the issue is blocked.
    """
    _, plan = _plan_for(_host(closes=()))
    action = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)[0]
    assert isinstance(action, RecoverTerminalIssueAction)

    surviving, removed, host = _apply_and_report(action)

    assert removed == ["pr-pending"]
    assert surviving == {
        "agent:backend",
        _UNRELATED_FAILURE,
        "publish-fail-count-2",
        _UNRELATED_BLOCK,
    }
    # The issue stays OPEN — no close, no state write of any kind.
    host.update_issue_state.assert_not_called()

    # ...and scheduling remains subject to the label that survived.
    decision = Scheduler(_config()).evaluate_issues(
        [_issue(labels=sorted(surviving - {"agent:backend"}))],
        check_dependencies=False,
    )[0]
    assert decision.available is False
    assert decision.reason.value == "blocked_label"


def test_routing_the_continuation_back_through_the_full_shed_fails_it() -> None:
    """Mutation proof for the regression above.

    The only thing standing between a continuation merge and the terminal
    recovery's full authority is the action's ``label_scope``. Flip it back to
    RECOVERED_WORKFLOW — the routing this PR's first round used — and the same
    apply clears the unrelated failure and blocking labels too, silently
    unblocking an issue on evidence that never spoke to why it was blocked.
    Every assertion the regression makes about survival is inverted here, so
    the regression cannot pass under that routing.
    """
    _, plan = _plan_for(_host(closes=()))
    planned = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)[0]
    assert isinstance(planned, RecoverTerminalIssueAction)
    mutated = replace(
        planned, label_scope=TerminalRecoveryLabelScope.RECOVERED_WORKFLOW
    )

    surviving, removed, _host_used = _apply_and_report(mutated)

    assert set(removed) == {
        "pr-pending",
        _UNRELATED_FAILURE,
        "publish-fail-count-2",
        _UNRELATED_BLOCK,
    }
    assert surviving == {"agent:backend"}
    # And the consequence the regression exists to prevent: with its blocking
    # label gone the issue is schedulable again, on evidence that never said
    # the block was resolved.
    decision = Scheduler(_config()).evaluate_issues(
        [_issue(labels=[])], check_dependencies=False,
    )[0]
    assert decision.available is True
