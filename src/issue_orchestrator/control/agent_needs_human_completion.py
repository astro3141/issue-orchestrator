"""What a SUPPRESSED needs-human escalation leaves for the operator (#257).

The sibling of :mod:`agent_blocked_completion`, and it exists for the half of
that module's problem that had no planned path at all.

``coding-done needs_human`` writes ``push_branch`` + ``add_needs_human_label``
+ ``post_comment``. For an ordinary session all three run, and the label is what
holds the issue — which is why ``NEEDS_HUMAN`` deliberately keeps
``in-progress`` and plans nothing. A BOUNDED tech_lead role gets a different
tuple: ``shape_requested_actions_for_tech_lead`` drops the comment, the
zero-code lane drops the push, and
:meth:`~.subject_recovery_authority.SubjectRecoveryAuthority.completion_request_outcome`
refuses the label. Nothing is left, and "plan nothing" then means the run's
explicit request for a human decision reaches no label, no comment, and no
trace — the ownership claim it was relying on is reaped a tick or two later and
the issue returns to the queue with the question lost.

So this path speaks exactly where that tuple empties out: when the run's role
may NOT leave the recovery label. It is the substitute the owner's docstring
describes for a path whose planned effect is not one splice-able label — the
suppression is not a label this path would otherwise have added, it is a label
the COMPLETION RECORD asked for and the seventh door refused — so the label
decision stays with the door and the sentence explaining it comes from
:meth:`~.subject_recovery_authority.SubjectRecoveryAuthority.suppression_note`,
the one voice every other suppression already uses.

A run whose role MAY leave the label keeps today's policy untouched: the
requested label lands through the shared needs-human block owner, the agent's
own comment carries the question, and the claim is held.
"""

from __future__ import annotations

from ..domain.models import Session
from .actions import Action, AddCommentAction, RemoveLabelAction
from .label_manager import LabelManager
from .reconciliation import ExpectedState
from .subject_recovery_authority import SubjectRecoveryAuthority


def agent_needs_human_actions(
    session: Session,
    expected: ExpectedState,
    *,
    label_manager: LabelManager,
    question: str | None,
    subject_recovery: SubjectRecoveryAuthority,
) -> list[Action]:
    """Actions for a needs-human completion whose escalation label was refused.

    Empty for a role that may leave the label — that run's escalation is
    already visible, and planning a second comment or releasing a claim the
    label is holding would be a change to a policy this has no quarrel with.

    Empty for a non-``issue-`` session for the same reason
    :func:`~.agent_blocked_completion.agent_blocked_actions` is: a review or
    rework terminal's escalation does not map onto issue state, and its parent
    workflow owns what happens next.

    Otherwise the run gets the voice the blocked path already has — the question
    it asked, the reason its subject carries no ``needs-human`` label, and the
    release of the ``in-progress`` claim nothing is left holding.
    """
    if subject_recovery.may_leave_recovery_label:
        return []
    if not session.terminal_id.startswith("issue-"):
        return []
    question_text = (question or "").strip() or "No question provided."
    note = subject_recovery.suppression_note(label_manager.needs_human)
    return [
        AddCommentAction(
            number=session.issue.number,
            comment=(
                "🙋 **Human Input Requested**\n\n"
                "The agent stopped and asked for a human decision.\n\n"
                f"**Question:** {question_text}\n"
                f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                f"- Session: `{session.terminal_id}`\n\n"
                f"{note}"
            ),
            reason="Notify about a needs-human escalation that carries no label",
            expected=expected,
        ),
        RemoveLabelAction(
            issue_number=session.issue.number,
            label=label_manager.in_progress,
            reason="Needs-human escalation left no label to hold the claim",
            expected=expected,
        ),
    ]
