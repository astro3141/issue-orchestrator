"""The one hydration path that cannot skip reconciliation (#149).

``ControlOperationExclusions`` is a projection, and until something publishes
it, it excludes nothing. #148 measured the consequence exactly: three places
hydrate the queue and then evaluate eligibility, none of them reconciled first,
so the permissive window was total — ordinary work could launch on an issue a
terminal-less control operation was already running.

The fix is an ordering, and an ordering that three call sites must each
remember is not enforced at all. So the sequence lives in a type instead::

    load durable continuation truth
        -> derive live_operations
        -> reconcile #146 ownership
        -> publish ControlOperationExclusions
        -> advance the operations this engine owns
        -> only then evaluate Actor/rework eligibility

Every method here does the first five before it does the sixth, and the sixth
is the ONLY thing it delegates to :class:`~.queue_cache.QueueCache`. A caller
holding this owner cannot express "hydrate without reconciling"; a caller
holding the cache directly is not hydrating from a refreshed board, which is
the distinction the two types draw.

``QueueCache`` is untouched and still reads only the published projection. No
raw ownership-store read reaches the scheduler, here or anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .continuation_live_truth import ContinuationReconciliation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ports.issue import Issue
    from .continuation_live_truth import ContinuationLiveReading, ContinuationLiveTruth
    from .continuation_runner import ControlContinuationRunner
    from .control_operation_ownership import ControlOperationOwnership
    from .queue_cache import QueueCache, QueueMutationOutcome

logger = logging.getLogger(__name__)


class ControlContinuation:
    """Reconciles control-operation ownership, advances it, then hydrates."""

    def __init__(
        self,
        ownership: "ControlOperationOwnership",
        live_truth: "ContinuationLiveTruth",
        runner: "ControlContinuationRunner",
    ) -> None:
        self._ownership = ownership
        self._live_truth = live_truth
        self._runner = runner

    def reconcile(self, board: Sequence["Issue"]) -> ContinuationReconciliation:
        """Publish the exclusions ``board``'s durable truth implies, then act.

        Args:
            board: The COMPLETE set of in-scope issues this engine knows about.
                Reconciliation releases every lease the derived set does not
                name, so a partial board would report other issues' running
                operations as finished and free them.

        Derivation happens inside the ownership owner's lock
        (:meth:`~.control_operation_ownership.ControlOperationOwnership.reconcile_derived`),
        which is what makes a stale snapshot unable to release a newer claim.

        Execution follows publication rather than preceding it: an operation is
        advanced only after the exclusion that protects it is in force, so
        there is no interval in which the work has begun and ordinary rework
        could still be found eligible.

        An unreadable durable record publishes NOTHING, advances nothing, and
        returns the projection already standing. That is the fail-closed
        direction: the standing projection was reconciled against a set that
        WAS readable, so keeping it can only keep an exclusion, never invent or
        drop one. Overwriting it with "nothing is live" would free every
        running operation on the strength of a broken instrument.
        """
        reading = self._read(board)
        if not reading.readable:
            return ContinuationReconciliation(
                exclusions=self._ownership.exclusions, readable=False
            )
        reconciliation = ContinuationReconciliation(
            exclusions=self._ownership.reconcile_derived(lambda: reading.keys),
            operations=reading.operations,
        )
        self._runner.advance(reconciliation)
        return reconciliation

    def hydrate_queue(
        self, cache: "QueueCache", board: Sequence["Issue"]
    ) -> list["Issue"]:
        """Reconcile over ``board``, then replace the queue from it."""
        self.reconcile(board)
        return cache.replace_from_refresh(list(board))

    def hydrate_issues(
        self,
        cache: "QueueCache",
        issues: Sequence["Issue"],
        *,
        board: Sequence["Issue"],
    ) -> list[tuple["Issue", "QueueMutationOutcome"]]:
        """Reconcile over ``board`` once, then upsert each refreshed issue.

        ``board`` is separate from ``issues`` and not derived from them: the
        eligibility questions are about a few issues, but the reconciliation
        that must precede them is about all of them. Passing only the upserted
        issues would release every other issue's live operation on the way in.

        Reconciliation happens exactly once, and it happens even when ``issues``
        is empty. An empty upsert list is not an empty hydration: the caller is
        still about to evaluate eligibility over the board it just read, and a
        startup with nothing to re-add is precisely the run where a lease
        written before the crash is the only surviving record that something is
        still running.
        """
        self.reconcile(board)
        return [(issue, cache.upsert_refreshed_issue(issue)) for issue in issues]

    def _read(self, board: Sequence["Issue"]) -> "ContinuationLiveReading":
        reading = self._live_truth.read(board)
        if not reading.readable:
            logger.warning(
                "[CONTINUATION] keeping the standing exclusion projection: %s",
                reading.detail,
            )
        return reading


__all__ = ["ControlContinuation"]
