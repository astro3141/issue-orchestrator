"""Producing the continuation's first-Reviewer quick-validation record (#173).

The ordinary path's first Reviewer is handed validation evidence its **coder
turn** produced: ``coding-done`` runs the profile's quick contract through
:class:`~.validation.AgentGate`, writes the record into the run directory, and
names it on the completion record. Everything downstream is a data dependency
on that one file — the completion record starts the review exchange, the pair
mirror copies the named record into pair and run scope, and a round cannot
advance while ``require_validation`` is on and the mirrored record is missing,
stale or failing.

**A continuation has no coder turn.** Its completion record is synthesised from
the durable descriptor, whose fields are the agent's recorded intent and
nothing else. Without this preparation the exchange reaches the reviewer
pointing at a file nothing wrote, and the reviewer — told to trust that file —
answers ``changes_requested`` about a missing file rather than about the code.

So the evidence is *genuinely produced*, by system preparation, with no model
or coder turn:

* **The owner is the existing one.** :class:`~.validation.AgentGate` runs the
  profile's quick contract, under the suite label an agent-side run carries,
  into the run directory the mirror already reads. Nothing here runs a command
  of its own, and nothing here writes a record.
* **Nothing is reused and nothing is synthesised.** ``AgentGate`` runs
  unconditionally: it consults no cache and no durable evaluation history, so a
  candidate whose past verdict survives while its record died with the worktree
  gets a fresh run rather than a record invented from a receipt. The
  preparation either executes or refuses.
* **Nothing is retyped.** The gate carries no attempt identity, so it files no
  durable evaluation: this run cannot append to — or displace anything in — the
  candidate's publication history.
* **The candidate is left exactly as it was found.** The same
  :class:`~.candidate_integrity.CandidateIntegrity` reads
  :class:`~.worktree_runnability.WorktreeRunnability` takes around the
  operator's recipe are taken around the gate. On top of that, the record names
  the commit it ran at, which is the binding the exchange's mirror re-checks
  against the coder worktree's current HEAD before every round — evidence that
  does not name the candidate reads as stale there and refuses the round rather
  than passing silently.

A refusal is returned, not raised: the caller decides what it costs, and for
the continuation it costs the whole run — no run is opened, no exchange starts,
no pull request is created, and the durable record the pass did not change
derives the same phase next tick until the existing run allowance is gone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain.session_run import SessionRunAssets
from ..domain.validation_profile import ValidationGateKind
from ..infra.validation_profiles import UnknownValidationProfileError
from ..ports.command_runner import CommandRunner
from ..ports.working_copy import WorkingCopy
from .candidate_integrity import CandidateIntegrity
from .publication_gate import RunValidationContracts
from .validation import AgentGate

logger = logging.getLogger(__name__)

QUICK_VALIDATION_OPERATION = "quick validation"
"""How an altered candidate is attributed when the quick gate is what ran."""


@dataclass(frozen=True, slots=True)
class PreparedQuickValidation:
    """The evidence this preparation produced, or its honest absence.

    ``record_path`` is ``None`` in exactly one case: this run's frozen profile
    defines no quick contract, so there was no command to run and there is no
    record to name. That is what an ordinary coder turn writes in such a
    repository too — a completion record naming no validation evidence — and
    whether a review may proceed without it stays ``require_validation``'s
    question, unchanged.
    """

    record_path: Path | None


@dataclass(frozen=True, slots=True)
class RefusedQuickValidation:
    """Why this candidate's quick evidence could not be produced."""

    reason: str


class ContinuationQuickValidation:
    """Runs the configured quick contract for a continuation's own run."""

    def __init__(
        self,
        *,
        contracts: RunValidationContracts,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
    ) -> None:
        """Compose the gate's collaborators; decide nothing about them.

        ``contracts`` is the same resolver the publication gate uses to answer
        "which contract does this run execute": it reads the profile frozen
        onto the run's manifest when the run was created, so the continuation
        validates under the contract its descriptor recorded rather than
        whatever the current default is bound to today.
        """
        self._contracts = contracts
        self._command_runner = command_runner
        self._working_copy = working_copy
        self._integrity = CandidateIntegrity(
            working_copy, operation=QUICK_VALIDATION_OPERATION
        )

    def prepare(
        self, *, worktree: Path, run_assets: SessionRunAssets
    ) -> PreparedQuickValidation | RefusedQuickValidation:
        """Produce this run's quick-validation record, or say why it could not.

        Args:
            worktree: The continuation's provisioned checkout, standing at the
                exact candidate commit.
            run_assets: The run whose frozen profile selects the contract and
                whose directory receives the evidence.

        Returns:
            The produced record's path, or a refusal. A refusal is never a
            degraded start: the caller must open no run.
        """
        try:
            contract = self._contracts.contract_for_run(
                run_assets.run_dir, ValidationGateKind.QUICK
            )
        except UnknownValidationProfileError as retired:
            # The run froze a profile the current configuration no longer
            # defines. Substituting another contract would let this run claim
            # evidence for a gate it never executed.
            return RefusedQuickValidation(str(retired))
        if not contract.configured:
            logger.info(
                "[CONTINUATION] no quick contract configured [profile=%s]: this"
                " run names no validation evidence",
                contract.profile,
            )
            return PreparedQuickValidation(record_path=None)

        before = self._integrity.checkpoint(worktree)
        logger.info(
            "[CONTINUATION] preparing quick validation [profile=%s] %s in %s",
            contract.profile,
            contract.cmd,
            worktree,
        )
        result = AgentGate(
            worktree,
            command_runner=self._command_runner,
            working_copy=self._working_copy,
            contract=contract,
        ).run(session_output_dir=run_assets.run_dir)
        # Read whether or not the gate passed, and reported first: a failing
        # command and an altered candidate are two separate facts, and a
        # candidate this preparation moved or dirtied is the more serious one.
        altered = self._integrity.describe_change(worktree, before)
        if altered is not None:
            logger.error("[CONTINUATION] quick validation altered the candidate: %s", altered)
            return RefusedQuickValidation(altered)
        if not result.passed:
            return RefusedQuickValidation(result.reason)
        record_path = run_assets.validation_artifacts.record_path
        if not record_path.exists():
            # A pass the gate reported but left no record for. Nothing here
            # writes one: the exchange must read evidence a gate produced, and
            # a file this class authored would be exactly the fabrication the
            # leaf exists to rule out.
            return RefusedQuickValidation(
                f"the quick gate reported a pass but wrote no record at {record_path}"
            )
        return PreparedQuickValidation(record_path=record_path)


__all__ = [
    "QUICK_VALIDATION_OPERATION",
    "ContinuationQuickValidation",
    "PreparedQuickValidation",
    "RefusedQuickValidation",
]
