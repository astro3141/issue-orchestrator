"""Producing a continuation's first-Reviewer quick-validation record (#173).

The preparation on its own, at the ports it composes. What the runner does
*with* a refusal — discard the checkout, open no run, leave the durable history
alone — is proved in ``test_continuation_runner``; what is proved here is the
narrower claim the runner depends on: the evidence is produced by the existing
configured quick-validation owner, it is never reused from a durable verdict or
synthesised from a receipt, it is never the publish contract's, and an
operation that alters the candidate is refused rather than filed.

The gate is the real one. A double could not tell "delegates to the configured
quick owner" apart from "reimplements it", which is the whole claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.continuation_quick_validation import (
    ContinuationQuickValidation,
    PreparedQuickValidation,
    RefusedQuickValidation,
)
from issue_orchestrator.domain.attempt import Attempt, AttemptKey, StoredIssueKey
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.validation_profile import (
    AGENT_GATE_SUITE,
    ValidationGateKind,
)
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.entrypoints.bootstrap_continuation import (
    build_continuation_quick_validation,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.command_runner import CommandResult

SHA_A = "a" * 40
SHA_A_PRIME = "b" * 40
#: The operator's configured contracts. Sentinels, because what is under test is
#: that the CONFIGURED command runs: a name no production file mentions cannot
#: pass by being hard-coded somewhere.
QUICK_SENTINEL = "run-the-configured-quick-contract"
PUBLISH_SENTINEL = "run-the-configured-publish-contract"
PROFILE = "default"
NAMED_PROFILE = "strict"


@dataclass
class FakeWorkingCopy:
    """Reports whatever the test says the checkout currently holds."""

    head: str | None = SHA_A
    dirty: bool = False

    def get_head_sha(self, worktree: Path) -> str | None:
        return self.head

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return self.dirty


@dataclass
class FakeCommands:
    """The ``CommandRunner`` port, recording what the gate asked it to run."""

    commands: list[str] = field(default_factory=list)
    cwds: list[Path | None] = field(default_factory=list)
    failing: bool = False
    while_running: Any = None

    def run(self, command: str | list[str], **kwargs: Any) -> CommandResult:
        self.commands.append(str(command))
        cwd = kwargs.get("cwd")
        self.cwds.append(Path(cwd) if cwd is not None else None)
        if self.while_running is not None:
            self.while_running()
        if self.failing:
            return CommandResult(
                returncode=1, stdout="", stderr="1 failed", timed_out=False
            )
        return CommandResult(returncode=0, stdout="ok", stderr="", timed_out=False)


@dataclass
class FakeSessionOutput:
    """Allocates a real run directory and remembers the profile frozen onto it.

    The profile round-trips through the manifest exactly as the real adapter
    makes it: written at ``start_run``, read back by the contract resolver.
    A run whose manifest is never written must therefore read back as absent
    rather than as the default, which is what makes "the run's OWN contract"
    a testable claim.
    """

    manifests: dict[Path, dict[str, object]] = field(default_factory=dict)

    def start_run(
        self, worktree_path: Path, *, profile: str | None = PROFILE
    ) -> SessionRunAssets:
        run_dir = worktree_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.manifests[run_dir] = {"validation_profile": profile}
        return SessionRunAssets.from_paths(
            session_name="continuation-1",
            run_id="run-1",
            worktree_path=worktree_path,
            run_dir=run_dir,
            terminal_recording_path=run_dir / "terminal.cast",
            manifest_path=run_dir / "manifest.json",
            started_at="2026-08-20T00:00:00Z",
        )

    def read_manifest(self, run_dir: Path) -> dict[str, object] | None:
        return self.manifests.get(run_dir)


@dataclass
class Harness:
    preparation: ContinuationQuickValidation
    worktree: Path
    assets: SessionRunAssets
    commands: FakeCommands
    working_copy: FakeWorkingCopy


def _config(
    *, quick: str | None = QUICK_SENTINEL, publish: str | None = PUBLISH_SENTINEL
) -> Config:
    config = Config()
    config.validation.quick.cmd = quick
    config.validation.publish.cmd = publish
    return config


def _harness(
    tmp_path: Path,
    *,
    config: Config | None = None,
    profile: str | None = PROFILE,
) -> Harness:
    worktree = tmp_path / "continuation-149-aaaaaaaaaaaa"
    worktree.mkdir(parents=True, exist_ok=True)
    commands = FakeCommands()
    working_copy = FakeWorkingCopy()
    session_output = FakeSessionOutput()
    return Harness(
        preparation=build_continuation_quick_validation(
            config if config is not None else _config(),
            session_output=session_output,  # type: ignore[arg-type]
            command_runner=commands,  # type: ignore[arg-type]
            working_copy=working_copy,  # type: ignore[arg-type]
        ),
        worktree=worktree,
        assets=session_output.start_run(worktree, profile=profile),
        commands=commands,
        working_copy=working_copy,
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return _harness(tmp_path)


def _prepare(
    harness: Harness,
) -> PreparedQuickValidation | RefusedQuickValidation:
    return harness.preparation.prepare(
        worktree=harness.worktree, run_assets=harness.assets
    )


def _produced_record(harness: Harness) -> dict[str, Any]:
    prepared = _prepare(harness)
    assert isinstance(prepared, PreparedQuickValidation)
    assert prepared.record_path is not None
    return json.loads(prepared.record_path.read_text(encoding="utf-8"))


class TestTheEvidenceIsGenuinelyProduced:
    def test_the_configured_quick_contract_runs_in_the_candidates_checkout(
        self, harness: Harness
    ) -> None:
        _prepare(harness)

        assert harness.commands.commands == [QUICK_SENTINEL]
        assert harness.commands.cwds == [harness.worktree]

    def test_the_record_lands_where_the_exchange_reads_it(
        self, harness: Harness
    ) -> None:
        """The run-scoped path the pair mirror falls back to, and nowhere else."""
        prepared = _prepare(harness)

        assert isinstance(prepared, PreparedQuickValidation)
        assert prepared.record_path == harness.assets.validation_artifacts.record_path
        assert prepared.record_path is not None
        assert prepared.record_path.exists()

    def test_the_record_names_the_commit_the_gate_ran_at(
        self, harness: Harness
    ) -> None:
        assert _produced_record(harness)["head_sha"] == SHA_A

    def test_the_record_reports_the_pass_it_actually_reached(
        self, harness: Harness
    ) -> None:
        record = _produced_record(harness)

        assert record["passed"] is True
        assert record["exit_code"] == 0

    def test_the_gates_output_is_kept_beside_the_record(
        self, harness: Harness
    ) -> None:
        """A reviewer that doubts the record has the run's own output to read."""
        _prepare(harness)

        artifacts = harness.assets.validation_artifacts
        assert artifacts.stdout_path.read_text() == "ok"
        assert artifacts.stderr_path.exists()


class TestNothingIsReusedRetypedOrSynthesised:
    def test_the_record_carries_the_quick_contract_not_the_publish_one(
        self, harness: Harness
    ) -> None:
        """A ``publish_gate`` receipt can never satisfy the quick requirement."""
        record = _produced_record(harness)

        assert record["command"] == QUICK_SENTINEL
        assert record["suite"] == AGENT_GATE_SUITE
        assert ValidationGateKind.from_suite(record["suite"]) is (
            ValidationGateKind.QUICK
        )
        assert record["suite"] != ValidationGateKind.PUBLISH.suite

    def test_a_durable_verdict_whose_record_is_gone_is_never_synthesised(
        self, tmp_path: Path
    ) -> None:
        """Direction 2: preparation executes or fails; it never invents a file.

        The candidate carries a completed quick verdict for this exact commit
        whose run directory has since been reaped. A preparation that reused it
        would produce no record at all — a reviewer pointed at nothing — or, if
        it wrote one from the receipt, would claim exit codes and timestamps no
        gate reported. It does neither: the contract runs again.
        """
        attempts = SidecarAttemptStore(tmp_path / "primary")
        key = AttemptKey(StoredIssueKey("149", "owner/repo"), SHA_A)
        attempts.update(
            key,
            lambda attempt: attempt.with_completed_evaluation(
                ValidationVerdictReceipt(
                    suite=AGENT_GATE_SUITE,
                    head_sha=SHA_A,
                    verdict=ValidationVerdict.PASSED,
                    command=QUICK_SENTINEL,
                    profile=PROFILE,
                )
            ),
        )
        harness = _harness(tmp_path)

        prepared = _prepare(harness)

        assert harness.commands.commands == [QUICK_SENTINEL]
        assert isinstance(prepared, PreparedQuickValidation)
        stored = attempts.for_key(key)
        assert stored is not None
        assert len(stored.completed_evaluations) == 1

    def test_no_publication_evaluation_is_filed_for_this_run(
        self, tmp_path: Path
    ) -> None:
        """The gate carries no candidate identity, so it files nothing durable."""
        attempts = SidecarAttemptStore(tmp_path / "primary")
        key = AttemptKey(StoredIssueKey("149", "owner/repo"), SHA_A)
        attempts.update(
            key,
            lambda attempt: attempt.with_completed_evaluation(
                ValidationVerdictReceipt(
                    suite=ValidationGateKind.PUBLISH.suite,
                    head_sha=SHA_A,
                    verdict=ValidationVerdict.PASSED,
                    command=PUBLISH_SENTINEL,
                    profile=PROFILE,
                )
            ),
        )
        harness = _harness(tmp_path)

        _prepare(harness)

        stored: Attempt | None = attempts.for_key(key)
        assert stored is not None
        assert [
            evaluation.suite for evaluation in stored.publication_evaluations
        ] == [ValidationGateKind.PUBLISH.suite]

    def test_the_publish_contract_is_never_executed(self, harness: Harness) -> None:
        _prepare(harness)

        assert PUBLISH_SENTINEL not in harness.commands.commands


class TestTheRunsOwnContractIsTheOneThatRuns:
    def test_a_named_profile_frozen_onto_the_run_selects_its_own_quick_command(
        self, tmp_path: Path
    ) -> None:
        """The descriptor's profile, not the current default (#7059)."""
        from issue_orchestrator.infra.config_models import (
            ValidationCommandConfig,
            ValidationProfileConfig,
        )

        strict_quick = "run-the-strict-quick-contract"
        config = _config()
        config.validation.profiles = {
            NAMED_PROFILE: ValidationProfileConfig(
                quick=ValidationCommandConfig(cmd=strict_quick)
            )
        }
        harness = _harness(tmp_path, config=config, profile=NAMED_PROFILE)

        record = _produced_record(harness)

        assert harness.commands.commands == [strict_quick]
        assert record["profile"] == NAMED_PROFILE

    def test_a_run_whose_profile_is_retired_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Substituting a contract would claim a gate this run never executed."""
        harness = _harness(tmp_path, profile="a-profile-nobody-defines-any-more")

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert harness.commands.commands == []

    def test_a_repository_with_no_quick_contract_names_no_evidence(
        self, tmp_path: Path
    ) -> None:
        """Nothing to run, so nothing to claim — and nothing invented."""
        harness = _harness(tmp_path, config=_config(quick=None))

        prepared = _prepare(harness)

        assert prepared == PreparedQuickValidation(record_path=None)
        assert harness.commands.commands == []
        assert not harness.assets.validation_artifacts.record_path.exists()


class TestThePreparationMustNotAlterTheCandidate:
    def test_a_gate_that_moves_head_is_refused(self, harness: Harness) -> None:
        """The candidate's commit is what the evidence is about."""
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "head", SHA_A_PRIME
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "moved HEAD" in prepared.reason

    def test_evidence_produced_before_a_move_still_names_the_commit_it_ran_at(
        self, harness: Harness
    ) -> None:
        """The head binding the exchange re-checks cannot be fooled either.

        Even where a checkpoint could not see a move — the checkpoint is the
        primary refusal — the record names the commit the gate READ, so the
        mirror comparing it against the coder worktree's current HEAD reads it
        as stale and refuses the round rather than passing silently.
        """
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "head", SHA_A_PRIME
        )

        _prepare(harness)

        record = json.loads(
            harness.assets.validation_artifacts.record_path.read_text(
                encoding="utf-8"
            )
        )
        assert record["head_sha"] == SHA_A
        assert record["head_sha"] != harness.working_copy.head

    def test_a_gate_that_dirties_tracked_content_is_refused(
        self, harness: Harness
    ) -> None:
        """HEAD unmoved, tracked content modified: the dirty postflight."""
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "dirty", True
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "uncommitted changes" in prepared.reason
        assert harness.working_copy.head == SHA_A

    def test_a_checkout_that_was_already_dirty_is_not_blamed_on_the_gate(
        self, tmp_path: Path
    ) -> None:
        """Only a clean-to-dirty transition is the operation's doing."""
        harness = _harness(tmp_path)
        harness.working_copy.dirty = True

        prepared = _prepare(harness)

        assert isinstance(prepared, PreparedQuickValidation)

    def test_an_altered_candidate_outranks_a_gate_failure(
        self, harness: Harness
    ) -> None:
        """Two separate facts, and the first must not suppress the second."""
        harness.commands.failing = True
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "dirty", True
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "uncommitted changes" in prepared.reason


class TestAFailedOrUnprovableGateIsRefused:
    def test_a_failing_quick_contract_is_refused(self, harness: Harness) -> None:
        """A reviewer must never be handed a record reading ``passed: false``."""
        harness.commands.failing = True

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)

    def test_an_unreadable_head_is_refused_rather_than_guessed(
        self, harness: Harness
    ) -> None:
        harness.working_copy.head = None

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert harness.commands.commands == []
