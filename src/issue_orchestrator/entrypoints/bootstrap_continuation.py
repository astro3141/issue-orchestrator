"""Composition of the production control continuation's ports (#149).

Extracted from ``bootstrap`` for the same reason ``bootstrap_revalidation`` is:
the root stays navigable, and the wiring for one capability sits in one place.
Owns nothing but the wiring.

The continuation OWNER itself is not built here, and that is deliberate. It
holds the lock that serialises live-truth derivation against claim creation, so
it must live exactly as long as the engine does and be bound to the engine's
live ``OrchestratorState`` — which is the facade's, not this container's. So the
root supplies the durable ports, and the facade assembles the owner around them
(:attr:`~...infra.orchestrator.Orchestrator._control_continuation`).
"""

from __future__ import annotations

from ..infra.config import Config
from ..ports.continuation_ports import ContinuationPorts


def build_continuation_ports(config: Config) -> ContinuationPorts:
    """The durable ports the continuation owner is assembled around.

    Both composition roots call this rather than assembling the ports
    themselves, so the production root and the testing root cannot wire
    differently-shaped continuations — the divergence ``build_publication_gate``
    exists to prevent, one layer up.
    """
    from ..execution.control_operation_ownership_store import (
        SqliteControlOperationOwnershipStore,
    )
    from ..execution.run_review_verdict_bindings import RunReviewVerdictBindings

    return ContinuationPorts(
        # Leases live in the orchestrator-owned state directory, outside every
        # agent-writable worktree, beside the pending-work ledger (#146).
        ownership_store=SqliteControlOperationOwnershipStore.for_repo(
            config.repo_root
        ),
        # Reads back the exact-SHA verdict a finished exchange bound, so the
        # continuation can promote it into durable truth before the run's
        # worktree is discarded (#149).
        review_verdicts=RunReviewVerdictBindings(),
    )


__all__ = ["build_continuation_ports"]
