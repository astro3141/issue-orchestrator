"""Executing the control operations this engine owns (#149).

Everything before this module decides *what is live* and *who holds it*. This
module is the part that acts, and its whole design is that it composes owners
that already exist rather than reimplementing what they decide:

======================================  =====================================
same-SHA admission, allowance, gate     :class:`~.publication_revalidation.PublicationRevalidation` (#139)
opening a run at exactly A              :class:`~.continuation_run_open.ContinuationRunOpener` (#149, #153, #173)
reviewer-first exchange, PR creation    :class:`~.completion_processor.CompletionProcessor`
promoting the run's exact-A verdict     :class:`~.continuation_review_evidence.ContinuationReviewEvidence` (#178, #180)
settlement of a discharged intent       :class:`~.continuation_finalize.ContinuationFinalizer`
ownership and exclusion                 :class:`~.control_operation_ownership.ControlOperationOwnership` (#146)
======================================  =====================================

What is left here is the decision of *which* owned operation to advance now,
and the re-entry of a run that already exists into the completion pipeline.
Eight rules keep that from becoming a second lifecycle.

**It never acts on an issue this engine was not started for.** Everything above
this module is board-wide by design: a reconciliation releases every lease the
derived live set does not name, so an engine that LOOKED at less would report
another engine's running operation as finished and free it. The set handed here
is therefore the whole board's, and ``owned`` says only that this deployment
holds the durable lease — the stable ``single-instance`` holder every engine on
this checkout shares. #306 measured what follows if that visibility is taken as
permission: an engine started with ``--issue A`` advanced a LIVE continuation
for issue B — claiming it, submitting its job, cutting its worktree and running
its gate. So the first question asked of an owned operation is the engine's own
actuation scope, taken from the owner :class:`~.queue_cache.QueueCache` and the
#304 rework handoff already ask (:class:`~.issue_scope.EngineIssueScope`) rather
than re-derived here from ``--issue``. It is asked before the execution claim,
which makes every boundary below it — the job, the revalidation route, the
checkout, the gate, the durable attempt write — unreachable for an out-of-scope
operation. Withholding is not releasing: B stays derived, stays owned, keeps
excluding ordinary work, and is advanced by whichever engine's scope includes it
(see :meth:`ControlContinuationRunner._refuse_outside_scope`).

**It never decides admission.** A ``RETRY_PENDING`` operation is handed whole
to #139, which re-checks the contract, the allowance and the reserve-before-
execute ordering itself. Nothing here reads
``revalidation_allowance_available`` to decide whether to call — the phase
predicate only says a retry is *pending*, and #139 alone says whether one may
start. There is no second admission predicate and no second allowance.

**It never fabricates intent.** The completion record it hands the completion
owner is written from the descriptor, field for field — by the opener, which
is where every field of it comes from. A candidate with no descriptor never
reaches here: it is not live, so it is never owned, so it is never advanced.
The one field the descriptor cannot supply — ``validation_record_path``, which
the ordinary path's coder turn fills in — is not invented either: it names a
record the configured quick gate has just written into this run's own
directory, and a run whose gate produced no such record never opens (#173).

**It never reworks the candidate it is reviewing.** The exchange it re-enters
is reviewer-first and owns a coder, and on the ordinary path a
changes-requested round hands the feedback straight to that coder. Here that
round would move the branch to ``A'`` while the operation still held the issue
for exactly ``A`` — the ordering #149 settled, run backwards, and the shape
#193 was observed in. So this route tells the exchange it may not rework in
place (#180): a changes-requested round terminates it with a durable
``CHANGES_REQUESTED(A)``, which is the transfer fact ``EXIT_TO_REWORK`` reads,
and ordinary rework produces ``A'`` after the release rather than before it.

**It never races ordinary work.** Ownership already excludes the issue from the
queue, but an issue whose session is still running was never the continuation's
subject — the continuation exists for a candidate whose worktree is *gone*. So
a live session is an execution refusal, exactly as it is for the publish-retry
route, and the operation simply stays owned until the session finishes.

**It never starts new work while the engine is paused.** Pause is a new-work
barrier (#161), and this is the one place a control operation's work begins, so
this is where the barrier goes. It is deliberately NOT in
:meth:`~.continuation_scheduling.ControlContinuation.reconcile`: a paused engine
must go on reading durable truth, reconciling #146 ownership and publishing the
exclusion projection, or a pause would free every running operation to ordinary
work. What it must not do is submit a job, spend a #139 or #149 allowance, cut
a checkout, open a reviewer exchange or create a pull request — all of which
begin below :meth:`ControlContinuationRunner.advance`. Withholding is not
cancelling: an operation stays owned, its run stays open, and the next
reconciliation after a resume starts whatever is still live.

**It never discards what its own run produced.** The
:class:`~.completion_types.ProcessingResult` a run returns is the ONLY record
that this operation created the pull request its intent asked for — no session
completes, and the PR carries no code-review label, so none of the three writers
of ``pr-pending`` observe it. Handing that result to the finalizer is what makes
the operation terminate; logging it and dropping it is what made
``APPROVED_PENDING_PR`` re-run a full reviewer exchange on every reconciliation.

**It never settles on review evidence it does not hold.** A run whose completion
actually completed a review exchange terminates only once that exchange's
exact-``A`` :class:`~..domain.review_verdict_binding.BoundReviewVerdict` has been
read and promoted (#178). The promotion is therefore a decision and has an
owner — :class:`~.continuation_review_evidence.ContinuationReviewEvidence` — and
a refusal from it withholds the settlement rather than being logged past. A
completion that ran no exchange is untouched by the rule: it never had review
evidence, so requiring some would fail-close the one path this is not about.

**It never outlives its own run.** ``process`` is not necessarily finished when
it returns: with a background supervisor wired — the only configuration in which
this runner executes at all — the review exchange becomes its own job and the
result says ``review_exchange_deferred``. So the pass is not the unit of
ownership; the run is, and :mod:`.continuation_runs` holds it across as many
passes as the pipeline needs. A pass that disposed of its worktree on the way
out would delete the working directory of the exchange still running in it, and
the next pass would mint a fresh ``run_id`` that no dedupe keyed on the old one
could recognise — one more exchange per reconciliation, forever.

A supersession the durable record has not yet noticed is retired rather than
retried: if the issue's branch no longer points at the candidate, the intent
recorded for it is cleared, which drops the operation out of live truth on the
next pass and releases the lease. That is the same supersession rule the
descriptor writer applies, reached from the other side — and it is decided
where the branch is read, in the opener, not here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.continuation_phase import ContinuationPhase
from ..domain.review_exchange_rework import ReviewExchangeRework
from .continuation_live_truth import LiveContinuation
from .continuation_review_evidence import ContinuationReviewEvidence
from .continuation_run_open import ContinuationRunOpener
from .continuation_runs import ContinuationRun

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.issue_key import IssueKey
    from ..domain.models import OrchestratorState
    from ..domain.session_run import SessionRunAssets
    from ..ports.attempt_store import AttemptStore
    from ..ports.review_verdict_bindings import ReviewVerdictBindings
    from ..ports.session_output import SessionOutput
    from ..ports.working_copy import WorkingCopy
    from ..ports.worktree_manager import WorktreeManager
    from .completion_types import ProcessingResult
    from .continuation_finalize import ContinuationFinalizer
    from .continuation_in_flight import ContinuationsInFlight
    from .continuation_live_truth import ContinuationReconciliation
    from .continuation_quick_validation import ContinuationQuickValidation
    from .continuation_runs import ContinuationRuns
    from .issue_scope import EngineIssueScope
    from .publication_revalidation import PublicationRevalidation
    from .worktree_runnability import WorktreeRunnability

logger = logging.getLogger(__name__)


class ContinuationJobs(Protocol):
    """Somewhere to run one continuation off the tick thread.

    Narrower than :class:`~..ports.background_job.BackgroundJobRunner` because
    the continuation neither polls nor drains: draining belongs to whoever owns
    the runner's failure handling, and a second drainer would take completions
    that owner needs to see. All this needs is a place to start work and a
    truthful answer about whether it was accepted.
    """

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool: ...


class ContinuationCompletionOwner(Protocol):
    """The completion owner's surface, as the continuation uses it.

    Narrow on purpose: the continuation re-enters the ordinary completion
    pipeline exactly as the publish-retry route does, and naming only the one
    method it calls keeps a would-be second lifecycle from quietly growing
    reach into the rest of that owner.
    """

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: "SessionRunAssets",
        pr_number: int | None = ...,
        completion_path: str | None = ...,
        agent_label: str | None = ...,
        issue_key: "IssueKey | None",
        rework: ReviewExchangeRework = ...,
    ) -> "ProcessingResult": ...


CONTINUATION_JOB_PREFIX = "control-continuation"
"""Job-id namespace, so a continuation can never collide with a republish."""


class ControlContinuationRunner:
    """Advances the control operations a reconciliation says this engine owns."""

    def __init__(
        self,
        *,
        state: "OrchestratorState",
        scope: "EngineIssueScope",
        revalidation_route: "PublicationRevalidation",
        attempts: "AttemptStore",
        worktrees: "WorktreeManager",
        working_copy: "WorkingCopy",
        runnability: "WorktreeRunnability",
        quick_validation: "ContinuationQuickValidation",
        session_output: "SessionOutput",
        completion_processor: ContinuationCompletionOwner,
        review_verdicts: "ReviewVerdictBindings",
        finalizer: "ContinuationFinalizer",
        in_flight: "ContinuationsInFlight",
        runs: "ContinuationRuns",
        jobs: ContinuationJobs,
        repo_root: Path,
    ) -> None:
        self._state = state
        #: What this engine may ACT on (#307). Required, with no default: a
        #: runner assembled without one is the engine #306 measured, and it
        #: looks entirely healthy right up to the moment it cuts a worktree for
        #: an issue nobody asked it about. The OWNER, never a ``Config`` — this
        #: module must not be able to form its own opinion about ``--issue``,
        #: and it must not be able to disagree with the one the #304 rework
        #: handoff holds.
        self._scope = scope
        self._revalidation = revalidation_route
        # Opening a run is its own subject: an ordered sequence of owners in
        # which every refusal costs the same thing. Composed here from the
        # collaborators this runner is already given, rather than wired
        # separately, because the two are one deployment decision — a runner
        # that could not open a run would have nothing to advance.
        self._opener = ContinuationRunOpener(
            attempts=attempts,
            worktrees=worktrees,
            working_copy=working_copy,
            runnability=runnability,
            quick_validation=quick_validation,
            session_output=session_output,
            runs=runs,
            repo_root=repo_root,
        )
        self._completion_processor = completion_processor
        # Composed here for the reason the opener is: promoting the exchange's
        # verdict and settling on it are one deployment decision, and a runner
        # that could settle without having promoted would be precisely the
        # engine #178 exists to remove.
        self._review_evidence = ContinuationReviewEvidence(
            attempts=attempts, review_verdicts=review_verdicts
        )
        self._finalizer = finalizer
        # The SAME registry live truth reads. The claim taken here is what keeps
        # a reconciliation seconds later from deriving a mid-run candidate as
        # finished and releasing the lease under a running operation.
        self._in_flight = in_flight
        # Separate from the claim above and deliberately so: the claim spans one
        # job submission, while a run spans as many passes as the completion
        # pipeline needs to finish. See :mod:`.continuation_runs`.
        self._runs = runs
        self._jobs = jobs
        # What each LIVE phase does, decided in one table rather than by a
        # branch chain at the call site. Only live phases appear: a settled or
        # exhausted operation is never owned, so it never reaches here. A new
        # live phase with no entry raises at the lookup, which is the loud
        # failure a silent fallthrough would not be.
        self._advance_by_phase: dict[
            ContinuationPhase, Callable[[LiveContinuation], None]
        ] = {
            ContinuationPhase.RETRY_PENDING: self._revalidate,
            ContinuationPhase.PASS_PENDING_REVIEW: self._continue_into_review,
            ContinuationPhase.APPROVED_PENDING_PR: self._continue_into_review,
            # An operation this engine is already executing has nothing to
            # start. It is a live phase and therefore reachable, so it needs a
            # handler; the honest one does nothing. The claim below normally
            # refuses long before the lookup, but a run that finished between
            # the reconciliation and the submit reaches here, and a KeyError is
            # not what "the work is already done" should look like.
            ContinuationPhase.EXECUTING: _already_executing,
        }

    # ------------------------------------------------------------------
    # Tick entry point
    # ------------------------------------------------------------------

    def advance(self, reconciliation: "ContinuationReconciliation") -> None:
        """Start work for every owned operation this engine may start now.

        Takes the reconciliation rather than re-deriving one: acting on a later
        reading than the exclusion was published from is precisely the stale
        decision this whole leaf exists to prevent.

        ``owned`` and not ``operations``: a ``CONTENDED`` operation is another
        holder's to advance, and an ``UNAVAILABLE`` one is nobody's until the
        store advances again. Both still exclude ordinary work.

        The sweep comes first and is over ``operations``, not ``owned``: a run
        held open across passes belongs to an operation that is still live, and
        one whose operation has dropped out of live truth — superseded intent, a
        pull request that arrived some other way — is held by nobody and would
        otherwise survive until the engine restarted. A ``CONTENDED`` operation
        is live and keeps its run; only leaving the live set closes one.

        A paused engine gets the sweep and nothing else (#161). The sweep is
        disposal of a checkout nobody holds any more, not the start of anything,
        and withholding it would leave a paused engine leaking the worktrees of
        operations that left live truth while it was stopped.
        """
        self._runs.close_dropped(frozenset(reconciliation.keys))
        if self._state.paused:
            self._withhold(reconciliation)
            return
        for operation in reconciliation.owned:
            self._start(operation)

    def _withhold(self, reconciliation: "ContinuationReconciliation") -> None:
        """Name what a paused engine is not starting, and leave it owned.

        Every operation here keeps its lease, its recorded intent, its
        allowances and any run already open: the barrier withholds a START, and
        an operation that is not started has changed nothing to undo. It is
        also not a refusal the durable record remembers — the next
        reconciliation after a resume derives the same live set and starts it.
        """
        for operation in reconciliation.owned:
            logger.info(
                "[CONTINUATION] %s stays owned but idle: the engine is paused,"
                " so no new execution starts until it resumes",
                operation.key,
            )

    def _start(self, operation: LiveContinuation) -> None:
        # Authority first, before anything this engine could be said to have
        # started (#307). The reconciliation that produced this operation was
        # board-wide because it had to be, and the lease it holds is the stable
        # single-instance holder's — neither is a statement that THIS engine
        # was started to work the issue.
        if self._scope.excludes(operation.issue):
            self._refuse_outside_scope(operation)
            return
        issue_number = operation.issue.number
        if self._has_active_session(issue_number):
            logger.debug(
                "[CONTINUATION] %s stays owned but idle: the issue has a live"
                " session",
                operation.key,
            )
            return
        # Claimed HERE and not inside the job, because a job runner may queue
        # work: between "submitted" and "started" the operation would otherwise
        # be unclaimed, and a reconciliation in that gap derives from durable
        # facts a started run is about to change. The claim is also the primary
        # duplicate guard — atomic, and taken before anything external happens.
        if not self._in_flight.claim(operation.key):
            logger.debug(
                "[CONTINUATION] %s already executing in this engine", operation.key
            )
            return
        job_id = f"{CONTINUATION_JOB_PREFIX}:{':'.join(operation.key.durable_parts)}"
        # ``submit`` reports an already-running job by returning False, which is
        # the second half of the duplicate guard: a job still running from a
        # previous tick must not be started again, and the operation stays owned
        # meanwhile.
        if not self._jobs.submit(job_id, lambda: self._run(operation)):
            # The runner did not start it: either this operation's job is
            # already in flight from an earlier tick, or the deployment has no
            # background runner at all. Either way the operation stays owned
            # and the next reconciliation asks again — so the claim this tick
            # took must be given back, or nothing would ever ask again.
            #
            # One narrower window stays open by construction: a runner that
            # ACCEPTS the job and then never dispatches it (a supervisor
            # shutting down between the two) leaves a claim no ``finally`` will
            # release, and the lane stays held. It is not distinguishable from a
            # job that is about to start — the distinction is the whole point of
            # the claim — so there is nothing to detect. It is bounded the same
            # way every claim is: process-local, so a restart clears it.
            self._in_flight.release(operation.key)
            logger.debug("[CONTINUATION] %s not started this tick", operation.key)

    def _refuse_outside_scope(self, operation: LiveContinuation) -> None:
        """Leave an operation this engine reconciled but may not work (#307).

        Withheld, not released, and the difference is the whole of why this
        refusal is safe. The operation keeps its lease, its recorded intent,
        its allowances and any run already open; it stays in live truth, so it
        goes on excluding ordinary work on that issue, which is the
        conservative direction — a live control operation still holds the issue
        against a queue that would otherwise start a session on it.

        Nothing durable records the refusal either, which is what keeps it from
        becoming a scope-induced deadlock. The ownership holder is stable and
        restart-adoptable by design, so a later engine whose scope includes the
        issue adopts the very same lease, derives the very same phase, and
        starts the operation with no lease surgery and no state to unwind. The
        refusal is a fact about THIS engine's configuration, not about the
        candidate, and it lasts exactly as long as that configuration does.

        Logged rather than published, for the reason #304 gives for the same
        refusal one step further down the sequence: a narrowed engine
        announcing a refusal about a foreign issue is the cross-issue traffic
        this leaf removes, wearing a different shape. Nothing is stuck — the
        operation is somebody's, just not this engine's.
        """
        logger.debug(
            "[CONTINUATION] %s stays owned but idle: issue #%d is outside this"
            " engine's issue scope, so this engine reconciles it without"
            " advancing it",
            operation.key,
            operation.issue.number,
        )

    def _has_active_session(self, issue_number: int) -> bool:
        return any(
            session.issue.number == issue_number
            for session in self._state.active_sessions
        )

    # ------------------------------------------------------------------
    # Off-tick execution
    # ------------------------------------------------------------------

    def _run(self, operation: LiveContinuation) -> None:
        """Advance one operation. Runs off the tick thread.

        Ownership is not released on failure, and an error is not caught here.
        A run that fails changed no durable fact, so the next reconciliation
        derives the same phase and the operation is attempted again; releasing
        on error would instead free the issue while a partially-applied run's
        side effects were still landing. The job runner records what escaped,
        which is the loud report a swallow would not be.

        The execution claim IS released on error, and only there is the
        distinction: ownership is durable and says "this operation is someone's
        to advance", while the claim is process-local and says "a run is in
        flight right now". A run that ended — cleanly or not — is not in flight,
        and a claim left behind by a raised handler would pin the issue until
        the engine restarted.
        """
        try:
            self._advance_by_phase[operation.phase](operation)
        finally:
            self._in_flight.release(operation.key)

    def _revalidate(self, operation: LiveContinuation) -> None:
        """Hand the candidate to #139, whole.

        The attempt record travels as the identity; #139 re-reads it from the
        store and applies its own admission predicate. If it refuses, it
        refuses — this does not second-guess a refusal, because doing so would
        be the second admission owner the policy forbids.
        """
        outcome = self._revalidation.revalidate(operation.attempt)
        logger.info(
            "[CONTINUATION] %s revalidation started=%s reason=%s",
            operation.key,
            outcome.started,
            outcome.reason,
        )

    def _continue_into_review(self, operation: LiveContinuation) -> None:
        """Drive the exact candidate through the ordinary completion owner.

        Re-entrant across passes, because ``process`` is. When a background job
        supervisor is wired — which is exactly when this runner executes at all,
        since its own job goes through the same supervisor — the review exchange
        is submitted as its own job and ``process`` returns
        ``review_exchange_deferred``. The pipeline states what that obliges of a
        caller: the work is NOT terminated, and the completion record is left on
        disk so the next observation re-enters. So the run stays open, the next
        pass resumes it with the same worktree and the same ``run_id``, and only
        a terminal result closes it.

        A raised pipeline also leaves the run open. Deleting a worktree an
        exchange may still be using is the failure this ordering exists to stop,
        and it is not made safer by having arrived via an exception.
        """
        descriptor = operation.attempt.continuation_descriptor
        if descriptor is None:
            # Unreachable through live truth, which refuses a descriptor-less
            # candidate before it can be owned. Loud rather than assumed: a
            # continuation running on no recorded intent is the one outcome
            # this leaf exists to make impossible.
            logger.error(
                "[CONTINUATION] refusing %s: no recorded intent", operation.key
            )
            return
        run = self._runs.resume(operation.key)
        if run is None:
            run = self._opener.open(operation, descriptor)
            if run is None:
                return
        if self._process(operation, run):
            self._runs.close(operation.key)

    def _process(self, operation: LiveContinuation, run: ContinuationRun) -> bool:
        """Re-enter the completion pipeline for ``run``, and say whether it ended.

        The one thing this route says about the exchange itself is that it may
        not rework in place (#180). Everything else about the exchange — its
        rounds, its reviewer pairing, its gate — is the completion owner's, and
        the continuation second-guesses none of it. But the coder that exchange
        would rework with is not the continuation's to spend: this candidate's
        session is gone, the operation still holds the issue against ordinary
        work, and a coder round here would move the branch to ``A'`` while
        exactly ``A`` was still the thing under review. So a changes-requested
        round terminates the exchange with a durable ``CHANGES_REQUESTED(A)``,
        which :class:`~.continuation_review_evidence.ContinuationReviewEvidence`
        promotes onto the attempt so the phase derives ``EXIT_TO_REWORK`` —
        #149's ordering, in which ownership is released BEFORE ordinary rework
        is evaluated, and only ordinary rework ever produces ``A'``.

        Returns:
            Whether the pipeline reached a TERMINAL result. ``False`` means the
            review exchange is running in the background, or a post-review
            failure was rerouted into rework: the work continues, so the run
            must stay open and neither a verdict nor a settlement has been
            produced to record.

            ``True`` says the pipeline finished with this run's checkout, which
            is a fact about the pipeline and not about the settlement: a
            terminal pass that withheld its settlement still closes its run.
            The intent stays undischarged, so the operation stays live and the
            next reconciliation opens a fresh run for it — spending one #149
            allowance, which is what bounds the retry.
        """
        result = self._completion_processor.process(
            run.worktree,
            operation.issue.number,
            operation.issue.title,
            run_assets=run.assets,
            completion_path=run.completion_path,
            agent_label=run.agent_label,
            issue_key=operation.issue.key,
            rework=ReviewExchangeRework.HAND_OFF,
        )
        logger.info(
            "[CONTINUATION] %s completion processing success=%s: %s",
            operation.key,
            result.success,
            result.message,
        )
        if result.is_non_terminal:
            logger.info(
                "[CONTINUATION] %s keeps run %s open: completion has not"
                " finished for this record",
                operation.key,
                run.assets.run_id,
            )
            return False
        # The verdict first, and the settlement ONLY on the strength of it
        # (#178). The ordering was always the crash window — a
        # verdict-without-settlement re-enters the pipeline, finds the open pull
        # request and reuses it, so only the reverse could lose the review
        # outcome for good — but ordering alone let the second step run on a
        # promotion that had refused. A completion that completed a review
        # exchange now settles only on evidence this run actually holds; one
        # that ran no exchange settles exactly as it did before.
        evidence = self._review_evidence.promote(operation, result)
        if not evidence.settles:
            logger.warning(
                "[CONTINUATION] %s withholds settlement: its review exchange"
                " left no promoted exact-%s verdict (%s). The recorded intent"
                " stays undischarged and the operation stays re-enterable",
                operation.key,
                operation.key.head_sha[:12],
                evidence.value,
            )
            return True
        self._finalizer.finalize(operation, result)
        return True


def _already_executing(operation: LiveContinuation) -> None:
    """Start nothing: this engine already has a run in flight for ``operation``."""
    logger.debug("[CONTINUATION] %s is already executing", operation.key)


__all__ = ["CONTINUATION_JOB_PREFIX", "ControlContinuationRunner"]
