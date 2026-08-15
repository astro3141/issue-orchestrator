"""Durable latch for publication-gate refusals that were never recorded (#51).

The publication-gate verdict's primary record is the issue label
(``control/publication_authority.py``). When that write does not commit, the
refusal is still real and must still withhold review — and it must keep doing
so after the orchestrator restarts, because nothing else ever recorded it.

This port is deliberately the smallest thing that can express that:

* it is keyed by ISSUE, never by candidate. Which candidate ultimately holds
  review authority is a separate question (#50) and this latch does not touch
  it;
* it only ever *adds* a refusal and *removes one it added*. It can never grant
  review, so a reader that consults it can only become more conservative;
* it is NOT a second source of truth. A latched issue reads as refused exactly
  as the label would; an issue the label already refuses needs no latch.

Implemented by the orchestrator-owned pending-work ledger adapter, which
already lives outside every agent-writable worktree and is already covered by
the startup integrity checks and backups this latch needs.
"""

from __future__ import annotations

from typing import Protocol


class PublicationRefusalLatch(Protocol):
    """Issues whose publication-gate refusal could not be recorded remotely."""

    def latch_publication_refusal(self, issue_number: int) -> None:
        """Record that this issue's refusal is not provably recorded.

        Idempotent: the gate may refuse the same issue on every attempt, and
        re-latching must not accumulate rows or change what is already held.
        """
        ...

    def release_publication_refusal(self, issue_number: int) -> None:
        """Drop the latch because the verdict is now settled elsewhere.

        Only a committed settlement may call this — a granted candidate or a
        refusal the label now proves on its own. A no-op when nothing is
        latched.
        """
        ...

    def latched_publication_refusals(self) -> frozenset[int]:
        """Every issue still latched, for rebuilding the hold after a restart."""
        ...


__all__ = ["PublicationRefusalLatch"]
