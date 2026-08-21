"""What a FAILED PUBLISH means for the issue whose work could not land (#182).

The fourth completion-effect owner beside :mod:`provider_blocked_completion`,
:mod:`agent_blocked_completion`, and :mod:`invalid_record_actions`: those three
plan what a session that could not FINISH produces, this one plans what a
session that finished and could not PUBLISH produces — the agent did the work,
and the orchestrator could not push it or open its PR.

Extracted from :mod:`completion_action_planner` for the reason its siblings
were, and with more cause than any of them: this path carries a three-arm
verdict, a consecutive-failure counter, an escalation threshold, and a
suppression rule, and all of it sat among a dozen other completion-status
branches. The planner keeps the ROUTING decision — tech_lead decision rejection
versus generic publish failure — and delegates the effects here.

It is also the fifth door onto a SUBJECT's recovery state (#182 review F1), and
the only one a run reaches by SUCCEEDING at its own job:
``shape_requested_actions_for_tech_lead`` deliberately keeps ``PUSH_BRANCH`` and
``CREATE_PR``, so a focused tech_lead run publishes onto its disposable branch,
and a failed push lands ``publish-failed`` — or past the counter,
``needs-human`` — on ``issue-{N}``, which for a focused flavor IS the subject.
The threaded :class:`~.subject_recovery_authority.SubjectRecoveryAuthority` is
what decides whether this run's role may make that change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain.models import Session
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .label_manager import LabelManager
from .needs_human_block import NeedsHumanCause
from .reconciliation import ExpectedState
from .subject_recovery_authority import SubjectRecoveryAuthority

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PublishFailureVerdict:
    """What a failed publish does to its subject: the effects AND the words.

    One value because they are one decision (#182 review F1). A suppressed
    escalation announced as an escalation, or a counter rolled by a comment
    telling the operator the issue was left alone, is precisely the drift
    :mod:`.subject_recovery_authority` exists to prevent — so the arm that
    decides the label actions is the arm that writes the comment describing
    them, and nothing downstream re-derives either.
    """

    label_actions: tuple[Action, ...]
    comment: str
    comment_reason: str


# The publish-failure diagnosis every arm opens with. The escalating arm
# replaces it with the consecutive-failure count, which only it has earned the
# right to state.
_PUBLISH_FAILURE_INTRO = (
    "The agent completed its work, but the orchestrator could not push or"
    " create a PR."
)


def publish_failure_actions(
    session: Session,
    expected: ExpectedState,
    *,
    critical_errors: list[str],
    diagnostic_path: str | None,
    label_manager: LabelManager,
    max_consecutive_publish_failures: int,
    subject_recovery: SubjectRecoveryAuthority,
) -> list[Action]:
    """Actions for a session whose completed work could not be published.

    Tracks consecutive publish failures via publish-fail-count-N labels. After
    ``max_consecutive_publish_failures``, escalates to needs-human.

    ``subject_recovery`` is the caller-threaded answer to "may this run change
    its own subject's recovery state?" (#182). Like every other generic
    completion path, this module never learns whose session it is, so it
    receives the ANSWER rather than the role. It is a REQUIRED argument because
    a default would silently re-open the door for any future caller that forgot
    it. What survives a suppression is the diagnosis and the released claim: the
    operator still learns that publishing failed.
    """
    issue_number = session.issue.number
    first_error = critical_errors[0][:100] if critical_errors else "Unknown error"
    if len(first_error) == 100:
        first_error += "..."

    diagnostic_info = ""
    if diagnostic_path and session.worktree_path:
        worktree_name = Path(session.worktree_path).name
        diagnostic_info = f"\n**Diagnostic file:** `{worktree_name}/{diagnostic_path}`\n"

    verdict = _publish_failure_verdict(
        session,
        expected,
        labels=label_manager,
        max_failures=max_consecutive_publish_failures,
        subject_recovery=subject_recovery,
        first_error=first_error,
        diagnostic_info=diagnostic_info,
    )
    return [
        *verdict.label_actions,
        AddCommentAction(
            number=issue_number,
            comment=verdict.comment,
            reason=verdict.comment_reason,
            expected=expected,
        ),
        RemoveLabelAction(
            issue_number=issue_number,
            label=label_manager.in_progress,
            reason="Publishing failed - releasing claim",
            expected=expected,
        ),
    ]


def _publish_failure_verdict(
    session: Session,
    expected: ExpectedState,
    *,
    labels: LabelManager,
    max_failures: int,
    subject_recovery: SubjectRecoveryAuthority,
    first_error: str,
    diagnostic_info: str,
) -> _PublishFailureVerdict:
    """Escalate, mark, or leave the subject untouched — one decision (#182 F1).

    A run that holds no recovery authority over its subject does not merely lose
    the blocking label: it never reads the publish counter, let alone rolls it.
    That counter is the SUBJECT's publish history, and a bounded role adding to
    it would hasten a LATER escalation to ``needs-human`` — achieving through a
    successor exactly the recovery action its capability row forbids it from
    proposing. Dropping the whole mutation is also what the owner's suppression
    note already promises an operator: that the issue is left exactly as it was.

    The three arms are mutually exclusive and each writes its own comment, so an
    escalation that did not happen can never be announced as one.
    """
    if not subject_recovery.may_leave_recovery_label:
        return _PublishFailureVerdict(
            label_actions=(),
            comment=_publish_failure_comment(
                headline="❌ **Publishing Failed**",
                intro=_PUBLISH_FAILURE_INTRO,
                error_label="Error",
                first_error=first_error,
                diagnostic_info=diagnostic_info,
                session=session,
                note=subject_recovery.suppression_note(
                    labels.publish_failed, labels.needs_human
                ),
            ),
            comment_reason="Report the publish failure without blocking its subject",
        )

    issue_number = session.issue.number
    # Count previous consecutive publish failures from issue labels.
    prev_count = labels.extract_publish_fail_count(session.issue.labels)
    new_count = prev_count + 1

    if new_count >= max_failures:
        logger.info(
            "[COMPLETION] Publish failure count %d >= max %d, escalating to needs-human: issue=%d",
            new_count,
            max_failures,
            issue_number,
        )
        return _PublishFailureVerdict(
            label_actions=(
                AddLabelAction(
                    issue_number=issue_number,
                    label=labels.needs_human,
                    reason=f"Publishing failed {new_count} consecutive times — escalating to needs-human",
                    needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=labels.needs_rework,
                    reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                    expected=expected,
                ),
            ),
            comment=_publish_failure_comment(
                headline="❌ **Publishing Failed — Escalated**",
                intro=(
                    f"Publishing has failed **{new_count} consecutive times** "
                    f"(max: {max_failures})."
                ),
                error_label="Latest error",
                first_error=first_error,
                diagnostic_info=diagnostic_info,
                session=session,
                note=(
                    f"This issue has been marked as `{labels.needs_human}`"
                    " and needs human investigation.\nRemove the label after"
                    " investigating to allow reprocessing."
                ),
            ),
            comment_reason="Escalate repeated publish failure to human",
        )

    label_actions: list[Action] = [
        AddLabelAction(
            issue_number=issue_number,
            label=labels.publish_failed,
            reason="Publishing failed after agent completion (push/PR creation failed)",
            expected=expected,
        ),
        RemoveLabelAction(
            issue_number=issue_number,
            label=labels.needs_rework,
            reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
            expected=expected,
        ),
    ]
    if prev_count > 0:
        label_actions.append(
            RemoveLabelAction(
                issue_number=issue_number,
                label=labels.publish_fail_count_label(prev_count),
                reason="Updating publish failure count",
                expected=expected,
            )
        )
    label_actions.append(
        AddLabelAction(
            issue_number=issue_number,
            label=labels.publish_fail_count_label(new_count),
            reason=f"Publish failure #{new_count}",
            expected=expected,
        )
    )
    return _PublishFailureVerdict(
        label_actions=tuple(label_actions),
        comment=_publish_failure_comment(
            headline=f"❌ **Publishing Failed** (attempt {new_count}/{max_failures})",
            intro=_PUBLISH_FAILURE_INTRO,
            error_label="Error",
            first_error=first_error,
            diagnostic_info=diagnostic_info,
            session=session,
            note=(
                f"This issue has been marked as `{labels.publish_failed}`"
                " and will not be automatically retried.\nRemove the label"
                " to retry."
            ),
        ),
        comment_reason="Notify about processing failure",
    )


def _publish_failure_comment(
    *,
    headline: str,
    intro: str,
    error_label: str,
    first_error: str,
    diagnostic_info: str,
    session: Session,
    note: str,
) -> str:
    """One shape for all three publish-failure comments (#182 review F1).

    The arms differ in their headline, their opening line, and their closing
    note; the diagnosis in between is the same facts about the same failure, so
    it is written once. The note always lands last, which is where every other
    recovery-state path puts the sentence about the label.
    """
    return (
        f"{headline}\n\n"
        f"{intro}\n\n"
        f"**{error_label}:** {first_error}\n"
        f"{diagnostic_info}\n"
        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
        f"- Session: `{session.terminal_id}`\n"
        f"\n{note}"
    )
