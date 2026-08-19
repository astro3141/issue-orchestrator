"""Executing the control operations this engine owns (#149).

Everything before this module decides *what is live* and *who holds it*. This
module is the part that acts, and its whole design is that it composes owners
that already exist rather than reimplementing what they decide:

======================================  =====================================
same-SHA admission, allowance, gate     :class:`~.publication_revalidation.PublicationRevalidation` (#139)
exact-commit materialisation            the issue's own branch, verified
reviewer-first exchange, PR creation    :class:`~.completion_processor.CompletionProcessor`
ownership and exclusion                 :class:`~.control_operation_ownership.ControlOperationOwnership` (#146)
======================================  =====================================

Three rules keep it from becoming a second lifecycle.

**It never decides admission.** A ``RETRY_PENDING`` operation is handed whole
to #139, which re-checks the contract, the allowance and the reserve-before-
execute ordering itself. Nothing here reads
``revalidation_allowance_available`` to decide whether to call — the phase
predicate only says a retry is *pending*, and #139 alone says whether one may
start. There is no second admission predicate and no second allowance.

**It never fabricates intent.** The completion record it hands the completion
owner is written from the descriptor, field for field. A candidate with no
descriptor never reaches here: it is not live, so it is never owned, so it is
never advanced.

**It never races ordinary work.** Ownership already excludes the issue from the
queue, but an issue whose session is still running was never the continuation's
subject — the continuation exists for a candidate whose worktree is *gone*. So
a live session is an execution refusal, exactly as it is for the publish-retry
route, and the operation simply stays owned until the session finishes.

A supersession the durable record has not yet noticed is retired rather than
retried: if the issue's branch no longer points at the candidate, the intent
recorded for it is cleared, which drops the operation out of live truth on the
next pass and releases the lease. That is the same supersession rule the
descriptor writer applies, reached from the other side.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.attempt import Attempt, AttemptKey
from ..domain.continuation_phase import ContinuationPhase
from ..domain.models import CompletionOutcome, CompletionRecord, get_completion_path
from .continuation_live_truth import LiveContinuation

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
    from .continuation_live_truth import ContinuationReconciliation
    from .publication_revalidation import PublicationRevalidation

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


@dataclass(frozen=True, slots=True)
class _ContinuationRun:
    """One disposable worktree, verified to stand at the candidate's commit."""

    worktree: Path
    agent_label: str


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
        session_output: "SessionOutput",
        completion_processor: ContinuationCompletionOwner,
        review_verdicts: "ReviewVerdictBindings",
        jobs: ContinuationJobs,
        repo_root: Path,
    ) -> None:
        self._state = state
        self._revalidation = revalidation_route
        self._attempts = attempts
        self._worktrees = worktrees
        self._working_copy = working_copy
        self._session_output = session_output
        self._completion_processor = completion_processor
        self._review_verdicts = review_verdicts
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
        }

    # ------------------------------------------------------------------
    # Tick entry point
    # ------------------------------------------------------------------

    def advance(self, reconciliation: "ContinuationReconciliation") -> None:
        """Start work for every owned operation that has none in flight.

        Takes the reconciliation rather than re-deriving one: acting on a later
        reading than the exclusion was published from is precisely the stale
        decision this whole leaf exists to prevent.

        ``owned`` and not ``operations``: a ``CONTENDED`` operation is another
        holder's to advance, and an ``UNAVAILABLE`` one is nobody's until the
        store answers again. Both still exclude ordinary work.
        """
        for operation in reconciliation.owned:
            self._start(operation)

    def _start(self, operation: LiveContinuation) -> None:
        issue_number = operation.issue.number
        if self._has_active_session(issue_number):
            logger.debug(
                "[CONTINUATION] %s stays owned but idle: the issue has a live"
                " session",
                operation.key,
            )
            return
        job_id = f"{CONTINUATION_JOB_PREFIX}:{':'.join(operation.key.durable_parts)}"
        # ``submit`` reports an already-running job by returning False, which is
        # the whole duplicate guard: a job still running from a previous tick
        # must not be started again, and the operation stays owned meanwhile.
        if not self._jobs.submit(job_id, lambda: self._run(operation)):
            # The runner did not start it: either this operation's job is
            # already in flight from an earlier tick, or the deployment has no
            # background runner at all. Either way the operation stays owned
            # and the next reconciliation asks again.
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
        """
        self._advance_by_phase[operation.phase](operation)

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
        """Drive the exact candidate through the ordinary completion owner."""
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
        run = self._materialize(operation)
        if run is None:
            return
        try:
            self._process(operation, run, descriptor)
        finally:
            self._worktrees.remove_checkout(run.worktree, force=True)

    def _materialize(self, operation: LiveContinuation) -> _ContinuationRun | None:
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
            self._worktrees.remove_checkout(info.path, force=True)
            self._retire(operation)
            return None
        return _ContinuationRun(worktree=info.path, agent_label=agent_lane)

    def _process(
        self,
        operation: LiveContinuation,
        run: _ContinuationRun,
        descriptor: "ContinuationDescriptor",
    ) -> None:
        """Replay the recorded intent through the completion owner.

        The run scaffold freezes the profile *the descriptor recorded*, for the
        reason #139's does: a candidate evaluated under one contract is
        continued under that contract, whatever the current default is bound to
        today. The publication gate therefore reuses the passing record for
        this exact HEAD/command/profile instead of re-running it.
        """
        assets = self._session_output.start_run(
            worktree_path=run.worktree,
            session_name=_worktree_name(operation),
            issue_number=operation.issue.number,
            agent_label=run.agent_label,
            validation_profile=descriptor.profile,
        )
        completion_path = get_completion_path(
            run.agent_label, run_dir=assets.run_dir.name
        )
        _write_completion_record(
            run.worktree / completion_path,
            descriptor,
            session_name=assets.identity.session_name,
        )
        result = self._completion_processor.process(
            run.worktree,
            operation.issue.number,
            operation.issue.title,
            run_assets=assets,
            completion_path=completion_path,
            agent_label=run.agent_label,
            issue_key=operation.issue.key,
        )
        logger.info(
            "[CONTINUATION] %s completion processing success=%s: %s",
            operation.key,
            result.success,
            result.message,
        )
        self._record_review_verdict(operation, assets.run_dir)

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
        key = AttemptKey(operation.issue.key, operation.key.head_sha)
        self._attempts.update(
            key, lambda attempt: attempt.with_continuation_review_verdict(binding)
        )
        logger.info(
            "[CONTINUATION] %s durable review verdict=%s",
            operation.key,
            binding.verdict.value,
        )

    def _retire(self, operation: LiveContinuation) -> None:
        """Clear the recorded intent for a candidate the branch has left behind."""
        key = AttemptKey(operation.issue.key, operation.key.head_sha)
        self._attempts.update(key, Attempt.without_continuation_descriptor)


def _worktree_name(operation: LiveContinuation) -> str:
    """A run-scoped name that cannot collide with the issue's own worktree."""
    return f"continuation-{operation.issue.number}-{operation.key.head_sha[:12]}"


def _write_completion_record(
    path: Path,
    descriptor: "ContinuationDescriptor",
    *,
    session_name: str,
) -> None:
    """Put the recorded intent where the completion owner reads intent.

    Every field the agent owned is copied from the descriptor; every field the
    ORCHESTRATOR owns (the session identity, the timestamp) is supplied by the
    orchestrator. Nothing is invented in between: ``summary`` names this
    replay for a human reading the record, and carries no claim about the work.
    """
    record = CompletionRecord(
        session_id=session_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary="Recorded continuation intent replayed by the orchestrator",
        requested_actions=list(descriptor.requested_actions),
        implementation=descriptor.implementation or None,
        problems=descriptor.problems or None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")


__all__ = ["CONTINUATION_JOB_PREFIX", "ControlContinuationRunner"]
