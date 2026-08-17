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

All of that evidence lives inside the coder worktree and dies with it. So
every run of the publish contract also files a durable verdict receipt on
``Attempt(issue, A)`` (#85) — see :mod:`.publication_verdict`. The gate is the
producer seam for that fact because it is the only thing that knows the
contract it just executed; a caller reconstructing the verdict from the
outcome would be a second place that decides what the gate decided.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain.issue_key import IssueKey
from ..domain.session_run import SessionRunAssets, ValidationArtifactPaths
from ..domain.validation_profile import ValidationGateKind
from ..infra.validation_profiles import (
    ValidationGateContract,
    ValidationProfile,
    ValidationProfileRegistry,
)
from ..ports import CommandRunner, WorkingCopy
from ..ports.attempt_store import AttemptStore
from ..ports.session_output import SessionOutput, ValidationRecord
from ..ports.validation_attempt_key_factory import ValidationAttemptKeyFactory
from .publication_verdict import PublicationVerdictReceipts
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

    @property
    def repository_has_publication_contract(self) -> bool:
        """Whether *any* profile in the registry defines a publish command.

        The registry-wide question, asked here because this class already owns
        "which contract does a run execute" and is the only collaborator the
        gate has to ask it of. Review admission asks the same registry the same
        question (:attr:`~..infra.validation_profiles.
        ValidationProfileRegistry.any_publish_command_configured`), so the two
        sides of #45's requirement cannot disagree about whether this
        repository gates publication at all.
        """
        return self._profiles.any_publish_command_configured


def publish_gate_output_dir(run_dir: Path) -> Path:
    """Where a run's publish-gate evidence is written."""
    return run_dir / PUBLISH_GATE_DIR_NAME


def build_publication_gate(
    *,
    session_output: SessionOutput,
    profiles: ValidationProfileRegistry,
    command_runner: CommandRunner,
    working_copy: WorkingCopy,
    attempt_store: AttemptStore,
    attempt_keys: ValidationAttemptKeyFactory,
) -> "PublicationGate":
    """The one way to assemble the publication gate.

    Both composition roots call this rather than spelling the wiring out
    themselves, so the production root and the testing root cannot build
    differently-shaped pipelines — the divergence that let simulated
    scenarios pass while production ran no publish contract at all (#25).

    ``attempt_store`` and ``attempt_keys`` are required, not optional: a gate
    built without them would run the publish contract and leave no durable
    trace of what it decided, which is the defect #85 exists to close.
    """
    return PublicationGate(
        contracts=RunValidationContracts(session_output, profiles),
        command_runner=command_runner,
        working_copy=working_copy,
        verdicts=PublicationVerdictReceipts(attempt_store, attempt_keys),
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
    issue exists to prevent. The verdict receipt this gate files on the
    attempt (#85) is a different slot with a different meaning: it states what
    this contract decided rather than pointing at a record, and the quick gate
    never writes it.
    """

    def __init__(
        self,
        *,
        contracts: RunValidationContracts,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
        verdicts: PublicationVerdictReceipts,
    ) -> None:
        self._contracts = contracts
        self._command_runner = command_runner
        self._working_copy = working_copy
        self._verdicts = verdicts

    def check(
        self,
        *,
        worktree: Path,
        run_assets: SessionRunAssets,
        issue_key: IssueKey | None,
    ) -> PublicationGateOutcome:
        """Run the publish contract for ``run_assets`` and decide publication.

        Args:
            worktree: The working copy the contract runs in.
            run_assets: The owned run whose frozen profile selects the contract.
            issue_key: The candidate's canonical issue identity, under which
                this run's verdict is filed durably. Required as an explicit
                argument — including when it is ``None`` — so a caller that has
                no canonical identity says so rather than omitting it. The
                manual-reprocess route is the ``None`` case: it holds only an
                issue *number* from a URL path, and deriving a key from a
                work-item snapshot is what #40 removed. The republish path
                carries the key on its durable locators instead.

        Returns:
            The outcome, including the evidence paths this gate wrote to.
            ``allowed`` is True when the publish command passed (or was reused
            from a passing record for this exact HEAD/command/profile), and
            when *no* profile in the repository configures a publish command —
            an explicit operator choice, not a silent skip. A run whose own
            profile configures none while another profile does is refused
            instead: see :meth:`_uncertifiable_candidate`.
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
            uncertifiable = self._uncertifiable_candidate(contract, run_assets)
            if uncertifiable is not None:
                return uncertifiable
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
        self._record_verdict(result.record, issue_key)
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

    def _uncertifiable_candidate(
        self,
        contract: ValidationGateContract,
        run_assets: SessionRunAssets,
    ) -> PublicationGateOutcome | None:
        """Refuse a candidate whose own profile can never certify it (#45).

        Whether a receipt is *produced* is decided by the run's own frozen
        profile: an unconfigured publish contract runs nothing and files
        nothing. Whether a receipt is *required* is decided by the repository:
        review admission demands one as soon as any profile defines a publish
        command, because a candidate's profile is not knowable from the PR it
        opened — the receipt is the only thing that would say.

        Those two granularities meet here, and only here, with both facts in
        hand. Left alone they produce the worst possible outcome: publication
        succeeds, the PR opens with the review trigger on it, and every scan
        and every launch refuses the review with ``publication_receipt_missing``
        forever, with no label, no terminal state and nothing to retry. So this
        fails closed instead, at the one moment an operator is watching: the
        completion is refused through the ordinary gate-failure path, wearing
        the ordinary refusal label and carrying a reason that names the profile
        and what to do about it.

        ``None`` — no refusal — is the honest answer in the two other shapes: a
        repository that gates nothing anywhere (no contract exists, so none can
        be missing), and a completion whose profile *does* define the contract,
        which never reaches here.
        """
        if not self._contracts.repository_has_publication_contract:
            return None
        reason = (
            f"Validation profile '{contract.profile}' defines no "
            "validation.publish.cmd, but this repository gates publication in "
            "another profile. A candidate published under this profile could "
            "never carry the publication receipt review admission requires, so "
            "it would open a pull request no review could ever be launched "
            f"for. Configure validation.publish.cmd for profile "
            f"'{contract.profile}', or remove it everywhere."
        )
        logger.error(
            "Publication gate refused #%s: %s", run_assets.run_id, reason
        )
        return PublicationGateOutcome(
            allowed=False,
            reason=reason,
            evidence=GateEvidence(
                record=None,
                paths=ValidationArtifactPaths.in_directory(
                    run_dir=run_assets.run_dir,
                    output_dir=publish_gate_output_dir(run_assets.run_dir),
                ),
            ),
        )

    def _record_verdict(
        self,
        record: ValidationRecord | None,
        issue_key: IssueKey | None,
    ) -> None:
        """File this run's verdict on the attempt, when there is one to file.

        A run with no record executed no contract, and "never gated" is the
        *absence* of a receipt, not a receipt saying nothing. Writing one here
        would make the one state a reader most needs to distinguish
        indistinguishable. Two causes reach this branch, and skipping the
        receipt is right for both:

        - no profile configures a publish command, so nothing ran and nothing
          will ever ask for a receipt (a profile that is alone in configuring
          none is refused before this, by :meth:`_uncertifiable_candidate`);
        - ``ValidationGate.check`` refused before running because it could not
          determine HEAD (``control.validation``). That is a refusal rather
          than an unconfigured gate, but it has no commit — so there is no
          candidate ``A`` to file a verdict under in the first place.

        Failures on both sides are recorded: a FAIL and a timeout are facts
        about A exactly as a PASS is, and a reader that only ever saw passes
        could not tell a refusal from a gate that never ran.
        """
        if record is None:
            return
        if issue_key is None:
            logger.warning(
                "Publish verdict not durably recorded: no canonical issue "
                "identity for %s@%s; Attempt(issue, A) keeps no receipt for "
                "this run",
                record.suite,
                record.head_sha[:12],
            )
            return
        self._verdicts.record(issue_key=issue_key, record=record)
