"""The single owner of "may this run change its subject's recovery state?".

Six completion paths ask it (#136 A1/A2, #182). These tests pin the answer
itself and the one voice that explains a suppression; the per-path tests in
``tests/unit/test_completion_action_planner.py`` pin what each path does with
the answer.
"""

import pytest

from issue_orchestrator.control.actions import AddLabelAction
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.subject_recovery_authority import (
    SUBJECT_RECOVERY_UNBOUNDED,
    SubjectRecoveryAuthority,
)
from issue_orchestrator.domain.models import (
    SUBJECT_RECOVERY_ACTIONS,
    RequestedAction,
)
from issue_orchestrator.domain.tech_lead_capabilities import (
    TECH_LEAD_ACTION_CAPABILITIES,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor


@pytest.mark.parametrize("flavor", list(TechLeadSessionFlavor))
def test_the_answer_is_read_from_the_capability_table(
    flavor: TechLeadSessionFlavor,
) -> None:
    """Never matched by flavor name, so a new bounded role inherits the rule.

    A role declares its recovery authority in exactly one place — its capability
    row. Re-deriving the answer here from that row (rather than asserting a
    hard-coded list of flavors) is what makes a role that later GAINS a recovery
    kind lose every suppression in the same edit.
    """
    expected = (
        not flavor.is_issue_focused
        or TECH_LEAD_ACTION_CAPABILITIES.permits_recovery(flavor)
    )

    assert SubjectRecoveryAuthority.for_flavor(flavor).may_leave_recovery_label is expected


def test_the_bounded_planning_role_may_not_touch_its_subject() -> None:
    """#136's least-authority role is the one flavor the rule bites for today."""
    answer = SubjectRecoveryAuthority.for_flavor(
        TechLeadSessionFlavor.PLANNING_INVESTIGATION
    )

    assert answer.may_leave_recovery_label is False


def test_a_non_focused_run_may_retire_its_own_anchor() -> None:
    """A batch/health run's "subject" is bookkeeping, not a live work item."""
    for flavor in (
        TechLeadSessionFlavor.BATCH_REVIEW,
        TechLeadSessionFlavor.HEALTH_REVIEW,
    ):
        assert SubjectRecoveryAuthority.for_flavor(flavor).may_leave_recovery_label


def test_the_default_leaves_generic_recovery_labels_alone() -> None:
    """Non-tech_lead sessions and unproven roles take the conservative branch."""
    assert SUBJECT_RECOVERY_UNBOUNDED.may_leave_recovery_label is True


def test_the_suppression_note_names_every_withheld_label() -> None:
    """One voice, whatever the calling path withheld."""
    note = SubjectRecoveryAuthority(
        may_leave_recovery_label=False
    ).suppression_note("blocked-failed", "needs-human")

    assert "`blocked-failed` or `needs-human`" in note
    assert "holds no recovery authority over the issue" in note
    assert "remains available for normal work" in note


def test_the_suppression_note_refuses_a_role_that_was_allowed_to_label() -> None:
    """Fail loudly rather than tell an operator a label is absent when it is not."""
    with pytest.raises(ValueError, match="may NOT"):
        SUBJECT_RECOVERY_UNBOUNDED.suppression_note("blocked-failed")


def _add_blocked_failed() -> AddLabelAction:
    return AddLabelAction(
        issue_number=7,
        label="blocked-failed",
        reason="something went wrong",
        expected=build_expected_for_mutation(),
    )


def test_an_authorized_run_keeps_the_label_and_the_sentence_naming_it() -> None:
    """The permitted branch is the untouched behavior every other role gets."""
    add_label = _add_blocked_failed()

    outcome = SUBJECT_RECOVERY_UNBOUNDED.recovery_label_outcome(
        add_label=add_label, note_when_added="marked and parked"
    )

    assert outcome.label_actions == (add_label,)
    assert outcome.note == "marked and parked"


def test_a_bounded_run_loses_the_label_and_the_note_explains_that() -> None:
    """Label and note move together, so a comment can never claim a phantom label.

    Asking for the two separately is the drift this returns one object to
    prevent: a path could withhold the label and still print "this issue has
    been marked as ...", or add it and print the suppression note.
    """
    outcome = SubjectRecoveryAuthority(
        may_leave_recovery_label=False
    ).recovery_label_outcome(
        add_label=_add_blocked_failed(), note_when_added="marked and parked"
    )

    assert outcome.label_actions == ()
    assert "`blocked-failed` label was added" in outcome.note
    assert "marked and parked" not in outcome.note


# -- The seventh door: the agent's own completion record (#257) --------------

_BLOCKED_REQUEST_TUPLE = (
    RequestedAction.PUSH_BRANCH,
    RequestedAction.ADD_BLOCKED_LABEL,
    RequestedAction.POST_COMMENT,
)


def test_an_authorized_run_keeps_every_request_it_made() -> None:
    """The permitted branch leaves the untrusted tuple exactly as it arrived."""
    outcome = SUBJECT_RECOVERY_UNBOUNDED.completion_request_outcome(
        _BLOCKED_REQUEST_TUPLE
    )

    assert outcome.requested_actions == _BLOCKED_REQUEST_TUPLE
    assert outcome.suppressed == ()
    assert outcome.detail == ""


def test_a_bounded_run_loses_the_request_and_is_handed_what_it_lost() -> None:
    """Survivors and refusals travel together, so a drop cannot go untraced.

    Handing back only the survivors is what let the completion-record seam
    suppress an escalation and record nothing about it (#257 round 1 F1/A1):
    the caller had no value it was obliged to place anywhere.
    """
    outcome = SubjectRecoveryAuthority(
        may_leave_recovery_label=False
    ).completion_request_outcome(_BLOCKED_REQUEST_TUPLE)

    assert outcome.requested_actions == (
        RequestedAction.PUSH_BRANCH,
        RequestedAction.POST_COMMENT,
    )
    assert outcome.suppressed == (RequestedAction.ADD_BLOCKED_LABEL,)
    assert "add_blocked_label" in outcome.detail
    assert "holds no recovery authority" in outcome.detail


@pytest.mark.parametrize("action", sorted(SUBJECT_RECOVERY_ACTIONS, key=str))
def test_every_recovery_request_in_the_vocabulary_is_refused(
    action: RequestedAction,
) -> None:
    """Derived from the domain's set, so a recovery action joins in one edit.

    ``add_needs_human_label`` retires the subject exactly as ``add_blocked``
    does; the rule is "may this run change its subject's RECOVERY state", not
    "may it add one particular label".
    """
    outcome = SubjectRecoveryAuthority(
        may_leave_recovery_label=False
    ).completion_request_outcome((action,))

    assert outcome.requested_actions == ()
    assert outcome.suppressed == (action,)


def test_a_request_outside_the_vocabulary_is_never_touched() -> None:
    """The door refuses recovery intent, not everything a bounded run asks."""
    ordinary = tuple(
        action for action in RequestedAction if action not in SUBJECT_RECOVERY_ACTIONS
    )

    outcome = SubjectRecoveryAuthority(
        may_leave_recovery_label=False
    ).completion_request_outcome(ordinary)

    assert outcome.requested_actions == ordinary
    assert outcome.suppressed == ()
