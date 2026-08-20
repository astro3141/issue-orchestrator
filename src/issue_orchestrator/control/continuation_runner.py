"""Executing the control operations this engine owns (#149).

Everything before this module decides *what is live* and *who holds it*. This
module is the part that acts, and its whole design is that it composes owners
that already exist rather than reimplementing what they decide:

======================================  =====================================
same-SHA admission, allowance, gate     :class:`~.publication_revalidation.PublicationRevalidation` (#139)
exact-commit materialisation            the issue's own branch, verified
making that checkout runnable           :class:`~.worktree_runnability.WorktreeRunnability` (#153)
the first reviewer's quick evidence     :class:`~.continuation_quick_validation.ContinuationQuickValidation` (#173)
reviewer-first exchange, PR creation    :class:`~.completion_processor.CompletionProcessor`
settlement of a discharged intent       :class:`~.continuation_finalize.ContinuationFinalizer`
ownership and exclusion                 :class:`~.control_operation_ownership.ControlOperationOwnership` (#146)
======================================  =====================================

Six rules keep it from becoming a second lifecycle.

**It never decides admission.** A ``RETRY_PENDING`` operation is handed whole
to #139, which re-checks the contract, the allowance and the reserve-before-
execute ordering itself. Nothing here reads
``revalidation_allowance_available`` to decide whether to call — the phase
predicate only says a retry is *pending*, and #139 alone says whether one may
start. There is no second admission predicate and no second allowance.

**It never fabricates intent.** The completion record it hands the completion
owner is written from the descriptor, field for field. A candidate with no
descriptor never reaches here: it is not live, so it is never owned, so it is
never advanced. The one field the descriptor cannot supply —
``validation_record_path``, which the ordinary path's coder turn fills in — is
not invented either: it names a record the configured quick gate has just
written into this run's own directory, and a run whose gate produced no such
record never opens (#173).

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
descriptor writer applies, reached from the other side.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.attempt import Attempt
from ..domain.continuation_phase import ContinuationPhase
from ..domain.models import get_completion_path
from .continuation_intent_record import write_continuation_completion_record
from .continuation_live_truth import LiveContinuation
from .continuation_quick_validation import PreparedQuickValidation
from .continuation_runs import ContinuationRun

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.continuation_descriptor import ContinuationDescriptor
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
    ) -> "ProcessingResult": ...


CONTINUATION_JOB_PREFIX = "control-continuation"
"""Job-id namespace, so a continuation can never collide with a republish."""


class ControlContinuationRunner:
    """Advances the control operations a reconciliation says this engine owns."""

    def __init__(
        self,
        *,
        state: "OrchestratorState",
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
        self._revalidation = revalidation_route
        self._attempts = attempts
        self._worktrees = worktrees
        self._working_copy = working_copy
        # The provisioning CORE, not the launch provisioner: the continuation's
        # bound is #149's run allowance, already spent before the checkout
        # existed, and the launch provisioner's consecutive-failure ledger and
        # ``needs-human`` escalation would be a second one over the same run.
        self._runnability = runnability
        # The evidence a coder turn would have produced, produced by the
        # system instead (#173). Composed, never reimplemented: it runs the
        # configured quick contract through the existing agent-side gate.
        self._quick_validation = quick_validation
        self._session_output = session_output
        self._completion_processor = completion_processor
        self._review_verdicts = review_verdicts
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
        self._repo_root = repo_root
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
            run = self._open_run(operation, descriptor)
            if run is None:
                return
        if self._process(operation, run):
            self._runs.close(operation.key)

    def _open_run(
        self, operation: LiveContinuation, descriptor: "ContinuationDescriptor"
    ) -> ContinuationRun | None:
        """Mint this operation's run: its worktree, its environment, its intent.

        Allocated once and registered with the owner immediately, so from the
        moment the checkout exists there is exactly one place that knows about
        it and exactly one place that will dispose of it.

        The order is::

            reserve the run allowance
                -> materialize the checkout at exactly A
                -> make it runnable, candidate unchanged
                -> allocate run assets
                -> produce this run's quick-validation evidence
                -> write the intent naming it, register the run

        The allowance is spent FIRST, before the checkout and long before the
        exchange, in the start-budget style #139 chose and for the reason it
        gives: a run interrupted anywhere leaves the allowance spent rather than
        refunding itself. Refunding would make the bound mean "one per crash",
        and this is the bound on the most expensive work in the system.

        Provisioning follows materialisation and precedes every asset the
        pipeline keys work to: a run whose worktree is not runnable must not
        exist, because the run directory, the completion record and the
        exchange's ``run_id`` are what re-enter the pipeline on the next pass.

        The run scaffold freezes the profile *the descriptor recorded*, for the
        reason #139's does: a candidate evaluated under one contract is
        continued under that contract, whatever the current default is bound to
        today. The publication gate therefore reuses the passing record for this
        exact HEAD/command/profile instead of re-running it.

        Quick-validation preparation sits between the run assets and the intent
        because it needs the first and is named by the second (#173): the run
        directory is where the evidence is written, and the completion record
        is what points the review exchange at it. A continuation has no coder
        turn to produce that evidence, and a reviewer told to trust a file
        nothing wrote answers about the missing file rather than about the
        code. A refusal here opens no run at all — see :meth:`_prepare_evidence`
        for what that costs and why it is not refunded.
        """
        if not self._reserve_run(operation):
            return None
        materialized = self._materialize(operation)
        if materialized is None:
            return None
        worktree, agent_label = materialized
        if not self._make_runnable(operation, worktree):
            return None
        assets = self._session_output.start_run(
            worktree_path=worktree,
            session_name=_worktree_name(operation),
            issue_number=operation.issue.number,
            agent_label=agent_label,
            validation_profile=descriptor.profile,
        )
        evidence = self._prepare_evidence(operation, worktree, assets)
        if evidence is None:
            return None
        completion_path = get_completion_path(
            agent_label, run_dir=assets.run_dir.name
        )
        # Written once, with the run. A resumed pass leaves the record exactly
        # as the pipeline left it: it is deliberately still on disk, and the
        # exchange running against it was started from that identity.
        write_continuation_completion_record(
            worktree / completion_path,
            descriptor,
            session_name=assets.identity.session_name,
            validation_record_path=evidence.record_path,
        )
        run = ContinuationRun(
            worktree=worktree,
            agent_label=agent_label,
            assets=assets,
            completion_path=completion_path,
        )
        self._runs.opened(operation.key, run)
        return run

    def _reserve_run(self, operation: LiveContinuation) -> bool:
        """Spend one of this candidate's continuation-run allowances.

        The ceiling itself belongs to :class:`~..domain.attempt.Attempt`, which
        re-asserts it on the write — so this neither reads the counter nor
        compares it, exactly as the retry path leaves admission to #139. A
        refusal here means the phase predicate and the durable record disagreed
        about the allowance, which is the interleaving the store settles.

        Returns:
            Whether a run may now be opened. ``False`` is a refusal to start,
            never a degraded start: the next reconciliation derives
            ``RUNS_EXHAUSTED`` from the same counter and hands the candidate
            back to ordinary rework.
        """
        try:
            reserved = self._attempts.update(
                operation.attempt.key,
                lambda attempt: attempt.with_continuation_run_reserved(),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "[CONTINUATION] %s opens no run: allowance could not be"
                " reserved: %s",
                operation.key,
                exc,
            )
            return False
        logger.info(
            "[CONTINUATION] %s reserved continuation run %d",
            operation.key,
            reserved.continuation_runs_used,
        )
        return True

    def _materialize(self, operation: LiveContinuation) -> tuple[Path, str] | None:
        """A disposable worktree standing at exactly this candidate's commit.

        On the issue's own branch rather than a detached checkout, because the
        pull request the continuation may create names a branch as its head and
        must name one whose tip *is* ``A``. The HEAD is verified after checkout
        rather than assumed: "the branch is probably still there" is exactly
        the assumption that would open a PR against other work.

        A branch that has moved past the candidate is supersession. The
        recorded intent is retired, which drops the operation from live truth
        on the next pass and releases the lease — the same rule the descriptor
        writer applies when a newer candidate files its own intent.
        """
        # The board's own accessor, not a second scan of its labels: which
        # lane an issue is in is the board's fact and has one reader.
        agent_lane = operation.issue.agent_type
        if agent_lane is None:
            logger.warning(
                "[CONTINUATION] refusing %s: the issue names no agent lane, so"
                " no reviewer pairing can be resolved",
                operation.key,
            )
            return None
        try:
            info = self._worktrees.create(
                self._repo_root,
                operation.issue.number,
                operation.issue.title,
                worktree_name=_worktree_name(operation),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            # Narrow on purpose. These are the failures a checkout can have;
            # anything else is a defect, and swallowing it here would turn a
            # broken continuation into a silently idle one that still holds the
            # issue. An escaped error is recorded by the job runner instead,
            # and the durable facts it did not change derive the same phase
            # next tick.
            logger.warning(
                "[CONTINUATION] %s could not be materialized: %s",
                operation.key,
                exc,
            )
            return None
        head = self._working_copy.get_head_sha(info.path)
        if head is None or head.strip().lower() != operation.key.head_sha:
            logger.info(
                "[CONTINUATION] retiring %s: the branch now stands at %s",
                operation.key,
                (head or "unknown")[:12],
            )
            self._discard_checkout(info.path)
            self._retire(operation)
            return None
        return info.path, agent_lane

    def _discard_checkout(self, worktree: Path) -> None:
        """Dispose of a checkout no run owns yet.

        Every pre-run refusal disposes HERE rather than through
        :class:`~.continuation_runs.ContinuationRuns`, because the run does not
        exist: nothing else knows this checkout is there, and the name is
        deterministic per candidate, so one left behind would block every later
        pass at ``git worktree add``.

        After a run is opened the rule inverts — the run owner disposes, and
        only when the completion pipeline says the work is finished — so this is
        reachable only from the refusals above ``_open_run``'s registration.
        """
        self._worktrees.remove_checkout(worktree, force=True)

    def _make_runnable(self, operation: LiveContinuation, worktree: Path) -> bool:
        """Make the continuation's CODER worktree runnable, or open no run.

        The checkout this run materialises is not a read-only carrier for a
        cached verdict. It is handed to the persistent review exchange as the
        coder's worktree, and a ``CHANGES_REQUESTED`` round asks that coder to
        edit and validate *in it* — so it needs the same runtime environment
        every other agent worktree gets, or the round dies on a toolchain that
        was never installed and the failure is attributed to the candidate
        (#48, #153).

        The recipe is the operator's own (``Config.setup_worktree``, via
        :class:`~.worktree_runnability.WorktreeRunnability`); nothing is
        hard-coded or copied here. The same core also proves what
        materialisation established is still true afterwards: provisioning may
        write untracked runtime state into the worktree, and may not move
        ``HEAD`` off ``A`` or leave the candidate's tracked content modified.

        The sibling REVIEWER worktree keeps its own policy
        (:mod:`~..execution.reviewer_worktree`): deliberately unprovisioned,
        guarded where the provider supports it. Nothing here reaches it.

        Failure opens no run at all, and the checkout is discarded through the
        one pre-run disposal (:meth:`_discard_checkout`). The reservation stays
        spent: it is a start budget, and refunding it would turn a repeatably
        broken environment into an unbounded supply of continuation runs. A
        later pass may open another run only while #149's existing allowance
        lasts, and once it is gone the ordinary ``RUNS_EXHAUSTED`` derivation
        hands the candidate back to rework — where the launch provisioner
        escalates the same broken environment to ``needs-human``. There is no
        second counter and no escalation of its own.

        Returns:
            Whether the worktree is runnable and the run may be opened.
        """
        unrunnable = self._runnability.make_runnable(worktree)
        if unrunnable is None:
            return True
        logger.warning(
            "[CONTINUATION] %s opens no run: its coder worktree could not be"
            " made runnable: %s",
            operation.key,
            unrunnable,
        )
        self._discard_checkout(worktree)
        return False

    def _prepare_evidence(
        self,
        operation: LiveContinuation,
        worktree: Path,
        assets: "SessionRunAssets",
    ) -> PreparedQuickValidation | None:
        """Produce this run's quick-validation evidence, or open no run.

        The ordinary path's first reviewer reads a record the CODER TURN
        produced and named on its completion record. A continuation replays a
        recorded intent and has no coder turn, so the same record is produced
        here — by the same configured quick gate, into this run's own
        directory, before the intent that names it is written. Nothing is
        reused from a durable verdict and nothing is synthesised: the gate runs
        or the run does not open.

        A refusal costs the whole run and the reservation stays spent, for the
        reason :meth:`_make_runnable`'s does — it is a start budget, and
        refunding it would turn a repeatably failing suite into an unbounded
        supply of continuation runs. Once #149's allowance is gone the ordinary
        ``RUNS_EXHAUSTED`` derivation hands the candidate back to rework, where
        a coder can see and fix what the quick contract rejected.

        Returns:
            What the preparation produced, or ``None`` when no run may open.
        """
        prepared = self._quick_validation.prepare(
            worktree=worktree, run_assets=assets
        )
        if isinstance(prepared, PreparedQuickValidation):
            logger.info(
                "[CONTINUATION] %s prepared quick validation: record=%s",
                operation.key,
                prepared.record_path or "none (no quick contract configured)",
            )
            return prepared
        logger.warning(
            "[CONTINUATION] %s opens no run: its reviewer's quick-validation"
            " evidence could not be produced: %s",
            operation.key,
            prepared.reason,
        )
        self._discard_checkout(worktree)
        return None

    def _process(self, operation: LiveContinuation, run: ContinuationRun) -> bool:
        """Re-enter the completion pipeline for ``run``, and say whether it ended.

        Returns:
            Whether the pipeline reached a TERMINAL result. ``False`` means the
            review exchange is running in the background, or a post-review
            failure was rerouted into rework: the work continues, so the run
            must stay open and neither a verdict nor a settlement has been
            produced to record.
        """
        result = self._completion_processor.process(
            run.worktree,
            operation.issue.number,
            operation.issue.title,
            run_assets=run.assets,
            completion_path=run.completion_path,
            agent_label=run.agent_label,
            issue_key=operation.issue.key,
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
        self._record_review_verdict(operation, run.assets.run_dir)
        # The verdict first, then the settlement: both are facts this run
        # produced, and the ordering is the crash window. Settled-without-a-
        # verdict loses only evidence about a PR that demonstrably exists;
        # verdict-without-settlement re-enters the pipeline, finds the open PR
        # and reuses it. Only the first ordering can lose the review outcome
        # for good.
        self._finalizer.finalize(operation, result)
        return True

    def _record_review_verdict(
        self, operation: LiveContinuation, run_dir: Path
    ) -> None:
        """Promote this run's exact-``A`` verdict binding into durable truth.

        The binding the exchange writes lives in the run directory, inside the
        worktree this run is about to delete — durable enough for the session
        that made it, and gone before anything could read it back. Copying it
        onto the attempt is what makes ``EXIT_TO_REWORK``, ``SETTLED_NO_PR``
        and ``APPROVED_PENDING_PR`` reconstructible after a restart, which is
        the whole of §8's review half.

        A verdict bound to another commit is dropped rather than filed: the
        attempt would refuse it anyway, and refusing here says why.
        """
        binding = self._review_verdicts.for_run(run_dir)
        if binding is None:
            logger.info(
                "[CONTINUATION] %s recorded no review verdict this run",
                operation.key,
            )
            return
        if not binding.covers(operation.key.head_sha):
            logger.warning(
                "[CONTINUATION] discarding %s verdict bound to %s: it is"
                " evidence about other work",
                operation.key,
                binding.reviewed_sha[:12],
            )
            return
        # The attempt's OWN key, not a third spelling rebuilt from the issue and
        # the operation. ``LiveContinuation`` already carries the record this
        # operation is about, and the binding between candidate and evidence
        # should have one spelling wherever it is written.
        self._attempts.update(
            operation.attempt.key,
            lambda attempt: attempt.with_continuation_review_verdict(binding),
        )
        logger.info(
            "[CONTINUATION] %s durable review verdict=%s",
            operation.key,
            binding.verdict.value,
        )

    def _retire(self, operation: LiveContinuation) -> None:
        """Clear the recorded intent for a candidate the branch has left behind.

        Any run the operation held goes with it. Retirement is reached while
        OPENING a run, so today there is never one to close — but retiring drops
        the operation out of live truth, and an operation nothing will advance
        again is one nothing else would close a run for. The disposal belongs
        with the decision that stranded it rather than with the caller that
        happens to make it today.
        """
        self._runs.close(operation.key)
        self._attempts.update(
            operation.attempt.key, Attempt.without_continuation_descriptor
        )


def _already_executing(operation: LiveContinuation) -> None:
    """Start nothing: this engine already has a run in flight for ``operation``."""
    logger.debug("[CONTINUATION] %s is already executing", operation.key)


def _worktree_name(operation: LiveContinuation) -> str:
    """A run-scoped name that cannot collide with the issue's own worktree."""
    return f"continuation-{operation.issue.number}-{operation.key.head_sha[:12]}"


__all__ = ["CONTINUATION_JOB_PREFIX", "ControlContinuationRunner"]
