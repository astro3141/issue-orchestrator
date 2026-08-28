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
from dataclasses import dataclass

from ..domain.models import Session
from .actions import Action, CloseIssueAction
from .completion_gate_narrative import GateFailureNarrative
from .completion_types import ResultOnlyDelivery
from .pull_request_observation import PullRequestObservation
from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultOnlyCloseIssueAction(CloseIssueAction):
    """The terminal close of a result-only run — and the GATE for its release.

    A distinct type rather than a plain :class:`CloseIssueAction` because the
    completion applier must be able to tell this one close from every other
    (#337 round 3, F1). Ordering the close before the release of ``in-progress``
    is not a fail-stop boundary: ``ActionApplier`` catches an ordinary close
    error and reports a FAILED result, then applies the rest of the batch — so
    a failed close followed by a successful release leaves exactly the state
    the ordering was meant to prevent, an OPEN issue with its execution claim
    given up, which the scheduler cannot tell from work never started.

    Marked as a completion GATE (``completion_effect_gate``), the release is
    withheld unless this close COMMITS, so a failed close leaves the issue open
    and still carrying its claim label instead of open and released. That alone
    is not the boundary: ``Scheduler`` blocks ``in-progress`` only while an
    ACTIVE session also exists, and this session has just terminalized, so the
    label is stale and the planner's ordinary stale cleanup removes it (#337
    round 4, F1-R). What makes the state durable is the BLOCKING label the gate
    failure's surface plants — see
    :data:`FAILED_RESULT_ONLY_CLOSE_NARRATIVE`.

    It carries no fields of its own; ``ActionApplier`` dispatches it on the
    inherited ``ActionType.CLOSE_ISSUE`` and applies it exactly like any other
    close. What differs is not HOW it applies, but what may follow it.
    """


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
    pull_request: PullRequestObservation,
) -> list[Action]:
    """The terminal disposition of a run whose comment is its whole delivery.

    Returns nothing for every completion no owner settled this way — the
    ordinary PR-carried lifecycle, which is also the fail-safe direction: an
    unsettled run keeps exactly today's behaviour.

    ``pull_request`` is a SECOND, independent condition, and it is here because
    the settlement's five facts answer "did this RUN produce code?" and not
    "does this ISSUE have work in flight" (#337 round 2, N4). The two come
    apart in a shape this repository has actually seen: a rework worktree that
    arrives reset to the base, its pull request's commits reachable only from
    the remote branch. Such a run has a clean tree, sits at the base, adds no
    commit, and would satisfy every fact the lane proves — and closing its
    issue would close one whose pull request is open and unmerged.

    It is the OBSERVATION and not a url, because ``None`` is what a missing
    pull request and an unreadable lookup both leave behind, and this decision
    is exactly where the two must not be confused (#337 round 3, F2). The
    review/rework fallback reads ``get_pr(session.pr_number)`` for a session
    that is KNOWN to have a pull request; when that read raises, "I could not
    tell" arriving as "there is none" would authorise the very close the guard
    exists to refuse. Only an OBSERVED absence opens this path; both
    ``OBSERVED_PRESENT`` and ``UNKNOWN`` refuse, and the run keeps the ordinary
    lifecycle it would have had before the lane existed.

    A genuine evidence run has no pull request; that is the whole premise of
    the lane. Asking costs nothing — the completion handler has already fetched
    it for this issue — and it stays a guard beside the settlement rather than
    a part of the proof, which is why an unproven settlement still refuses
    first.

    The caller orders the returned close BEFORE the release of the claim label,
    and :mod:`.completion_effect_gate` makes that ordering binding: the close
    is a gate, so a close that FAILS withholds the release instead of letting
    it commit after it, and its failure is escalated durably by
    :data:`FAILED_RESULT_ONLY_CLOSE_NARRATIVE`.
    """
    if not result_only.delivered:
        return []
    if not pull_request.observed_absent:
        logger.warning(
            "[COMPLETION] Result-only delivery for issue #%d, but its pull"
            " request is %s (%s); refusing the terminal close (%s)",
            session.issue.number,
            pull_request.presence.value,
            pull_request.detail or "no detail recorded",
            result_only.detail,
        )
        return []
    logger.info(
        "[COMPLETION] Result-only delivery - closing issue #%d: %s",
        session.issue.number,
        result_only.detail,
    )
    return [
        ResultOnlyCloseIssueAction(
            issue_number=session.issue.number,
            comment=CLOSE_COMMENT,
            reason="Completed run delivered its result with no code to merge",
            expected=expected,
        )
    ]


FAILED_RESULT_ONLY_CLOSE_NARRATIVE = GateFailureNarrative(
    subject="result-only terminal close",
    heading="Result-Only Close Did Not Complete",
    explanation=(
        "This run delivered its result as the comment above and produced no code"
        " to merge, so the orchestrator planned to close the issue instead of"
        " handing it to `pr-pending`. The close failed at apply time. The release"
        " of `in-progress` was withheld and the session was recorded as FAILED,"
        " so no half-applied disposition committed — but the issue is still OPEN"
        " with no pull request whose merge could ever close it."
    ),
    label_because=(
        "a finished run left an open issue that nothing else will ever close,"
        " and an open issue with no blocking label is selected again on the next"
        " tick — which would re-run the same measurement and post the same"
        " result, without bound"
    ),
    remedy=(
        "Close the issue by hand once you have read the result above, or remove"
        " the label to let the issue be worked again."
    ),
)
"""What an operator reads when the terminal close of a result-only run fails.

The durable half of the F1 boundary (#337 round 4, F1-R). Withholding the
release is what stops a HALF-APPLIED disposition from committing, but it does
not by itself keep the issue out of the queue: the ``in-progress`` label left
behind is stale the moment the session terminalizes, ``Scheduler`` blocks that
label only alongside an active session, and the ordinary stale cleanup removes
it. The only thing holding the issue after that is the unreleased session-history
claim, which is process-local and is NOT one of
``ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES`` — so a restart would relaunch
the finished run.

Planting the shared blocking label is what makes the stop durable and bounded,
and it is what ``domain.models`` already says a ``failed`` history status is
expected to do: "plant a BLOCKING label, so the scheduler refuses the issue
whether or not any in-memory gate is retired". The escalation ends the way every
other :attr:`~.needs_human_block.NeedsHumanCause.SESSION_LIFECYCLE` block ends —
a human clears the label — which is the same bounded stop this lane replaced a
bounded publish failure with.
"""


__all__ = [
    "CLOSE_COMMENT",
    "FAILED_RESULT_ONLY_CLOSE_NARRATIVE",
    "ResultOnlyCloseIssueAction",
    "result_only_terminal_actions",
]
