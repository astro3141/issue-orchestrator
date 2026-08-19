"""Turning one continuation run's result into the facts that settle it (#149).

The analogue is :mod:`.publish_retry_finalize`, and the analogy is exact. Both
are routes with no session and therefore no session completion; both re-enter
the ordinary completion pipeline; and both would, without a named finalizer,
leave the outcome of their own run in a :class:`~.completion_types.ProcessingResult`
that nothing reads. ``RetrySuccessFinalizer`` converts that result into the
label actions and review routing that settle a publish retry. This converts it
into the durable settlement and the board signal that settle a continuation.

The two differ in exactly one place, and the difference is the whole reason
this is a second finalizer rather than a second caller of the first:

* The publish-retry route settles ONTO the board — ``pr-pending`` is both what
  it writes and what the rest of the system reads back. A continuation cannot
  settle that way alone, because whether it is still live is decided from the
  attempt record on every reconciliation, seconds apart, from a board snapshot
  that may predate the label by a whole refresh. Settling on a durable fact
  makes termination immediate and cache-independent.
* The publish-retry route may still owe a code review; a continuation never
  does. Its exchange is reviewer-first and has already run against this exact
  commit, so ``should_queue_pr_review`` would refuse a review anyway — routing
  through ``discovered_reviews`` to reach ``pr-pending`` would ask the planner
  to queue a review the run already performed.

**Order is load-bearing.** The board signal is applied FIRST and the durable
settlement second. A failed label write therefore settles nothing, and the next
reconciliation derives the same live phase and tries again (finding, and
reusing, the pull request that already exists) — costly, and the right cost.
The reverse order would terminate the operation while the board still showed no
pull request, and reconciliation would then release the lane back to ordinary
work for an issue whose approved PR is already open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.continuation_settlement import (
    ContinuationSettlement,
    ContinuationSettlementKind,
)
from .actions import AddLabelAction

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.continuation_descriptor import ContinuationDescriptor
    from ..ports.attempt_store import AttemptStore
    from .completion_types import ProcessingResult
    from .continuation_live_truth import LiveContinuation

logger = logging.getLogger(__name__)


class _ActionApplier(Protocol):
    """The one action this finalizer applies, and nothing else."""

    def apply(self, action: AddLabelAction) -> Any: ...


@dataclass(frozen=True, slots=True)
class _Settling:
    """A settlement about to be recorded, decided before anything is written."""

    settlement: ContinuationSettlement
    reason: str


class ContinuationFinalizer:
    """Records what one continuation run produced, so it never runs again."""

    def __init__(
        self,
        *,
        attempts: "AttemptStore",
        action_applier: _ActionApplier,
        pr_pending_label: str,
    ) -> None:
        self._attempts = attempts
        self._action_applier = action_applier
        self._pr_pending_label = pr_pending_label

    def finalize(
        self, operation: "LiveContinuation", result: "ProcessingResult"
    ) -> ContinuationSettlement | None:
        """Settle ``operation`` from what its own run produced, or settle nothing.

        Returns:
            The settlement recorded, or ``None`` when the run discharged
            nothing. ``None`` is not a failure: it leaves the recorded intent
            undischarged, which is what keeps a run that could not produce the
            pull request it was asked for retryable on the next pass.
        """
        settling = _decide(operation.attempt.continuation_descriptor, result)
        if settling is None:
            logger.info(
                "[CONTINUATION] %s settled nothing this run: success=%s pr=%s",
                operation.key,
                result.success,
                result.pr_url or "none",
            )
            return None
        settlement = settling.settlement
        if settlement.opened_pull_request:
            self._announce_pull_request(operation)
        self._attempts.update(
            operation.attempt.key,
            lambda attempt: attempt.with_continuation_settlement(settlement),
        )
        logger.info(
            "[CONTINUATION] %s settled: %s (%s)",
            operation.key,
            settlement.kind.value,
            settling.reason,
        )
        return settlement

    def _announce_pull_request(self, operation: "LiveContinuation") -> None:
        """Put the board into the state a pull request puts every issue into.

        One label action, applied directly rather than routed through
        ``discovered_reviews``: the planner reads that fact as "queue the
        configured code review", and this run's reviewer-first exchange has
        already reviewed this exact commit. This is the same branch
        ``RetrySuccessFinalizer`` takes when its own routing decides no review
        is owed.

        Raises whatever the applier reports. The settlement write below is
        deliberately downstream of it: an unannounced pull request must leave
        the operation retryable rather than terminate it silently.
        """
        result = self._action_applier.apply(
            AddLabelAction(
                issue_number=operation.issue.number,
                label=self._pr_pending_label,
                issue_key=str(operation.issue.key.stable_id()),
                reason="control continuation created the recorded pull request",
            )
        )
        if not result.success:
            raise RuntimeError(
                result.error
                or f"could not label issue {operation.issue.number} awaiting-merge"
            )


def _decide(
    descriptor: "ContinuationDescriptor | None", result: "ProcessingResult"
) -> _Settling | None:
    """What ``result`` discharged, decided from the result and the intent alone.

    Pure, and separate from the writes, so the question "is this run finished"
    is answered once and before anything external is touched.
    """
    if descriptor is None:
        # Unreachable through live truth, which refuses a descriptor-less
        # candidate before it can be owned. Settling nothing is the safe half
        # of an impossible state: it records no claim about intent nobody made.
        return None
    if result.is_non_terminal:
        # The review exchange is still running in the background, or a
        # post-review failure was rerouted into rework. Either way completion
        # has NOT finished for this record, and the pipeline itself says so.
        return None
    now = datetime.now(timezone.utc).isoformat()
    pr_url = (result.pr_url or "").strip()
    if pr_url:
        return _Settling(
            settlement=ContinuationSettlement(
                kind=ContinuationSettlementKind.PULL_REQUEST_OPENED,
                settled_at=now,
                pr_url=pr_url,
            ),
            reason="the run created the recorded pull request",
        )
    if not descriptor.creates_pr and result.success:
        # The intent asked for no pull request, so a clean run owes nothing
        # more. Without this the operation would stay live on a PASS forever,
        # waiting for a PR that was never requested.
        return _Settling(
            settlement=ContinuationSettlement(
                kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
                settled_at=now,
            ),
            reason="the recorded intent asked for no pull request",
        )
    return None


__all__ = ["ContinuationFinalizer"]
