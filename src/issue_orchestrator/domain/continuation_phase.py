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
the exact-``A`` review outcome   ``Attempt.review_verdict``
the run's terminal outcome       ``Attempt.continuation_settlement``
the continuation's own allowance ``Attempt.continuation_runs_used``
the board                        labels the tick already fetched
===============================  =============================================

Two facts here are NOT durable, and neither can be: whether this engine is
executing the operation right now, and whether it is carrying a run it has
already opened. #139's
allowance is a *start* budget spent before any gate work, so from the instant a
revalidation begins until the instant it files a verdict the durable facts read
``allowance spent, latest evaluation still non-PASS`` — indistinguishable from
a revalidation that finished and failed, and therefore ``EXHAUSTED``, and
therefore not live: reconciliation would release the lease of a running
operation and re-admit ordinary work onto the issue while it executed. The
distinguishing fact is process-local by nature, so it is passed in as a fact
like any other and checked first. It cannot create the deadlock a durable
marker would, because nothing that says "executing" survives the process that
said it: after a crash the answer is ``False`` and the phase falls back to the
durable facts, which is #139's own fail-closed direction.

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
``pr-pending`` that by contract will never appear. The recorded settlement
closes the same hole from the other side, for the intent that DID ask for one:
the continuation's own PR reaches no ``pr-pending`` writer, so the phase reads
the fact the run recorded rather than the board signal that never arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .continuation_descriptor import ContinuationDescriptor
from .continuation_settlement import ContinuationSettlement
from .review_verdict_binding import ReviewVerdictOutcome


class ContinuationPhase(Enum):
    """Where one exact candidate sits in the control continuation.

    ``live`` and ``exits_to_rework`` are properties of the member rather than
    sets held beside the enum, so adding a phase forces its author to say both
    whether it holds the issue and whether it hands the candidate back. A phase
    with no answer does not compile.
    """

    #: This engine has a run in flight for the candidate. Live whatever the
    #: durable facts currently say, because they are mid-change: the run has
    #: spent budgets, may be writing verdicts, and still holds a worktree.
    EXECUTING = ("executing", True, False)
    #: No recorded intent for this candidate, so there is no continuation to
    #: run. The absence is refusal, never a permissive default.
    NO_RECORDED_INTENT = ("no_recorded_intent", False, False)
    #: Intent recorded, but the publication contract has never reported on this
    #: candidate. Nothing to retry and nothing to continue from.
    NOT_EVALUATED = ("not_evaluated", False, False)
    #: Non-PASS with the #139 allowance still unspent: one same-SHA
    #: revalidation may be admitted (by #139, never by this predicate).
    RETRY_PENDING = ("retry_pending", True, False)
    #: ``PASS(A)`` recorded and no exact-``A`` review has settled. The
    #: continuation keeps the operation live *through* the PASS.
    PASS_PENDING_REVIEW = ("pass_pending_review", True, False)
    #: ``APPROVED(A)`` and the recorded intent asks for a pull request, which
    #: the board does not yet show.
    APPROVED_PENDING_PR = ("approved_pending_pr", True, False)
    #: ``CHANGES_REQUESTED(A)``: the control continuation hands off to ordinary
    #: rework. Not live, so ownership is released before rework is evaluated.
    EXIT_TO_REWORK = ("exit_to_rework", False, True)
    #: Nothing further is owed: either ``APPROVED(A)`` with no ``CREATE_PR`` in
    #: the recorded intent, or a run that recorded exactly that as its outcome.
    SETTLED_NO_PR = ("settled_no_pr", False, False)
    #: The continuation reached a pull request — either the run recorded that
    #: it opened one, or the board carries ``pr-pending``. Terminal.
    SETTLED_PR = ("settled_pr", False, False)
    #: Non-PASS with the allowance spent. No second revalidation exists, so the
    #: candidate returns to ordinary rework with its evidence history intact.
    EXHAUSTED = ("exhausted", False, True)
    #: The continuation has opened every run it may (#149's own allowance) and
    #: none discharged the recorded intent. The same clean return ``EXHAUSTED``
    #: gives the revalidation half: ordinary rework takes the candidate back,
    #: with the descriptor, the evaluations and any review verdict intact.
    RUNS_EXHAUSTED = ("runs_exhausted", False, True)

    def __init__(self, value: str, live: bool, exits_to_rework: bool) -> None:
        if live and exits_to_rework:
            # The two answers are about the same instant: an operation that
            # still holds its issue has not handed it to anybody, and a phase
            # claiming both would have ordinary rework admitted onto an issue
            # the continuation is simultaneously excluding from it.
            raise ValueError(
                f"continuation phase {value!r} cannot be live and exit to rework"
            )
        self._value_ = value
        self._live = live
        self._exits_to_rework = exits_to_rework

    @property
    def live(self) -> bool:
        """Whether an operation in this phase holds its issue against ordinary work."""
        return self._live

    @property
    def exits_to_rework(self) -> bool:
        """Whether this phase returns the candidate to the ordinary rework lane.

        The three phases that answer yes are the three whose own documentation
        says so: ``CHANGES_REQUESTED(A)`` transfers the candidate, a spent
        revalidation allowance returns it, and a spent run allowance returns it.
        Every other non-live phase means something else entirely — no intent was
        ever recorded, nothing was ever evaluated, or the continuation reached
        its settlement — and handing any of those to rework would invent work
        rather than continue it.

        "Exits to rework" is not "is owed rework": the phase says the control
        continuation is finished with the candidate, and the rework-cycle owner
        still decides whether a cycle may be spent on it. Keeping the two apart
        is what stops this predicate from becoming a second budget.
        """
        return self._exits_to_rework


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
    #: What this candidate's continuation run recorded as its terminal outcome,
    #: or ``None`` while the recorded intent is still undischarged.
    settlement: ContinuationSettlement | None = None
    #: Whether this candidate's continuation may still OPEN a run (#149).
    continuation_run_allowance_available: bool = True
    #: Whether THIS engine has a job in flight for the candidate. One of the two
    #: facts here that are not durable, and that cannot be: see the module
    #: docstring for why they are facts rather than lease reads, and why they
    #: fail to ``False`` across a restart instead of pinning an operation.
    engine_is_executing: bool = False
    #: Whether this engine is carrying an already-open run for the candidate.
    #: Distinct from ``engine_is_executing``: a run spans as many passes as the
    #: completion pipeline needs, while a job in flight is one submission. Only
    #: this fact can tell "the allowance is spent and the run that spent it is
    #: still going" from "the allowance is spent and nothing came of it".
    engine_holds_open_run: bool = False


def derive_continuation_phase(facts: ContinuationFacts) -> ContinuationPhase:
    """The phase ``facts`` state, decided in one place and in one order.

    Order matters and is the policy: execution outranks everything, then
    settlement is checked before continuation, so a candidate that already
    reached a pull request or a terminal review verdict can never be
    re-derived as live and re-run. Nothing below reads a lease row, a label
    other than ``pr-pending``, a session, or a terminal.
    """
    if facts.engine_is_executing:
        # First, and above even "no recorded intent": a run in flight has
        # already changed durable facts and is still changing them, so every
        # predicate below is being read mid-write. The one thing that is
        # certainly true is that the operation is running, and an operation
        # that is running holds its issue.
        return ContinuationPhase.EXECUTING
    descriptor = facts.descriptor
    if descriptor is None:
        # The one refusal that outranks every other fact. A PASS, an approval
        # and a clean board still do not make a continuation, because no agent
        # ever said what should happen to this candidate.
        return ContinuationPhase.NO_RECORDED_INTENT
    if facts.settlement is not None:
        # Checked before the board and before the review verdict, because it is
        # the only settlement signal the continuation's OWN run can produce. The
        # continuation creates no session and its pull request carries no
        # code-review label, so nothing writes ``pr-pending`` for it; deriving
        # from the board alone is how ``APPROVED_PENDING_PR`` stayed live
        # forever, re-running a full reviewer exchange every reconciliation.
        return (
            ContinuationPhase.SETTLED_PR
            if facts.settlement.opened_pull_request
            else ContinuationPhase.SETTLED_NO_PR
        )
    if facts.board_shows_pr_pending:
        # Checked before the review verdict: a PR that exists is the settlement
        # the whole continuation was for, whatever the ownership store still
        # says. This is the crash window "PR created, release incomplete".
        return ContinuationPhase.SETTLED_PR
    if facts.review_verdict is ReviewVerdictOutcome.CHANGES_REQUESTED:
        return ContinuationPhase.EXIT_TO_REWORK
    if facts.review_verdict is ReviewVerdictOutcome.APPROVED:
        if not descriptor.creates_pr:
            return ContinuationPhase.SETTLED_NO_PR
        return _run_or_exhausted(facts, ContinuationPhase.APPROVED_PENDING_PR)
    if not facts.has_publication_evaluation:
        return ContinuationPhase.NOT_EVALUATED
    if facts.latest_publication_passed:
        return _run_or_exhausted(facts, ContinuationPhase.PASS_PENDING_REVIEW)
    return (
        ContinuationPhase.RETRY_PENDING
        if facts.revalidation_allowance_available
        else ContinuationPhase.EXHAUSTED
    )


def _run_or_exhausted(
    facts: ContinuationFacts, phase: ContinuationPhase
) -> ContinuationPhase:
    """``phase``, unless the continuation may open no further run for it.

    Both phases this guards drive a full reviewer/coder exchange, and both are
    re-derived unchanged by a run that ended without discharging the intent — so
    without the bound they are the system's one unbounded retry surface.

    A run this engine already HOLDS is not a new one, and that is the whole
    reason the second fact exists. The allowance is spent when a run opens, so
    from that instant the durable counter alone reads "exhausted" for the entire
    life of the run it just paid for — including every pass on which a deferred
    review exchange re-enters the pipeline. Dropping the operation there would
    release the lease under a running exchange and sweep away its worktree.

    After a crash the engine holds nothing, so the same facts read as exhausted
    and the candidate returns to ordinary rework. That is #139's fail-closed
    direction, not a gap: the exchange the spent allowance paid for died with
    the process, and nothing durable records that it accomplished anything.
    """
    if facts.continuation_run_allowance_available or facts.engine_holds_open_run:
        return phase
    return ContinuationPhase.RUNS_EXHAUSTED


__all__ = [
    "ContinuationFacts",
    "ContinuationPhase",
    "derive_continuation_phase",
]
