"""Which phase of the control continuation one exact candidate is in (#149).

Liveness is *derived*, never stored. There is no continuation state machine
whose transitions someone must remember to write, because such a machine would
be a second scheduler truth: durable facts and the machine could disagree, and
the machine would win. Instead, every phase below is a predicate over facts
four existing owners already keep durably:

===============================  =============================================
the recorded intent              ``Attempt.continuation_descriptor`` (#143)
the publication evaluations      ``Attempt.completed_evaluations`` (#85, #139)
the same-SHA allowance           ``Attempt.revalidation_budget_used`` (#139)
the exact-``A`` review outcome   ``Attempt.continuation_review_verdict``
the board                        labels the tick already fetched
===============================  =============================================

Two phases are load-bearing and were got wrong by the first reading of #148,
which is why they are spelled out here rather than left implicit:

* ``PASS_PENDING_REVIEW`` exists because "latest publication evaluation is
  non-PASS" is NOT the liveness predicate. Were it, recording ``PASS(A)`` would
  drop the operation and release its ownership at the exact moment the
  continuation is about to begin — before any reviewer has seen ``A``.
* ``EXIT_TO_REWORK`` is decided by ``CHANGES_REQUESTED(A)`` and not by the
  branch head reaching ``A'``. Ownership excludes ordinary rework, so waiting
  for ``A'`` would wait for work that ownership itself is preventing. The
  verdict is the transfer fact; ``A'`` is its consequence.

``SETTLED_NO_PR`` closes the third: an ``APPROVED(A)`` whose recorded intent
never asked for a pull request must settle, not wait forever for a
``pr-pending`` that by contract will never appear.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .continuation_descriptor import ContinuationDescriptor
from .review_verdict_binding import ReviewVerdictOutcome


class ContinuationPhase(Enum):
    """Where one exact candidate sits in the control continuation.

    ``live`` is a property of the member rather than a set held beside the
    enum, so adding a phase forces its author to say whether it holds the
    issue. A phase with no answer does not compile.
    """

    #: No recorded intent for this candidate, so there is no continuation to
    #: run. The absence is refusal, never a permissive default.
    NO_RECORDED_INTENT = ("no_recorded_intent", False)
    #: Intent recorded, but the publication contract has never reported on this
    #: candidate. Nothing to retry and nothing to continue from.
    NOT_EVALUATED = ("not_evaluated", False)
    #: Non-PASS with the #139 allowance still unspent: one same-SHA
    #: revalidation may be admitted (by #139, never by this predicate).
    RETRY_PENDING = ("retry_pending", True)
    #: ``PASS(A)`` recorded and no exact-``A`` review has settled. The
    #: continuation keeps the operation live *through* the PASS.
    PASS_PENDING_REVIEW = ("pass_pending_review", True)
    #: ``APPROVED(A)`` and the recorded intent asks for a pull request, which
    #: the board does not yet show.
    APPROVED_PENDING_PR = ("approved_pending_pr", True)
    #: ``CHANGES_REQUESTED(A)``: the control continuation hands off to ordinary
    #: rework. Not live, so ownership is released before rework is evaluated.
    EXIT_TO_REWORK = ("exit_to_rework", False)
    #: ``APPROVED(A)`` with no ``CREATE_PR`` in the recorded intent: terminal.
    SETTLED_NO_PR = ("settled_no_pr", False)
    #: The board carries ``pr-pending``: the continuation reached a PR.
    SETTLED_PR = ("settled_pr", False)
    #: Non-PASS with the allowance spent. No second revalidation exists, so the
    #: candidate returns to ordinary rework with its evidence history intact.
    EXHAUSTED = ("exhausted", False)

    def __init__(self, value: str, live: bool) -> None:
        self._value_ = value
        self._live = live

    @property
    def live(self) -> bool:
        """Whether an operation in this phase holds its issue against ordinary work."""
        return self._live


@dataclass(frozen=True, slots=True)
class ContinuationFacts:
    """The durable facts one phase decision is made from.

    A value object rather than four arguments, because the point of this seam
    is that the decision is made from *these* facts and nothing else. A caller
    holding a board snapshot, an attempt and a label set has to reduce them to
    this shape first, and anything it could not reduce — an issue title, a log
    line, a diagnostic — has nowhere to go.
    """

    descriptor: ContinuationDescriptor | None
    has_publication_evaluation: bool
    latest_publication_passed: bool
    revalidation_allowance_available: bool
    review_verdict: ReviewVerdictOutcome | None
    board_shows_pr_pending: bool


def derive_continuation_phase(facts: ContinuationFacts) -> ContinuationPhase:
    """The phase ``facts`` state, decided in one place and in one order.

    Order matters and is the policy: settlement is checked before continuation,
    so a candidate that already reached a pull request or a terminal review
    verdict can never be re-derived as live and re-run. Nothing below reads a
    lease row, a label other than ``pr-pending``, a session, or a terminal.
    """
    descriptor = facts.descriptor
    if descriptor is None:
        # The one refusal that outranks every other fact. A PASS, an approval
        # and a clean board still do not make a continuation, because no agent
        # ever said what should happen to this candidate.
        return ContinuationPhase.NO_RECORDED_INTENT
    if facts.board_shows_pr_pending:
        # Checked before the review verdict: a PR that exists is the settlement
        # the whole continuation was for, whatever the ownership store still
        # says. This is the crash window "PR created, release incomplete".
        return ContinuationPhase.SETTLED_PR
    if facts.review_verdict is ReviewVerdictOutcome.CHANGES_REQUESTED:
        return ContinuationPhase.EXIT_TO_REWORK
    if facts.review_verdict is ReviewVerdictOutcome.APPROVED:
        return (
            ContinuationPhase.APPROVED_PENDING_PR
            if descriptor.creates_pr
            else ContinuationPhase.SETTLED_NO_PR
        )
    if not facts.has_publication_evaluation:
        return ContinuationPhase.NOT_EVALUATED
    if facts.latest_publication_passed:
        return ContinuationPhase.PASS_PENDING_REVIEW
    return (
        ContinuationPhase.RETRY_PENDING
        if facts.revalidation_allowance_available
        else ContinuationPhase.EXHAUSTED
    )


__all__ = [
    "ContinuationFacts",
    "ContinuationPhase",
    "derive_continuation_phase",
]
