"""A completion's gating effects decide whether the success-only ones commit.

The failure direction these pin is the one #337 round 3 F1 named: ORDERING is
not a fail-stop boundary. ``ActionApplier`` catches an ordinary close error,
reports a FAILED result, and applies the rest of the batch — so a terminal close
planned first and then failing was still followed by the release of
``in-progress``, leaving an OPEN issue with its execution claim given up. That
is the state the scheduler cannot tell from work never started, which is exactly
the unbounded relaunch the terminal disposition exists to prevent.

These tests apply real action lists through the real :class:`ActionApplier` with
a repository host that refuses the close, and assert on what did and did not
reach the ports.
"""

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    ActionResult,
    ActionResultType,
    AddLabelAction,
    CloseIssueAction,
    RemoveLabelAction,
    ResetRetryIssueAction,
)
from issue_orchestrator.control.completion_effect_gate import (
    CompletionGateFailure,
    CompletionGateKind,
    CompletionGateOutcome,
    apply_completion_actions_gated,
    completion_gate_kind,
    completion_gate_outcome_after_apply,
    effective_terminal_status,
    evaluate_completion_gate_outcome,
    finalize_completion_gate_history,
    is_completion_gate_action,
    partition_completion_gate_actions,
)
from issue_orchestrator.control.completion_gate_surfaces import (
    GATE_FAILURE_NARRATIVES,
    build_completion_gate_failure_actions,
)
from issue_orchestrator.control.result_only_completion import (
    CLOSE_COMMENT,
    ResultOnlyCloseIssueAction,
)
from issue_orchestrator.domain.models import SessionHistoryEntry, SessionStatus

ISSUE = 337
IN_PROGRESS = "in-progress"


@pytest.fixture
def labels() -> MagicMock:
    port = MagicMock()
    port.has_label.return_value = True
    return port


@pytest.fixture
def repository_host() -> MagicMock:
    return MagicMock()


@pytest.fixture
def applier(labels: MagicMock, repository_host: MagicMock) -> ActionApplier:
    """The real applier over mocked ports — the batch behaviour is the subject."""
    return ActionApplier(
        labels=labels,
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=repository_host,
        worktree_manager=MagicMock(),
        fresh_issue_reader=None,
        reconcile=False,
    )


def _surface_for(kind: CompletionGateKind, detail: str) -> list:
    """The durable surface one gate's failure produces, through the dispatch."""
    return build_completion_gate_failure_actions(
        CompletionGateOutcome(
            committed=False,
            failures=(CompletionGateFailure(kind=kind, detail=detail),),
        ),
        issue_number=ISSUE,
        needs_human_label="needs-human",
        session_id="issue-337",
        runtime_minutes=1.0,
    )


def result_only_completion_actions() -> list:
    """What the completion planner emits for a settled result-only run."""
    return [
        ResultOnlyCloseIssueAction(
            issue_number=ISSUE,
            comment=CLOSE_COMMENT,
            reason="Completed run delivered its result with no code to merge",
        ),
        RemoveLabelAction(
            issue_number=ISSUE,
            label=IN_PROGRESS,
            reason="Session completed successfully",
        ),
    ]


class TestAFailedTerminalCloseCannotReleaseTheClaim:
    """The effect boundary #337 round 3 F1 required, proved at apply time."""

    def test_a_refused_close_withholds_the_claim_release(
        self, applier: ActionApplier, labels: MagicMock, repository_host: MagicMock
    ) -> None:
        repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")

        applied, error = apply_completion_actions_gated(
            applier, result_only_completion_actions(), issue_number=ISSUE
        )

        assert error is None
        assert [result.result_type for result in applied] == [
            ActionResultType.FAILURE
        ]
        labels.remove_label.assert_not_called()

    def test_the_issue_is_left_claimed_and_open_not_released(
        self, applier: ActionApplier, labels: MagicMock, repository_host: MagicMock
    ) -> None:
        """What the BATCH did and did not commit — not the durable boundary.

        Withholding the release is the half this module owns. Whether the
        resulting state actually survives into the next planning cycle is
        proved in ``test_result_only_close_failure_recovery.py``, because
        ordering and withholding alone never made it durable (#337 F1-R).
        """
        repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")

        apply_completion_actions_gated(
            applier, result_only_completion_actions(), issue_number=ISSUE
        )

        assert labels.remove_label.call_args_list == []
        # No close committed, so nothing moved the issue out of OPEN either.
        assert repository_host.update_issue_state.call_count == 1

    def test_a_committed_close_still_releases_the_claim(
        self, applier: ActionApplier, labels: MagicMock, repository_host: MagicMock
    ) -> None:
        """The falsification control: the gate only withholds on FAILURE."""
        applied, error = apply_completion_actions_gated(
            applier, result_only_completion_actions(), issue_number=ISSUE
        )

        assert error is None
        assert all(result.success for result in applied)
        repository_host.update_issue_state.assert_called_once_with(ISSUE, "closed")
        labels.remove_label.assert_called_once_with(ISSUE, IN_PROGRESS)

    def test_a_refused_close_terminalizes_the_completion_as_failed(
        self, applier: ActionApplier, repository_host: MagicMock
    ) -> None:
        """A run whose disposition did not commit is not a clean success."""
        repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")

        applied, apply_error = apply_completion_actions_gated(
            applier, result_only_completion_actions(), issue_number=ISSUE
        )
        outcome = completion_gate_outcome_after_apply(applied, apply_error)

        assert outcome.failed
        assert outcome.failed_kinds() == {CompletionGateKind.RESULT_ONLY_CLOSE}
        assert (
            effective_terminal_status(SessionStatus.COMPLETED, outcome)
            is SessionStatus.FAILED
        )

    def test_the_history_row_records_the_failed_disposition(
        self, applier: ActionApplier, repository_host: MagicMock
    ) -> None:
        repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")

        applied, apply_error = apply_completion_actions_gated(
            applier, result_only_completion_actions(), issue_number=ISSUE
        )
        entry = finalize_completion_gate_history(
            SessionHistoryEntry(
                issue_number=ISSUE,
                title="evidence run",
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=1,
            ),
            completion_gate_outcome_after_apply(applied, apply_error),
        )

        assert entry.status == "failed"
        assert "GitHub refused" in (entry.status_reason or "")

    def test_a_failed_close_is_not_reported_as_a_failed_reset(self) -> None:
        """Each surface is keyed on its OWN gate, not on any failure.

        Telling an operator that "Reset & Retry did not complete" for a run that
        never mandated a reset would be a false report.
        """
        actions = _surface_for(CompletionGateKind.RESULT_ONLY_CLOSE, "GitHub refused")

        assert "Reset & Retry" not in actions[1].comment
        assert "Result-Only Close Did Not Complete" in actions[1].comment
        assert "GitHub refused" in actions[1].comment

    def test_a_failed_reset_still_reaches_its_needs_human_surface(self) -> None:
        """The pre-existing member's operator surface must not have been lost."""
        actions = _surface_for(
            CompletionGateKind.MANDATED_RESET, "reset owner failed"
        )

        assert [type(action).__name__ for action in actions] == [
            "AddLabelAction",
            "AddCommentAction",
        ]
        assert "Reset & Retry Did Not Complete" in actions[1].comment
        assert "reset owner failed" in actions[1].comment

    def test_a_committed_gate_surfaces_nothing(self) -> None:
        """The success path writes nothing to GitHub."""
        assert (
            build_completion_gate_failure_actions(
                CompletionGateOutcome(committed=True),
                issue_number=ISSUE,
                needs_human_label="needs-human",
                session_id="issue-337",
                runtime_minutes=1.0,
            )
            == []
        )

    def test_every_gate_kind_has_a_surface(self) -> None:
        """A gate with no durable surface is the F1 defect, not a design choice."""
        assert set(GATE_FAILURE_NARRATIVES) == set(CompletionGateKind)


class TestOnlyTheResultOnlyCloseGatesTheBatch:
    """A generic close must keep behaving exactly as it did."""

    def test_a_generic_close_is_not_a_gate(self) -> None:
        assert completion_gate_kind(CloseIssueAction(issue_number=ISSUE)) is None
        assert not is_completion_gate_action(CloseIssueAction(issue_number=ISSUE))

    def test_a_failed_generic_close_does_not_withhold_its_siblings(
        self, applier: ActionApplier, labels: MagicMock, repository_host: MagicMock
    ) -> None:
        """Tech_lead terminal effects and close-on-merge are unchanged."""
        repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")

        applied, _error = apply_completion_actions_gated(
            applier,
            [
                CloseIssueAction(issue_number=ISSUE, reason="tracking issue done"),
                RemoveLabelAction(
                    issue_number=ISSUE, label=IN_PROGRESS, reason="done"
                ),
            ],
            issue_number=ISSUE,
        )

        assert len(applied) == 2
        labels.remove_label.assert_called_once_with(ISSUE, IN_PROGRESS)

    def test_the_result_only_close_is_a_gate(self) -> None:
        action = ResultOnlyCloseIssueAction(issue_number=ISSUE)

        assert completion_gate_kind(action) is CompletionGateKind.RESULT_ONLY_CLOSE

    def test_the_mandated_reset_is_still_a_gate(self) -> None:
        """The pre-existing member must not have been displaced."""
        action = ResetRetryIssueAction(issue_number=ISSUE, proposal_id="A1")

        assert completion_gate_kind(action) is CompletionGateKind.MANDATED_RESET

    def test_gates_are_partitioned_ahead_of_the_success_only_effects(self) -> None:
        close = ResultOnlyCloseIssueAction(issue_number=ISSUE)
        release = RemoveLabelAction(issue_number=ISSUE, label=IN_PROGRESS)
        comment = AddLabelAction(issue_number=ISSUE, label="done")

        gates, remainder = partition_completion_gate_actions(
            [release, close, comment]
        )

        assert gates == [close]
        assert remainder == [release, comment]

    def test_an_ungated_completion_applies_in_one_pass(
        self, applier: ActionApplier, labels: MagicMock
    ) -> None:
        """Ordinary completions keep exactly today's behaviour."""
        applied, error = apply_completion_actions_gated(
            applier,
            [RemoveLabelAction(issue_number=ISSUE, label=IN_PROGRESS)],
            issue_number=ISSUE,
        )

        assert error is None
        assert len(applied) == 1
        labels.remove_label.assert_called_once_with(ISSUE, IN_PROGRESS)


class TestTheGateVerdictFoldsWhatActuallyCommitted:
    def test_a_skipped_gate_counts_as_committed(self) -> None:
        """A stale downgrade is a non-failure: the owner surfaced instead."""
        action = ResetRetryIssueAction(issue_number=ISSUE, proposal_id="A1")
        skipped = ActionResult(
            action=action, result_type=ActionResultType.SKIPPED, error=None
        )

        assert evaluate_completion_gate_outcome([skipped]).committed

    def test_a_raised_apply_can_never_be_a_success(self) -> None:
        outcome = completion_gate_outcome_after_apply([], RuntimeError("claim lost"))

        assert outcome.failed
        assert "claim lost" in outcome.failure_summary()
        assert (
            effective_terminal_status(SessionStatus.COMPLETED, outcome)
            is SessionStatus.FAILED
        )

    def test_a_raised_apply_names_no_gate_and_surfaces_nothing(self) -> None:
        """No gate reached a verdict, so no owner's report would be true.

        A second GitHub write immediately after a reconciliation/claim raise
        would re-fail and mask the re-raise; the terminal is already FAILED.
        """
        outcome = completion_gate_outcome_after_apply([], RuntimeError("claim lost"))

        assert outcome.failed_kinds() == frozenset()
        assert outcome.unjudged is not None
        assert (
            build_completion_gate_failure_actions(
                outcome,
                issue_number=ISSUE,
                needs_human_label="needs-human",
                session_id="issue-337",
                runtime_minutes=1.0,
            )
            == []
        )
