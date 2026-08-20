"""Composition of the production control continuation's ports (#149).

Extracted from ``bootstrap`` for the same reason ``bootstrap_revalidation`` is:
the root stays navigable, and the wiring for one capability sits in one place.
Owns nothing but the wiring.

The continuation OWNER itself is not built here, and that is deliberate. It
holds the lock that serialises live-truth derivation against claim creation, so
it must live exactly as long as the engine does and be bound to the engine's
live ``OrchestratorState`` — which is the facade's, not this container's. So the
root supplies the durable ports, and
:func:`~..control.continuation_scheduling.build_control_continuation` assembles
the owner around them from the facade's state
(:attr:`~...infra.orchestrator.Orchestrator._control_continuation`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..infra.config import Config
from ..ports.continuation_ports import ContinuationPorts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..control.continuation_quick_validation import ContinuationQuickValidation
    from ..ports.command_runner import CommandRunner
    from ..ports.session_output import SessionOutput
    from ..ports.working_copy import WorkingCopy


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


def build_continuation_quick_validation(
    config: Config,
    *,
    session_output: "SessionOutput",
    command_runner: "CommandRunner",
    working_copy: "WorkingCopy",
) -> "ContinuationQuickValidation":
    """The one way to assemble the continuation's quick-validation preparation.

    Assembled here for the reason ``build_publication_revalidation`` is: the
    step runs for a candidate whose own session — and the coder turn that would
    otherwise have produced this evidence — is gone, so it belongs to the
    continuation's composition rather than to the completion pipeline's.

    The contract resolver is the same
    :class:`~..control.publication_gate.RunValidationContracts` the publication
    gate is built with, over the registry rebuilt from the current config, so
    "which contract does this run execute" has one answer however it is asked.
    What is deliberately NOT supplied is an attempt store: the gate this
    composes is the agent-side one, and a gate given a candidate identity would
    file a durable evaluation for a run that exists to hand one reviewer a
    file (#173).

    The two destinations for what the gate produces are the existing owners,
    for the same reason the gate is: ``RunEvidenceRecorder`` is what an agent's
    own ``coding-done`` records its gate result through, and
    :class:`~..control.gate_failure_diagnostics.GateFailureDiagnostics` is
    where the publication gate files a failing run's output so it outlives the
    worktree (#94). The failures directory is rooted in ``config.repo_root``
    and not in the run's checkout, which is the whole point: this caller
    deletes that checkout the moment its gate refuses.
    """
    from ..control.continuation_quick_validation import ContinuationQuickValidation
    from ..control.gate_failure_diagnostics import GateFailureDiagnostics
    from ..control.publication_gate import RunValidationContracts
    from ..execution.run_evidence import RunEvidenceRecorder
    from ..infra.validation_junit_paths import configured_validation_junit_xml_paths

    return ContinuationQuickValidation(
        contracts=RunValidationContracts(
            session_output, config.validation_profiles()
        ),
        command_runner=command_runner,
        working_copy=working_copy,
        evidence=RunEvidenceRecorder(session_output),
        diagnostics=GateFailureDiagnostics(config.repo_root),
        # The operator's configured report paths, as ``SessionController``
        # resolves them for the ordinary path's gate — read from the Config the
        # engine was launched with, never from the continuation checkout's
        # environment, which belongs to no session.
        junit_xml_paths=configured_validation_junit_xml_paths(config),
    )


__all__ = ["build_continuation_ports", "build_continuation_quick_validation"]
