"""The review evidence one continuation run produced, and what it permits (#178).

#180 gave this route the only fact it could not derive: WHICH run the exchange
allocated (:attr:`~.completion_types.ProcessingResult.review_exchange_run`).
Promotion of that run's :class:`~..domain.review_verdict_binding.BoundReviewVerdict`
onto the attempt has worked ever since. What did not follow is the consequence:
promotion could refuse — no exchange run, no binding, a binding about another
commit — and settlement ran anyway, because the refusals were logged and
dropped. #193 is the shape that produces: ``review_verdict = null``
recorded beside ``continuation_settlement = pull_request_opened``, a terminal
outcome asserting a review nothing can evidence.

So the promotion is a DECISION, not a side effect, and this module is its owner.
It answers one question — *may this run settle?* — and the answer is a member of
:class:`ReviewEvidence` rather than a bool, because several of the ways to
arrive at "no" are indistinguishable in a log line and each differs in what the
operator should do about it.

Two properties are the whole contract:

* **A completion that actually completed a review exchange settles only on a
  promoted exact-``A`` binding.** Missing, unlocatable, corrupt and ``A'``-bound
  all refuse. Nothing is synthesised from the pull request, the board, or the
  exchange's prose — the binding is the sole review authority, exactly as it was.
* **A completion that ran NO exchange, and says so, is unchanged.** It never had
  review evidence to hold, so requiring some would fail-close the one path this
  leaf is not about. Read off the result's own
  ``review_exchange_completed`` rather than off the absence of a run, so the one
  outcome that permits settlement without evidence cannot be reached by a result
  that claims a finished exchange.

A refusal is not a failure of the run: it leaves the recorded intent
undischarged, so the operation stays live and the next reconciliation re-enters
the pipeline. Under the pull-request collision strategies that permit it —
``new_branch`` (the default) and ``reuse_open`` — that re-entry finds and reuses
whatever pull request already exists, so a withheld settlement costs a run
rather than a duplicate; a repository configured ``pr_collision: fail`` refuses
the second publication instead, and reaches the same bound one pass later.
That retry is bounded by #149's own run allowance — every re-entry opens a run
and spends one — so a candidate whose exchange can never produce a readable
exact-``A`` binding reaches ``RUNS_EXHAUSTED`` and returns to ordinary rework
rather than looping.

**Nothing here raises.** A corrupt binding is read through a port that raises on
purpose (a corrupt authority artifact is not an absent one), and letting that
escape would be the wrong loudness in this one position: the run would stay open,
the operation would stay live carrying it, and ``engine_holds_open_run`` would
hold the candidate above the allowance bound that is supposed to end it — an
unbounded re-entry over an artifact that will not parse on the next pass either.
Caught here it becomes what it is: evidence that cannot be read, therefore no
settlement, therefore one spent allowance and the ordinary hand-back. The catch
can stay narrow because the parser it protects against answers every corrupt
shape with the same exception family
(:meth:`~..domain.review_verdict_binding.BoundReviewVerdict.from_payload`).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.review_verdict_binding import BoundReviewVerdict
    from ..ports.attempt_store import AttemptStore
    from ..ports.review_verdict_bindings import ReviewVerdictBindings
    from .completion_types import ProcessingResult
    from .continuation_live_truth import LiveContinuation

logger = logging.getLogger(__name__)


class ReviewEvidence(Enum):
    """What one continuation run's review evidence permits.

    ``settles`` is a property of the member rather than a set held beside the
    enum, for the reason :class:`~..domain.continuation_phase.ContinuationPhase`
    does the same with ``live``: a new outcome forces its author to say whether
    a run holding it may terminate. An outcome with no answer does not compile.
    """

    #: This run completed no review exchange, so it holds no review evidence and
    #: was never asked to. The pre-existing no-verdict path, unchanged.
    NO_EXCHANGE = ("no_exchange", True)
    #: The result says an exchange completed and names no run to read its
    #: binding from. Distinct from ``UNRECORDED`` because the exchange may well
    #: have bound a verdict — nothing here can find out — and distinct from
    #: ``NO_EXCHANGE`` because a completed exchange is exactly the case that
    #: owes evidence. Whoever built the result dropped the run.
    UNLOCATED = ("unlocated", False)
    #: The exchange's exact-``A`` binding was read and written onto the attempt.
    PROMOTED = ("promoted", True)
    #: The exchange ran and reached no verdict it could bind. Absent evidence is
    #: not permissive evidence.
    UNRECORDED = ("unrecorded", False)
    #: A binding exists and does not parse. Strictly worse than absent: a
    #: damaged authority artifact must never read as a clean answer.
    UNREADABLE = ("unreadable", False)
    #: The binding is evidence about another commit. Dropped rather than filed —
    #: :class:`~..domain.attempt.Attempt` would refuse it anyway — and now
    #: dropped without settling on it either.
    MISBOUND = ("misbound", False)
    #: Read, exact-``A``, and the durable write did not land. "Promoted" means
    #: the attempt carries it; a settlement on top of a failed write would
    #: record the outcome without the evidence, which is #193 by another route.
    UNPROMOTED = ("unpromoted", False)

    def __init__(self, value: str, settles: bool) -> None:
        self._value_ = value
        self._settles = settles

    @property
    def settles(self) -> bool:
        """Whether a run holding this evidence may record its settlement."""
        return self._settles


class ContinuationReviewEvidence:
    """Promotes the owning run's verdict, and says whether settlement may follow."""

    def __init__(
        self,
        *,
        attempts: "AttemptStore",
        review_verdicts: "ReviewVerdictBindings",
    ) -> None:
        self._attempts = attempts
        self._review_verdicts = review_verdicts

    def promote(
        self, operation: "LiveContinuation", result: "ProcessingResult"
    ) -> ReviewEvidence:
        """Promote this run's exact-``A`` verdict binding into durable truth.

        The binding the exchange writes lives in the run directory, inside the
        worktree the run is about to delete — durable enough for the session
        that made it, and gone before anything could read it back. Copying it
        onto the attempt is what makes ``EXIT_TO_REWORK``, ``SETTLED_NO_PR`` and
        ``APPROVED_PENDING_PR`` reconstructible after a restart, which is the
        whole of §8's review half.

        WHICH run is read off the pipeline's own result and not off the calling
        route's run (#180). The exchange allocates a run of its own — a sibling
        under the same worktree's ``sessions/``, not a directory beneath it — so
        the continuation cannot derive it, and the version that derived it
        anyway read an empty directory for every verdict ever bound: approvals
        as well as rejections, which is why ``EXIT_TO_REWORK`` was unreachable
        and a rejected candidate spent a second full continuation run before
        ``RUNS_EXHAUSTED`` released it. The pipeline names the run it allocated,
        so there is nothing left to derive.

        Returns:
            What the run's review evidence permits. Only
            :attr:`ReviewEvidence.PROMOTED` and :attr:`ReviewEvidence.NO_EXCHANGE`
            permit settlement; every other member leaves the recorded intent
            undischarged so the operation stays re-enterable.
        """
        exchange_run = result.review_exchange_run
        if exchange_run is None:
            return self._without_a_run(operation, result)
        try:
            binding = self._review_verdicts.for_exchange_run(exchange_run)
        except (OSError, ValueError) as exc:
            # Narrow on purpose: these are the ways reading a stored authority
            # artifact fails — the file, and its contents. Every corrupt shape
            # of the contents lands in the second, because the parser answers
            # all of them with ``ValueError`` and reserves nothing for one
            # field. Anything else is a defect and escapes, because a broken
            # reader must not be reported as unreadable evidence.
            logger.error(
                "[CONTINUATION] %s settles nothing: its review exchange's"
                " verdict binding could not be read: %s",
                operation.key,
                exc,
            )
            return ReviewEvidence.UNREADABLE
        if binding is None:
            logger.warning(
                "[CONTINUATION] %s settles nothing: its review exchange"
                " completed but bound no verdict",
                operation.key,
            )
            return ReviewEvidence.UNRECORDED
        if not binding.covers(operation.key.head_sha):
            logger.warning(
                "[CONTINUATION] %s settles nothing: its review exchange's"
                " verdict is bound to %s, which is evidence about other work",
                operation.key,
                binding.reviewed_sha[:12],
            )
            return ReviewEvidence.MISBOUND
        return self._record(operation, binding)

    def _without_a_run(
        self, operation: "LiveContinuation", result: "ProcessingResult"
    ) -> ReviewEvidence:
        """Decide what a result naming no exchange run permits.

        The permissive answer is stated from the POSITIVE fact and not from the
        absence: ``review_exchange_completed`` is False, so no exchange reached
        an outcome on this pass and none owed evidence. The two ways a live
        exchange leaves no run — deferral and the validation reroute — are
        non-terminal and never reach here, so today the two spellings agree.

        They agree because another module keeps them paired
        (:meth:`~.completion_types.ActionExecutionOutcome.of`). Deriving the one
        member that permits settlement without evidence from an invariant held
        somewhere else is how a fail-open gets built: it would be a defect
        there, and a settled continuation asserting an unevidenced review here.
        Asked of the result's own claim, that outcome is unspellable rather
        than merely unreachable.
        """
        if result.review_exchange_completed:
            logger.error(
                "[CONTINUATION] %s settles nothing: its completion reports a"
                " finished review exchange and names no run its verdict"
                " binding could be read from",
                operation.key,
            )
            return ReviewEvidence.UNLOCATED
        logger.info(
            "[CONTINUATION] %s ran no review exchange this run",
            operation.key,
        )
        return ReviewEvidence.NO_EXCHANGE

    def _record(
        self, operation: "LiveContinuation", binding: "BoundReviewVerdict"
    ) -> ReviewEvidence:
        """Write the validated binding onto the attempt it is evidence about.

        The attempt's OWN key, not a third spelling rebuilt from the issue and
        the operation. :class:`~.continuation_live_truth.LiveContinuation`
        already carries the record this operation is about, and the binding
        between candidate and evidence should have one spelling wherever it is
        written.
        """
        try:
            self._attempts.update(
                operation.attempt.key,
                lambda attempt: attempt.with_review_verdict(binding),
            )
        except (OSError, ValueError) as exc:
            logger.error(
                "[CONTINUATION] %s settles nothing: its exact-%s review verdict"
                " could not be made durable: %s",
                operation.key,
                binding.reviewed_sha[:12],
                exc,
            )
            return ReviewEvidence.UNPROMOTED
        logger.info(
            "[CONTINUATION] %s durable review verdict=%s",
            operation.key,
            binding.verdict.value,
        )
        return ReviewEvidence.PROMOTED


__all__ = ["ContinuationReviewEvidence", "ReviewEvidence"]
