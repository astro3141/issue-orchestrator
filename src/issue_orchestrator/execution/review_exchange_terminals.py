"""The terminals a reviewer round can reach, and what each one records.

A reviewer round ends the exchange or it does not, and there are exactly three
ways it ends:

=====================================  ======================================
the reviewer approves                  ``OK`` / ``REVIEWER_OK``
the caller owns no coder (#180)        ``STOPPED`` / ``REVIEWER_REQUESTED_CHANGES``
the coder stopped getting closer       ``STOPPED`` / ``REVIEWER_REPORTS_NO_PROGRESS``
=====================================  ======================================

Both halves of that live here, together and on purpose.

:class:`ReviewerRoundTerminals` decides *which* terminal, if any, a round
reaches. It owns the no-progress counter rather than reading one, because that
counter is the single piece of state the decision carries across rounds: a
round loop that incremented it in one place and consulted it in another is how
"which rounds counted" drifts from "which rounds stopped the exchange". It also
means the round loop asks one question instead of testing three conditions in
an order it has to get right.

:func:`complete_with_reviewer_decision` closes the exchange at whichever
terminal was decided. The three differ only in ``status``/``reason``, and they
used to be separate functions with one body — which is how a durable authority
record can end up written at one terminal and not another. Binding the
Foundation records (#15, #34) here gives every terminal one derivation site by
construction, and is what let #180's handoff arrive with no new authority type:
it reaches the same producer with the same presented commit, and the only thing
that differs is which verdict it hands over.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.review_artifacts import ReviewDecision
from ..domain.review_exchange import ReviewExchangeOutcome, ReviewExchangeResponse
from ..domain.review_exchange_rework import ReviewExchangeRework
from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..domain.review_exchange_summary import ReviewExchangeReason, ReviewExchangeStatus
from ..domain.review_verdict_binding import ReviewVerdictOutcome
from ..events import EventName
from ..ports import (
    TraceEvent,
    make_review_exchange_completed_event,
    make_review_exchange_round_completed_event,
)
from .candidate_execution_identity import CandidateExecutionIdentityRecorder
from .review_exchange_records import bind_review_verdict, write_exchange_summary

ReviewerTerminal = tuple[ReviewExchangeStatus, ReviewExchangeReason]
"""Which terminal a reviewer round reached, as the summary spells it."""

EmitEvent = Callable[[EventName, dict[str, Any]], None]


class ReviewerRoundTerminals:
    """Decides whether a reviewer round ends the exchange, and how.

    Constructed once per exchange with the two policies that can end one
    without an approval: whether the caller owns a coder to rework with, and
    how many rounds of "not getting closer" it will sit through.
    """

    def __init__(
        self, *, rework: ReviewExchangeRework, max_no_progress: int
    ) -> None:
        self._rework = rework
        self._max_no_progress = max_no_progress
        self._no_progress_count = 0

    def for_round(self, reviewer: ReviewExchangeResponse) -> ReviewerTerminal | None:
        """The terminal ``reviewer`` reached, or ``None`` to run a coder turn.

        Order is the policy. The approval is checked first because it is the
        one answer no other policy may override. #180's handoff comes next and
        is unconditional: a caller with no coder has nothing to hand the
        feedback to, so there is no round at which it would be right to
        continue — and it therefore precedes the no-progress budget, which
        measures whether successive CODER turns are getting closer and has
        nothing to measure in an exchange that runs none.
        """
        if reviewer.response_type == "ok":
            return (ReviewExchangeStatus.OK, ReviewExchangeReason.REVIEWER_OK)
        if not self._rework.runs_coder_rounds:
            return (
                ReviewExchangeStatus.STOPPED,
                ReviewExchangeReason.REVIEWER_REQUESTED_CHANGES,
            )
        if reviewer.getting_closer is False:
            self._no_progress_count += 1
        else:
            self._no_progress_count = 0
        if 0 < self._max_no_progress <= self._no_progress_count:
            return (
                ReviewExchangeStatus.STOPPED,
                ReviewExchangeReason.REVIEWER_REPORTS_NO_PROGRESS,
            )
        return None


def complete_with_reviewer_decision(
    *,
    run_assets: ReviewExchangeRunAssets,
    exchange_dir: Path,
    terminal: ReviewerTerminal,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    decision: ReviewDecision,
    verdict: ReviewVerdictOutcome,
    review_artifacts: list[dict[str, str]],
    emit: EmitEvent,
    issue_number: int,
    session_name: str,
    validation_record_path: Path,
    presented_head_sha: str | None,
    execution_identities: CandidateExecutionIdentityRecorder,
) -> ReviewExchangeOutcome:
    """Close the exchange at the terminal the reviewer decided."""
    status, reason = terminal
    summary = write_exchange_summary(
        exchange_dir, round_index,
        status=status,
        reason=reason,
        reviewer_response=reviewer,
        review_artifacts=review_artifacts,
        validation_record_path=validation_record_path,
    )
    # The orchestrator's single derivation, passed in rather than assumed
    # here: binding states what it concluded, and never promotes the
    # reviewer's own claim to authority.
    bind_review_verdict(
        exchange_dir=exchange_dir,
        verdict=verdict,
        presented_head_sha=presented_head_sha,
        completed_rounds=round_index,
    )
    # §4's other half, bound to the same observation and therefore to the same
    # commit: who executed this candidate, as the orchestrator launched them.
    execution_identities.record(presented_head_sha)
    _emit(emit, make_review_exchange_round_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer.response_type,
        "reviewer_response_text": reviewer.response_text,
        "review_decision_verdict": decision.verdict,
        "review_nit_policy": decision.nit_policy,
        "review_abstraction_status": decision.abstraction_review.status,
        "artifacts": review_artifacts,
        "coder_response_type": None,
    }))
    _emit(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": status.value,
        "reason": reason.value,
        "review_decision_verdict": decision.verdict,
        "review_nit_policy": decision.nit_policy,
        "review_abstraction_status": decision.abstraction_review.status,
        "artifacts": review_artifacts,
    }))
    return ReviewExchangeOutcome(
        status=status,
        rounds=round_index,
        reason=reason,
        run_assets=run_assets,
        reviewer_response=reviewer,
        summary=summary,
    )


def _emit(emit: EmitEvent, event: TraceEvent) -> None:
    emit(event.event_type, event.data)


__all__ = [
    "ReviewerRoundTerminals",
    "ReviewerTerminal",
    "complete_with_reviewer_decision",
]
