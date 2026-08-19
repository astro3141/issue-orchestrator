"""Owner for the cleanup-fact queues a session completion writes.

Recording is one half: the completion handoff files the cleanup a finished
session earned. Consumption is the other: once that cleanup has actually been
carried out, the fact must leave the queue or it is replanned forever. Both
halves mutate the same two collections, so they share one owner rather than
letting callers reach into ``state.pending_cleanups`` / ``state.immediate_cleanups``.
"""

from collections.abc import MutableSequence

from ..domain.models import (
    ImmediateCleanup,
    OrchestratorState,
    PendingCleanup,
    Session,
    SessionStatus,
)
from .completion_handler import CleanupDecision, CleanupDisposition


class CompletionCleanupStateOwner:
    """Own cleanup-queue mutation for the facts a completion produces."""

    def __init__(self, state: OrchestratorState) -> None:
        self._pending: MutableSequence[PendingCleanup] = state.pending_cleanups
        self._immediate: MutableSequence[ImmediateCleanup] = state.immediate_cleanups

    def record(
        self,
        decision: CleanupDecision,
        session: Session,
        effective_status: SessionStatus,
    ) -> None:
        """Record exactly the fact selected by the completion decision owner."""
        if effective_status is not SessionStatus.COMPLETED:
            self._record_immediate(session, effective_status)
            return

        if decision.disposition is CleanupDisposition.DEFERRED:
            assert decision.pending_cleanup is not None
            self._pending.append(decision.pending_cleanup)
            return

        if decision.disposition is CleanupDisposition.IMMEDIATE:
            self._record_immediate(session, effective_status)
            return

        if decision.disposition is CleanupDisposition.NONE:
            return

        raise ValueError(f"Unhandled cleanup disposition: {decision.disposition!r}")

    def discard_immediate(self, issue_number: int, worktree_path: str) -> None:
        """Drop the immediate cleanup whose disposal has already been carried out.

        Ordinary planning ticks consume these facts wholesale at end of tick
        (see ``tech_lead_artifact_retention.clear_discovered_facts``), but a
        paused tick clears nothing — it consumes exactly what it disposed, or
        the same disposal would be re-attempted every tick and would reappear
        as a duplicate the moment the engine resumed (#167).
        """
        remaining = [
            cleanup
            for cleanup in self._immediate
            if not (
                cleanup.issue_number == issue_number
                and cleanup.worktree_path == worktree_path
            )
        ]
        # In place: the collection is shared live state, and other owners hold
        # references to the same list.
        self._immediate[:] = remaining

    def _record_immediate(
        self,
        session: Session,
        effective_status: SessionStatus,
    ) -> None:
        self._immediate.append(
            ImmediateCleanup(
                issue_number=session.issue.number,
                terminal_id=session.terminal_id,
                worktree_path=str(session.worktree_path),
                reason=effective_status.value,
                # Disposable tech-lead investigation worktrees are removed
                # on completion regardless of ordinary cleanup settings.
                scratch_worktree=session.scratch_worktree,
            )
        )
