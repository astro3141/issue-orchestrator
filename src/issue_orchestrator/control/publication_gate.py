"""The orchestrator's single pre-publication validation owner (#25).

Two things live here, both answers to "which contract does *this run* execute":

* :class:`RunValidationContracts` — reads the profile frozen into a run's
  manifest and hands back the typed contract for a requested gate kind. One
  owner for a question the session controller and the completion processor
  both have to ask; before this, each resolved the profile itself and then
  reached into ``profile.quick`` at its own gate-construction site.
* :class:`PublicationGate` — runs the profile's ``publish`` contract before
  the orchestrator publishes, and records the result as ``publish_gate``.

Why this exists at all: ``CompletionProcessor`` has had a publish-gate seam
for a long time, but composition never built one, so ``validation.publish.cmd``
was never executed anywhere in the orchestrator path. Meanwhile the session
controller's quick gate stamped ``suite=publish_gate`` onto records of the
*quick* command. The evidence said the publish contract had run; nothing had.

Evidence isolation is part of the contract. The publish gate writes into its
own ``publish-gate/`` directory under the run, because the quick gate that
runs later in the same tick writes ``validation-record.json`` and the
validation logs into the run directory root. Sharing that location would let
the quick run overwrite the only proof of what the publish contract did — the
same conflation through a different door.

Isolation only holds if the gate's *readers* look in the same place, so
:class:`PublicationGateOutcome` carries the evidence paths out with the
decision. Callers attaching the result to a run must not name those paths
themselves: doing so is how the run's manifest came to point at the quick
gate's logs while carrying the publish gate's record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain.session_run import SessionRunAssets, ValidationArtifactPaths
from ..domain.validation_profile import ValidationGateKind
from ..infra.validation_profiles import (
    ValidationGateContract,
    ValidationProfile,
    ValidationProfileRegistry,
)
from ..ports import CommandRunner, WorkingCopy
from ..ports.session_output import SessionOutput, ValidationRecord
from .validation import GateEvidence, ValidationGate

logger = logging.getLogger(__name__)

PUBLISH_GATE_DIR_NAME = "publish-gate"
"""Run-directory subdirectory holding publish-gate evidence."""


@dataclass(frozen=True, slots=True)
class PublicationGateOutcome:
    """The publication gate's decision, and the proof it left behind.

    ``evidence`` is not optional and not a caller's guess: the gate that ran
    the command is the only thing that knows which directory it wrote to, so
    the decision, the record and the artifact locations travel as one value
    (#25). A caller cannot attach the record without the paths it came with.
    """

    allowed: bool
    reason: str
    evidence: GateEvidence
    cache_hit: bool = False

    @property
    def record(self) -> ValidationRecord | None:
        """The record the run produced, or ``None`` if it ran no command."""
        return self.evidence.record


class RunValidationContracts:
    """Resolves the validation contract a run executes, from durable run state.

    The profile is read from the run directory's manifest — written when the
    run was created — so a restarted orchestrator, a rework round and a retry
    all validate against the same contract the run was launched under.

    A run created before profiles existed, or by a launch path that did not
    record one, reads back as the default profile. A run naming a profile the
    current config no longer defines raises, because substituting another
    contract would let the run claim it satisfied a gate it never executed.
    """

    def __init__(
        self,
        session_output: SessionOutput,
        profiles: ValidationProfileRegistry,
    ) -> None:
        self._session_output = session_output
        self._profiles = profiles

    def profile_for_run(self, run_dir: Path) -> ValidationProfile:
        """The validation profile frozen for this run.

        Raises:
            UnknownValidationProfileError: when the recorded profile is gone.
        """
        manifest = self._session_output.read_manifest(run_dir) or {}
        recorded = manifest.get("validation_profile")
        return self._profiles.resolve(
            recorded if isinstance(recorded, str) and recorded else None
        )

    def contract_for_run(
        self, run_dir: Path, kind: ValidationGateKind
    ) -> ValidationGateContract:
        """The typed contract this run executes for ``kind``."""
        return self.profile_for_run(run_dir).contract(kind)


def publish_gate_output_dir(run_dir: Path) -> Path:
    """Where a run's publish-gate evidence is written."""
    return run_dir / PUBLISH_GATE_DIR_NAME


def build_publication_gate(
    *,
    session_output: SessionOutput,
    profiles: ValidationProfileRegistry,
    command_runner: CommandRunner,
    working_copy: WorkingCopy,
) -> "PublicationGate":
    """The one way to assemble the publication gate.

    Both composition roots call this rather than spelling the wiring out
    themselves, so the production root and the testing root cannot build
    differently-shaped pipelines — the divergence that let simulated
    scenarios pass while production ran no publish contract at all (#25).
    """
    return PublicationGate(
        contracts=RunValidationContracts(session_output, profiles),
        command_runner=command_runner,
        working_copy=working_copy,
    )


class PublicationGate:
    """Runs the configured publish contract before the orchestrator publishes.

    The orchestrator is the only authority that may publish an agent's work to
    the remote, and this is the gate that authority runs. It executes the
    *run's own* frozen profile — not a command captured at composition time —
    so a rework round and the run it reworks are held to the same contract.

    Caching is by HEAD SHA + command + profile within the publish record
    store. Attempt-scoped caching is deliberately not used here: an attempt
    carries one validation record path, and letting the publish gate read or
    write it would put quick and publish results in one slot — the reuse this
    issue exists to prevent.
    """

    def __init__(
        self,
        *,
        contracts: RunValidationContracts,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
    ) -> None:
        self._contracts = contracts
        self._command_runner = command_runner
        self._working_copy = working_copy

    def check(
        self,
        *,
        worktree: Path,
        run_assets: SessionRunAssets,
    ) -> PublicationGateOutcome:
        """Run the publish contract for ``run_assets`` and decide publication.

        Returns:
            The outcome, including the evidence paths this gate wrote to.
            ``allowed`` is True when the publish command passed (or was reused
            from a passing record for this exact HEAD/command/profile), and
            when the run's profile configures no publish command at all — an
            explicit operator choice, validated at config load, not a silent
            skip.
        """
        contract = self._contracts.contract_for_run(
            run_assets.run_dir, ValidationGateKind.PUBLISH
        )
        if contract.configured:
            logger.info(
                "Publication gate: running publish contract [profile=%s] %s",
                contract.profile,
                contract.cmd,
            )
        else:
            logger.debug(
                "Publication gate: no publish command configured [profile=%s]",
                contract.profile,
            )
        gate = ValidationGate(
            worktree=worktree,
            command_runner=self._command_runner,
            working_copy=self._working_copy,
            contract=contract,
        )
        # One local for both: the directory the gate runs into is the
        # directory the outcome reports. There is no second expression that
        # could name a different one.
        output_dir = publish_gate_output_dir(run_assets.run_dir)
        result = gate.check(session_output_dir=output_dir)
        return PublicationGateOutcome(
            allowed=result.allowed,
            reason=result.reason,
            evidence=GateEvidence(
                record=result.record,
                paths=ValidationArtifactPaths.in_directory(
                    run_dir=run_assets.run_dir, output_dir=output_dir
                ),
            ),
            cache_hit=result.cache_hit,
        )
