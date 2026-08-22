"""Apply-time owner for handing an abandoned candidate back (#195).

The release is up to four ordered steps against three different collaborators
— the label writer, the comment writer, and the session-history owner — and the
ORDER is the policy: it is what stops the issue being handed back while a label
still says a session owns it, and what stops an escalation being announced
before it has happened. Keeping that sequence in one boundary is the same move
:mod:`.history_reconciliation` makes for terminal reconciliation, and the same
one :mod:`.stale_cleanup_planning` makes for the plan-time half of this
decision.

The label and comment halves are delegated back to the applier's ordinary
add/remove/comment paths rather than reimplemented here, so reconciliation
gating, claim verification, needs-human cause bookkeeping, label-store
write-through and mutation stats all stay in the one place that owns them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeAlias

from ..infra.logging_config import issue_log
from .action_results import ActionResult
from .actions import Action, ReleaseAbandonedIssueAction
from .session_history import SessionHistoryOwner

logger = logging.getLogger(__name__)

ActionApplyStep: TypeAlias = Callable[[Action], ActionResult]
"""One of the applier's ordinary write paths, injected rather than reimplemented."""


def apply_release_abandoned_issue(
    action: ReleaseAbandonedIssueAction,
    *,
    history_owner: SessionHistoryOwner | None,
    add_label: ActionApplyStep,
    remove_label: ActionApplyStep,
    add_comment: ActionApplyStep,
) -> ActionResult:
    """Give an abandoned candidate back to scheduling, in order.

    Up to four steps, ordered like ``RECOVER_TERMINAL_ISSUE`` and each gated on
    the one before:

    1. plant the escalation label, when this run has spent its release budget
       for the issue. First, because it is what the issue is being handed back
       TO — a release that landed without it would leave the issue considerable
       with nothing in the system refusing it;
    2. shed the stale ``in-progress`` label;
    3. release this run's duplicate-launch claim;
    4. announce the escalation, when there was one.

    Releasing before either label would hand the issue back while a label the
    write failed to change still says a session owns it. Failing the whole
    command instead leaves the issue exactly as it was, still wearing
    ``in-progress``, so the next tick sees the same stale label and plans the
    same command again.

    The claim half goes through the history owner, which keeps the operator's
    record of the failed session intact — a record is not a claim.
    """
    if history_owner is None:
        return ActionResult.fail(action, "Session history owner is not configured")

    if (escalation := action.escalation()) is not None:
        escalated = add_label(escalation)
        if not escalated.success:
            return ActionResult.fail(
                action,
                escalated.error or "release-budget escalation label add failed",
            )

    removal = remove_label(action.label_removal())
    if not removal.success:
        return ActionResult.fail(
            action,
            removal.error or "stale in-progress label removal failed",
        )

    release = history_owner.release_claim(action.issue_number)
    return ActionResult.ok(
        action,
        issue_number=action.issue_number,
        label=action.label,
        released_entries=release.released_entries,
        releases_granted=release.releases_granted,
        escalation_label=action.escalation_label,
        escalation_announced=_announce_escalation(action, add_comment=add_comment),
    )


def _announce_escalation(
    action: ReleaseAbandonedIssueAction,
    *,
    add_comment: ActionApplyStep,
) -> bool:
    """Post the escalation explanation, if this release carried one.

    Last, and the one step that does NOT fail the command: by the time it runs,
    the escalation it describes has already happened, so reporting the release
    as failed would be untrue and would read as "the issue is still
    relaunchable" to anything auditing the result. A failed post is reported as
    ``escalation_announced=False`` and logged, and it is not retried — the same
    release retired the claim that made the issue an abandoned candidate, so
    the next tick has nothing to re-plan. The label, which is what actually
    refuses the next launch, landed in step 1 regardless.

    Returns whether an announcement was posted; ``False`` both for a release
    with nothing to announce and for one whose post failed, which the error log
    distinguishes.
    """
    announcement = action.escalation_announcement()
    if announcement is None:
        return False

    posted = add_comment(announcement)
    if not posted.success:
        logger.error(
            issue_log(
                action.issue_number,
                "Released after spending the abandoned-release budget, but the "
                "escalation comment could not be posted: %s. The %s label is "
                "applied and still refuses the next launch.",
            ),
            posted.error,
            action.escalation_label,
        )
    return posted.success
