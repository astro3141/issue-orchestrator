"""What to plan for an issue whose ``in-progress`` label went stale.

Three outcomes, decided in one place so the rule cannot be enforced differently
by path:

- an ordinary stale label -> the plain removal the planner has always planned;
- an ABANDONED candidate within this run's release budget -> the release
  command, which sheds the label and hands the duplicate-launch claim back so
  the next legitimate attempt can start (#195);
- an abandoned candidate whose budget is spent -> the same release, carrying a
  ``needs-human`` escalation and its explanation with it.

The third arm is what keeps the second from being an unbounded relaunch loop.
The release retires ``session_history_issue_numbers``, which is the only member
of the planner's launch filter that can hold a ``validation_failed`` issue, and
no other budget in the system counts fresh coding launches. So the ceiling has
to plant something the scheduler refuses, and it plants the same blocking label
``max_consecutive_publish_failures`` does -- an escalation an operator can see
and clear, rather than an issue that quietly stops being retried.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from .abandoned_candidates import AbandonedCandidate, AbandonedCandidates
from .actions import Action, ReleaseAbandonedIssueAction, RemoveLabelAction
from .reconciliation import build_expected_for_mutation

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

_ORDINARY_STALE_REASON = "stale - no running session"
_RELEASE_REASON = "abandoned after completion - no owner, no running session"


def plan_stale_in_progress_actions(
    *,
    stale_issues: Sequence["Issue"],
    abandoned: AbandonedCandidates,
    labels: "LabelManager",
) -> list[Action]:
    """Plan the label (and, for abandoned candidates, release) actions."""
    actions: list[Action] = []
    for issue in stale_issues:
        candidate = abandoned.verdict(issue.number)
        if candidate is None:
            actions.append(RemoveLabelAction(
                issue_number=issue.number,
                label=labels.in_progress,
                reason=_ORDINARY_STALE_REASON,
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            logger.info(
                "Planner: removing stale in-progress label from issue #%d", issue.number
            )
            continue
        actions.extend(_release_actions(candidate, labels=labels))
    return actions


def _release_actions(
    candidate: AbandonedCandidate,
    *,
    labels: "LabelManager",
) -> list[Action]:
    """The release, plus the escalation when this run has spent its budget."""
    issue = candidate.issue
    if not candidate.exhausted:
        logger.info(
            "Planner: releasing abandoned issue #%d (stale in-progress label and no "
            "owner; %d of %d automatic attempts already granted)",
            issue.number,
            candidate.releases_granted,
            candidate.max_releases,
        )
        return [ReleaseAbandonedIssueAction(
            issue_number=issue.number,
            label=labels.in_progress,
            reason=_RELEASE_REASON,
            expected=build_expected_for_mutation(),
            issue_key=issue.key.stable_id(),
        )]

    logger.warning(
        "Planner: abandoned issue #%d has spent this run's release budget "
        "(%d of %d) - escalating to %s instead of relaunching",
        issue.number,
        candidate.releases_granted,
        candidate.max_releases,
        labels.needs_human,
    )
    return [
        # The escalation label AND its explanation travel INSIDE the release
        # command so the applier can order them: blocking label first, stale
        # label second, claim third, announcement last. Planned as siblings
        # instead, either could be applied after a release that failed — the
        # label leaving a window where the issue is considerable and nothing
        # refuses it, the comment announcing an escalation that did not happen
        # and re-posting it every tick until it did.
        ReleaseAbandonedIssueAction(
            issue_number=issue.number,
            label=labels.in_progress,
            reason=_RELEASE_REASON,
            expected=build_expected_for_mutation(),
            issue_key=issue.key.stable_id(),
            escalation_label=labels.needs_human,
            escalation_reason=(
                f"{candidate.releases_granted} abandoned attempt(s) already relaunched "
                f"this run (max: {candidate.max_releases}) - escalating to human"
            ),
            escalation_comment=_escalation_comment(candidate, labels=labels),
        ),
    ]


def _escalation_comment(
    candidate: AbandonedCandidate,
    *,
    labels: "LabelManager",
) -> str:
    return (
        "⚠️ **Validation keeps failing — escalated**\n\n"
        f"This run has already relaunched this issue **{candidate.releases_granted} "
        f"time(s)** after its validation gate refused the work "
        f"(max: {candidate.max_releases}), and the latest attempt was refused again.\n\n"
        f"The issue has been marked `{labels.needs_human}` instead of being "
        "relaunched. Remove the label after investigating to allow reprocessing, "
        "or raise `retry.max_abandoned_releases` if more automatic attempts are "
        "wanted.\n\n"
        "The failed sessions stay in this run's history for diagnosis."
    )
