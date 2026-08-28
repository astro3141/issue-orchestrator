"""What a finished run with NOTHING TO MERGE means for its issue (#337).

The completion-effect owner beside :mod:`publish_failure_completion` and
:mod:`agent_blocked_completion`: those plan what a session that could not
publish or could not finish produces; this one plans what a session that
finished successfully — and has no pull request to hand its issue to —
produces.

An ordinary success does not end at the completion. It hands the issue to
``pr-pending``, which is precisely why the completion planner may RELEASE the
``in-progress`` claim label, and the merge of the pull request whose body says
``Closes #N`` is what eventually closes the issue. Both halves of that chain
depend on a pull request existing.

A run the completion settlement PROVED offers no code candidate has none. No
branch is pushed, no pull request is opened, so no ``pr-pending`` is ever
stamped and no merge will ever arrive. The claim label is released and nothing
takes its place, which leaves an OPEN issue carrying no lifecycle label at all
— indistinguishable to :class:`~.scheduler.Scheduler` from work never started.
The next tick selects it, a new agent session runs the same measurement, the
same review exchange runs, and a second RESULT is posted; every tick,
unbounded. The behaviour this replaced was a bounded publish FAILURE, so
without a terminal disposition the repair would have traded a bounded stop for
an unbounded repeat.

So the close is planned here, from the settlement the publication owner
carried, and never from the ABSENCE of a ``pr_url``: a publish that failed is
also missing one, and that run needs the bounded publish-failure routing, not a
terminal close.
"""

from __future__ import annotations

import logging

from ..domain.models import Session
from .actions import Action, CloseIssueAction
from .completion_types import ResultOnlyDelivery
from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

CLOSE_COMMENT = (
    "Closed by issue-orchestrator: this run published its result as the comment"
    " above and produced no code to merge, so there is no pull request whose"
    " merge would close this issue. Reopen it if the work is not finished."
)
"""Why an issue closed with no pull request — the one fact its own timeline lacks.

The RESULT comment the run published is already on the issue by the time this
close applies; it is what ``post_comment`` delivered. What is NOT anywhere on
the issue is the reason no pull request accompanies it, and an operator reading
a closed evidence-only issue has no other source for it.
"""


def result_only_terminal_actions(
    session: Session,
    expected: ExpectedState,
    result_only: ResultOnlyDelivery,
    *,
    pull_request_url: str | None,
) -> list[Action]:
    """The terminal disposition of a run whose comment is its whole delivery.

    Returns nothing for every completion no owner settled this way — the
    ordinary PR-carried lifecycle, which is also the fail-safe direction: an
    unsettled run keeps exactly today's behaviour.

    ``pull_request_url`` is a SECOND, independent condition, and it is here
    because the settlement's five facts answer "did this RUN produce code?"
    and not "does this ISSUE have work in flight" (#337 round 2, N4). The two
    come apart in a shape this repository has actually seen: a rework worktree
    that arrives reset to the base, its pull request's commits reachable only
    from the remote branch. Such a run has a clean tree, sits at the base, adds
    no commit, and would satisfy every fact the lane proves — and closing its
    issue would close one whose pull request is open and unmerged.

    A genuine evidence run has no pull request; that is the whole premise of
    the lane. So the presence of one is enough to refuse, and it costs nothing
    to ask: the completion handler has already fetched it for this issue. It is
    a guard rather than the proof — an unreadable PR lookup yields ``None`` and
    falls back to the settlement — which is why it sits beside the settlement
    instead of replacing any part of it.

    The caller orders this BEFORE the release of the claim label, so a partial
    apply fails safe: a closed issue is out of selection whatever becomes of
    its labels afterwards, whereas a released-but-unclosed one is exactly the
    unbounded relaunch this module exists to prevent.
    """
    if not result_only.delivered:
        return []
    if pull_request_url:
        logger.warning(
            "[COMPLETION] Result-only delivery for issue #%d, but pull request"
            " %s exists for it; refusing the terminal close (%s)",
            session.issue.number,
            pull_request_url,
            result_only.detail,
        )
        return []
    logger.info(
        "[COMPLETION] Result-only delivery - closing issue #%d: %s",
        session.issue.number,
        result_only.detail,
    )
    return [
        CloseIssueAction(
            issue_number=session.issue.number,
            comment=CLOSE_COMMENT,
            reason="Completed run delivered its result with no code to merge",
            expected=expected,
        )
    ]


__all__ = ["CLOSE_COMMENT", "result_only_terminal_actions"]
