"""What an AGENT-reported block means for the issue it stopped (#182).

The other half of the BLOCKED completion split, beside
:mod:`provider_blocked_completion`: that module handles a block the provider
caused, which says nothing about the issue's substance; this one handles the
agent saying it cannot proceed, which is a verdict ON the issue and marks it as
such.

Extracted from :mod:`completion_action_planner` for the reason its sibling was:
the distinction between the two blocks is the whole policy, and it was buried
among a dozen other completion-status branches. Extracting it also gives the
door #182 had to close its own place to be read — the blocking label this path
stamps on ``issue-{N}`` is a change to the SUBJECT's recovery state, and the
threaded :class:`~.subject_recovery_authority.SubjectRecoveryAuthority` is what
decides whether this run's role may make it.
"""

from __future__ import annotations

from ..domain.models import Session
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .label_manager import LabelManager
from .reconciliation import ExpectedState
from .subject_recovery_authority import SubjectRecoveryAuthority


def agent_blocked_actions(
    session: Session,
    expected: ExpectedState,
    *,
    label_manager: LabelManager,
    blocked_label: str | None,
    blocked_reason: str | None,
    subject_recovery: SubjectRecoveryAuthority,
) -> list[Action]:
    """Actions for a session the agent reported as blocked.

    Only an ``issue-`` session's block maps onto issue state. Review and rework
    BLOCKED completions do not map to issue-blocking labels; their parent
    workflows own any PR/review state transitions.

    Reachable for a bounded tech_lead role, which is why the question is asked
    here at all (#182): every tech_lead flavor runs in an ``issue-`` terminal,
    and the tech_lead prompt itself instructs the agent to report a missing
    workspace via ``coding-done blocked``. Blocking the subject that way is the
    state a planning run's admission required to be ABSENT — so the label is the
    owner's call, while the reason, the operator's comment, and the claim
    release are unconditional.
    """
    if not session.terminal_id.startswith("issue-"):
        return []
    reason_text = blocked_reason.strip() if blocked_reason else "No reason provided."
    label = blocked_label or label_manager.blocked
    block = subject_recovery.recovery_label_outcome(
        add_label=AddLabelAction(
            issue_number=session.issue.number,
            label=label,
            reason="Agent reported issue as blocked",
            expected=expected,
        ),
        note_when_added=(
            f"This issue has been marked as `{label}` and will not be"
            " automatically retried.\nRemove the label to allow reprocessing."
        ),
    )
    return [
        *block.label_actions,
        AddCommentAction(
            number=session.issue.number,
            comment=(
                "🚧 **Session Blocked**\n\n"
                "The agent reported this issue as blocked.\n\n"
                f"**Reason:** {reason_text}\n"
                f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                f"- Session: `{session.terminal_id}`\n\n"
                f"{block.note}"
            ),
            reason="Notify about blocked session and reason",
            expected=expected,
        ),
        RemoveLabelAction(
            issue_number=session.issue.number,
            label=label_manager.in_progress,
            reason="Session blocked - releasing claim",
            expected=expected,
        ),
    ]
