"""The agent-side quick gate, and what a run of it found.

Split from :mod:`.validation`, which owns the orchestrator's cache-aware
:class:`~.validation.ValidationGate`, because this gate is a different owner
with a different contract: it runs unconditionally, consults no cache and no
durable evaluation history, and takes no attempt identity — so a run of it can
neither reuse a verdict nor file one.

That difference is why it now has two callers. It was the completion command's
gate alone (``coding-done`` runs it over the worktree the agent just finished
in); since #173 the continuation runs the same gate to produce the evidence a
coder turn would otherwise have produced for the first Reviewer. Both attach
the result to a run, which is why the passed/reason pair is converted to a
typed outcome here — once, on the result — rather than at each recording site.

What both gates share stays in :mod:`.validation`: the runner that executes a
command and produces a record, how a gate reads the commit it is about to
judge, and how it words a refusal.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..domain.artifact_contracts import (
    ValidationFailed,
    ValidationOutcome,
    ValidationPassed,
)
from ..domain.validation_profile import AGENT_GATE_SUITE
from ..infra.validation_profiles import ValidationGateContract
from ..ports import CommandRunner, WorkingCopy
from ..ports.session_output import ValidationRecord
from .gate_failure_diagnostics import CandidateGateDiagnostics
from .validation import (
    ValidationRecordStore,
    ValidationRunner,
    gate_failure_reason,
    read_gate_head_sha,
)

logger = logging.getLogger(__name__)


@dataclass
class AgentGateResult:
    """Result of an agent gate check."""

    passed: bool
    reason: str
    record: Optional[ValidationRecord] = None
    record_path: Optional[str] = None  # Path where validation record was written

    @property
    def outcome(self) -> ValidationOutcome:
        """What this run says the manifest's validation outcome now is.

        Here rather than at each recording site: both callers of this gate
        attach the result to a run, and two private conversions of "passed plus
        reason" into a typed outcome are how one of them starts recording a
        pass with the previous failure's reason still on it — the defect
        ``update_validation_outcome`` exists to make impossible one layer down.
        """
        if self.passed:
            return ValidationPassed()
        return ValidationFailed(reason=self.reason or "validation failed")


class AgentGate:
    """Validation gate for agent completion.

    Unlike :class:`~.validation.ValidationGate` this runs unconditionally (no
    cache) and records the result for informational purposes.

    It runs the profile's *quick* contract and records its own suite label, so
    a reader can tell an agent-side run from the orchestrator's own quick gate
    while both remain honestly labelled as the quick contract. Handing it a
    publish contract is rejected rather than mislabelled (#25).
    """

    SUITE_NAME = AGENT_GATE_SUITE

    def __init__(
        self,
        worktree: Path,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
        contract: ValidationGateContract,
        failure_diagnostics: CandidateGateDiagnostics | None = None,
    ):
        """Initialize agent gate for a worktree.

        Args:
            worktree: Path to the git worktree
            contract: The profile's quick contract. An unconfigured contract
                (no ``cmd``) means the gate is disabled.
            failure_diagnostics: Durable destination for a failed run's output
                (#94), passed straight through to the runner that produces it,
                exactly as :class:`~.validation.ValidationGate` passes its own.
                ``None`` where the caller holds no candidate identity — which
                is every agent-side caller, and was every caller of this gate
                until the continuation (#173). The gate does not consult it: an
                artefact this gate wrote is evidence for a human, never an
                input to a later decision.

        Raises:
            ValueError: when handed a contract other than the quick one.
        """
        if not contract.is_quick:
            raise ValueError(
                "AgentGate runs the quick contract; "
                f"got {contract.kind.value!r}"
            )
        self.worktree = worktree
        self.command_runner = command_runner
        self.working_copy = working_copy
        self.contract = contract
        self.command = contract.cmd
        self.timeout_seconds = contract.timeout_seconds
        self.profile = contract.profile
        self.store = ValidationRecordStore(worktree, contract.kind)
        self.runner = ValidationRunner(
            self.store, command_runner, failure_diagnostics=failure_diagnostics
        )

    def run(self, session_output_dir: Path) -> AgentGateResult:
        """Run the agent gate validation.

        Unlike ValidationGate.check(), this always runs the validation
        (no cache lookup) because we want to capture the result at
        the specific point in time when the completion command is called.

        Args:
            session_output_dir: Directory to write validation output

        Returns:
            AgentGateResult with validation status
        """
        # Gate disabled if no command
        if not self.command:
            logger.debug("Agent gate disabled (no command configured)")
            return AgentGateResult(
                passed=True,
                reason="Agent gate disabled (no command configured)",
            )

        # Get HEAD SHA
        head_sha = read_gate_head_sha(self.working_copy, self.worktree)
        if not head_sha:
            return AgentGateResult(
                passed=False,
                reason="Cannot determine HEAD SHA",
            )

        # Run validation
        logger.info(
            "Agent gate: running validation for %s [profile=%s]",
            head_sha[:8],
            self.profile,
        )
        record = self.runner.run(
            suite=self.SUITE_NAME,
            head_sha=head_sha,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            session_output_dir=session_output_dir,
            profile=self.profile,
        )

        # Get the path where the record was written
        record_path = str(self.store.get_record_path(head_sha))

        if record.passed:
            return AgentGateResult(
                passed=True,
                reason=f"Validation passed for {head_sha[:8]}",
                record=record,
                record_path=record_path,
            )
        else:
            return AgentGateResult(
                passed=False,
                reason=gate_failure_reason(record),
                record=record,
                record_path=record_path,
            )


__all__ = ["AgentGate", "AgentGateResult"]
