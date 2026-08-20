"""Minting one continuation run (#149, #173).

Everything that must go right before a continuation run exists, and the
disposal rule for every way it can go wrong. Split from
:mod:`.continuation_runner`, which decides *which* owned operation to advance
and re-enters the completion pipeline for a run that already exists: opening a
run is a sequence of five owners in a fixed order, and each step's refusal
costs the same thing, which is one subject rather than a section of another.

The order is::

    reserve the run allowance
        -> materialize the checkout at exactly A
        -> make it runnable, candidate unchanged
        -> allocate run assets
        -> produce this run's quick-validation evidence
        -> write the intent naming it, register the run

The allowance is spent FIRST, before the checkout and long before the exchange,
in the start-budget style #139 chose and for the reason it gives: a run
interrupted anywhere leaves the allowance spent rather than refunding itself.
Refunding would make the bound mean "one per crash", and this is the bound on
the most expensive work in the system. Every refusal below therefore leaves the
reservation spent, and once #149's allowance is gone the ordinary
``RUNS_EXHAUSTED`` derivation hands the candidate back to rework.

Provisioning follows materialisation and precedes every asset the pipeline keys
work to: a run whose worktree is not runnable must not exist, because the run
directory, the completion record and the exchange's ``run_id`` are what re-enter
the pipeline on the next pass.

The run scaffold freezes the profile *the descriptor recorded*, for the reason
#139's does: a candidate evaluated under one contract is continued under that
contract, whatever the current default is bound to today. The publication gate
therefore reuses the passing record for this exact HEAD/command/profile instead
of re-running it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.attempt import Attempt
from ..domain.models import get_completion_path
from .continuation_intent_record import write_continuation_completion_record
from .continuation_live_truth import LiveContinuation
from .continuation_quick_validation import PreparedQuickValidation
from .continuation_runs import ContinuationRun

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.continuation_descriptor import ContinuationDescriptor
    from ..domain.session_run import SessionRunAssets
    from ..ports.attempt_store import AttemptStore
    from ..ports.session_output import SessionOutput
    from ..ports.working_copy import WorkingCopy
    from ..ports.worktree_manager import WorktreeManager
    from .continuation_quick_validation import ContinuationQuickValidation
    from .continuation_runs import ContinuationRuns
    from .worktree_runnability import WorktreeRunnability

logger = logging.getLogger(__name__)


class ContinuationRunOpener:
    """Opens a continuation's run, or refuses and leaves nothing behind."""

    def __init__(
        self,
        *,
        attempts: "AttemptStore",
        worktrees: "WorktreeManager",
        working_copy: "WorkingCopy",
        runnability: "WorktreeRunnability",
        quick_validation: "ContinuationQuickValidation",
        session_output: "SessionOutput",
        runs: "ContinuationRuns",
        repo_root: Path,
    ) -> None:
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
        # The run owner, told about the checkout the moment it exists.
        self._runs = runs
        self._repo_root = repo_root

    def open(
        self, operation: LiveContinuation, descriptor: "ContinuationDescriptor"
    ) -> ContinuationRun | None:
        """Mint this operation's run: its worktree, its environment, its intent.

        Allocated once and registered with the owner immediately, so from the
        moment the checkout exists there is exactly one place that knows about
        it and exactly one place that will dispose of it.

        Quick-validation preparation sits between the run assets and the intent
        because it needs the first and is named by the second (#173): the run
        directory is where the evidence is written, and the completion record
        is what points the review exchange at it. A continuation has no coder
        turn to produce that evidence, and a reviewer told to trust a file
        nothing wrote answers about the missing file rather than about the
        code. A refusal here opens no run at all — see :meth:`_prepare_evidence`
        for what that costs and why it is not refunded.

        Returns:
            The registered run, or ``None`` when no run may open. ``None`` is
            never a degraded start: the caller advances nothing.
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
            session_name=continuation_worktree_name(operation),
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
                worktree_name=continuation_worktree_name(operation),
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
        reachable only from the refusals above :meth:`open`'s registration.
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
        spent, as this module's header says every refusal leaves it: a later
        pass may open another run only while #149's existing allowance lasts,
        and once it is gone the ordinary ``RUNS_EXHAUSTED`` derivation hands the
        candidate back to rework — where the launch provisioner escalates the
        same broken environment to ``needs-human``. There is no second counter
        and no escalation of its own.

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

        What a coder finds waiting after ``RUNS_EXHAUSTED`` is a durable
        artefact, not this log line and not the run directory: the discard
        below deletes every path the gate wrote inside the checkout, so the
        gate files a failing run's output into the primary checkout as it
        produces it (#94). It is filed under this candidate's
        ``(issue, head_sha)``, which is why the preparation is handed the issue
        key rather than deriving one.

        Returns:
            What the preparation produced, or ``None`` when no run may open.
        """
        prepared = self._quick_validation.prepare(
            worktree=worktree,
            run_assets=assets,
            issue_key=operation.issue.key,
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


def continuation_worktree_name(operation: LiveContinuation) -> str:
    """A run-scoped name that cannot collide with the issue's own worktree."""
    return f"continuation-{operation.issue.number}-{operation.key.head_sha[:12]}"


__all__ = ["ContinuationRunOpener", "continuation_worktree_name"]
