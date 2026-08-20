"""Producing a continuation's first-Reviewer quick-validation record (#173).

The preparation on its own, at the ports it composes. What the runner does
*with* a refusal — discard the checkout, open no run, leave the durable history
alone — is proved in ``test_continuation_runner``; what is proved here is the
narrower claim the runner depends on: the evidence is produced by the existing
configured quick-validation owner, it is never reused from a durable verdict or
synthesised from a receipt, it is never the publish contract's, and an
operation that alters the candidate is refused rather than filed.

Producing the evidence is only half of it, and the other half is what the two
final classes pin: the run is told what its gate found, in the manifest every
validation surface reads first, and a *failing* run's own output is written
outside the checkout its caller is about to delete.

The gate is the real one, and so are the session-output adapter and the
diagnostics store it records through. A double could not tell "delegates to the
configured quick owner" apart from "reimplements it", which is the whole claim,
and a double for the recorder would prove the calls were made rather than that
a reader can see the result.
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
from issue_orchestrator.control.gate_failure_diagnostics import (
    DIAGNOSTIC_FILE_NAME,
    GATE_FAILURES_DIR,
    STDERR_FILE_NAME,
    STDOUT_FILE_NAME,
)
from issue_orchestrator.domain.attempt import Attempt, AttemptKey, StoredIssueKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_key_codec import issue_key_path_part
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
from issue_orchestrator.execution.session_output_adapter import (
    FileSystemSessionOutput,
)
from issue_orchestrator.execution.validation_failure_summary import (
    load_validation_failure_summary,
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
#: The candidate a failed run's durable output is filed under — the same
#: identity, in the same spelling, that its attempt sidecar is keyed by.
ISSUE = GitHubIssueKey(repo="acme/repo", external_id="149")


@dataclass
class FakeWorkingCopy:
    """Reports whatever the test says the checkout currently holds.

    Tracked and untracked dirt are separate fields because the postflight's
    whole rule is that they are different facts: a suite is expected to write
    untracked report files into the checkout it ran in, and is not allowed to
    edit the candidate's tracked content. A fake carrying one boolean could
    not tell the test which of the two it had set up.

    ``enumeration_fails`` plays the git error that leaves the dirty state
    unknown, which the real adapter reports as ``None`` and not as "clean".
    """

    head: str | None = SHA_A
    tracked_dirt: tuple[str, ...] = ()
    untracked_dirt: tuple[str, ...] = ()
    enumeration_fails: bool = False

    def get_head_sha(self, worktree: Path) -> str | None:
        return self.head

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return bool(self.tracked_dirt or self.untracked_dirt)

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        if self.enumeration_fails:
            return None
        if mode == "all":
            return sorted([*self.tracked_dirt, *self.untracked_dirt])
        return sorted(self.tracked_dirt)


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
class Harness:
    preparation: ContinuationQuickValidation
    worktree: Path
    repo_root: Path
    assets: SessionRunAssets
    session_output: FileSystemSessionOutput
    commands: FakeCommands
    working_copy: FakeWorkingCopy

    @property
    def manifest(self) -> dict[str, Any]:
        """What this run now says about itself, read back off disk."""
        recorded = self.session_output.read_manifest(self.assets.run_dir)
        assert recorded is not None
        return recorded

    def durable_failures(self, head_sha: str = SHA_A) -> list[Path]:
        """What a reader holding only the primary checkout can still find."""
        failures_dir = self.repo_root / GATE_FAILURES_DIR
        if not failures_dir.exists():
            return []
        prefix = f"{issue_key_path_part(ISSUE)}--{head_sha}--"
        return sorted(
            path
            for path in failures_dir.iterdir()
            if path.is_dir() and path.name.startswith(prefix)
        )


def _config(
    tmp_path: Path,
    *,
    quick: str | None = QUICK_SENTINEL,
    publish: str | None = PUBLISH_SENTINEL,
) -> Config:
    config = Config()
    config.repo_root = tmp_path / "primary"
    config.validation.quick.cmd = quick
    config.validation.publish.cmd = publish
    return config


def _harness(
    tmp_path: Path,
    *,
    config: Config | None = None,
    profile: str = PROFILE,
) -> Harness:
    worktree = tmp_path / "continuation-149-aaaaaaaaaaaa"
    worktree.mkdir(parents=True, exist_ok=True)
    commands = FakeCommands()
    working_copy = FakeWorkingCopy()
    # The real adapter, not a double: the profile has to round-trip through a
    # manifest for "the run's OWN contract" to be a claim about anything, and
    # what this preparation records onto that manifest is half of what #173
    # owes the run — a fake would prove only that the calls were made.
    session_output = FileSystemSessionOutput()
    resolved = config if config is not None else _config(tmp_path)
    return Harness(
        preparation=build_continuation_quick_validation(
            resolved,
            session_output=session_output,
            command_runner=commands,  # type: ignore[arg-type]
            working_copy=working_copy,  # type: ignore[arg-type]
        ),
        worktree=worktree,
        repo_root=resolved.repo_root,
        assets=session_output.start_run(
            worktree, "continuation-1", validation_profile=profile
        ),
        session_output=session_output,
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
        worktree=harness.worktree,
        run_assets=harness.assets,
        issue_key=ISSUE,
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
        config = _config(tmp_path)
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
        harness = _harness(tmp_path, config=_config(tmp_path, quick=None))

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
            harness.working_copy, "tracked_dirt", ("src/app.py",)
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "modified tracked content" in prepared.reason
        assert "src/app.py" in prepared.reason
        assert harness.working_copy.head == SHA_A

    def test_a_gate_that_leaves_its_own_report_file_behind_still_produces_evidence(
        self, harness: Harness
    ) -> None:
        """The suite's output is not the candidate being altered.

        A quick contract configured as ``pytest -q --junitxml=test-results.xml``
        writes an untracked file into the checkout it ran in, and the report is
        the point — ``junit_xml_paths`` names it so the run can carry it. A
        postflight that read raw porcelain would refuse every such run, spend
        the allowance, and blame the candidate for a suite that passed.
        """
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "untracked_dirt", ("test-results.xml", ".coverage")
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, PreparedQuickValidation)
        assert prepared.record_path is not None

    def test_a_tracked_edit_to_an_operator_declared_runtime_path_is_refused(
        self, harness: Harness
    ) -> None:
        """``runtime-ignore`` classifies untracked state, and classifies by path.

        The completion guard and the pre-push check drop a runtime-ignored path
        on the strength of the path alone. Applying that classification to the
        candidate's *tracked* content would let a gate edit a tracked file whose
        path happens to match an operator pattern and leave this postflight
        reporting no change — preparation admitted, and a reviewer handed
        evidence produced by a run that mutated the candidate. The operator's
        declaration keeps its meaning over untracked runtime state; it is not a
        way to make tracked candidate content invisible here.
        """
        runtime_ignore = harness.worktree / ".issue-orchestrator" / "runtime-ignore"
        runtime_ignore.parent.mkdir(parents=True, exist_ok=True)
        runtime_ignore.write_text("build/test-results/\n", encoding="utf-8")
        harness.commands.while_running = lambda: setattr(
            harness.working_copy,
            "tracked_dirt",
            ("build/test-results/report.xml",),
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "modified tracked content" in prepared.reason
        assert "build/test-results/report.xml" in prepared.reason

    def test_the_same_runtime_path_left_untracked_still_produces_evidence(
        self, harness: Harness
    ) -> None:
        """The other half of the pair above: tracked-ness is what decides.

        Same path, same ``runtime-ignore`` declaration, written by the gate as
        new untracked output rather than as an edit to the candidate's tracked
        content — and this is ordinary suite output, so the run proceeds. The
        refusal above is not a narrowing of what a suite may emit.
        """
        runtime_ignore = harness.worktree / ".issue-orchestrator" / "runtime-ignore"
        runtime_ignore.parent.mkdir(parents=True, exist_ok=True)
        runtime_ignore.write_text("build/test-results/\n", encoding="utf-8")
        harness.commands.while_running = lambda: setattr(
            harness.working_copy,
            "untracked_dirt",
            ("build/test-results/report.xml",),
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, PreparedQuickValidation)

    def test_a_tracked_edit_under_a_built_in_runtime_prefix_is_refused(
        self, harness: Harness
    ) -> None:
        """The built-in half of the same classification is declined too.

        ``.issue-orchestrator/`` is a runtime-metadata prefix every dirty guard
        hides — and the orchestrator's own repository tracks files beneath it
        (``.issue-orchestrator/config/``). No ``runtime-ignore`` file has to
        exist for that to matter, so this half of the filter is pinned
        separately from the operator-declared one.
        """
        harness.commands.while_running = lambda: setattr(
            harness.working_copy,
            "tracked_dirt",
            (".issue-orchestrator/config/modes/default/main.yaml",),
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "modified tracked content" in prepared.reason
        assert ".issue-orchestrator/config/modes/default/main.yaml" in prepared.reason

    def test_a_checkout_whose_dirt_cannot_be_read_is_refused(
        self, harness: Harness
    ) -> None:
        """Unknown is not clean: an unprovable candidate opens no run."""
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "enumeration_fails", True
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "could not be enumerated" in prepared.reason

    def test_a_checkout_that_was_already_dirty_is_not_blamed_on_the_gate(
        self, tmp_path: Path
    ) -> None:
        """Only dirt that appeared during the gate is the operation's doing."""
        harness = _harness(tmp_path)
        harness.working_copy.tracked_dirt = ("src/app.py",)

        prepared = _prepare(harness)

        assert isinstance(prepared, PreparedQuickValidation)

    def test_dirt_the_gate_added_to_an_already_dirty_checkout_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        """A path already dirty must not cover for one that was not."""
        harness = _harness(tmp_path)
        harness.working_copy.tracked_dirt = ("src/app.py",)
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "tracked_dirt", ("src/app.py", "src/gate_edited.py")
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "src/gate_edited.py" in prepared.reason
        assert "src/app.py" not in prepared.reason

    def test_an_altered_candidate_outranks_a_gate_failure(
        self, harness: Harness
    ) -> None:
        """Two separate facts, and the first must not suppress the second."""
        harness.commands.failing = True
        harness.commands.while_running = lambda: setattr(
            harness.working_copy, "tracked_dirt", ("src/app.py",)
        )

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert "modified tracked content" in prepared.reason


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


class TestTheRunIsToldWhatItsGateFound:
    """The other half of producing evidence: recording that it exists.

    A continuation run that validated and told its own manifest nothing is not
    a run with less detail — every validation surface in the product reads the
    manifest's outcome first and shows nothing at all without it. So the claims
    here are made on both sides of that boundary: the gate result reaches the
    manifest, and the manifest reaches the consumer that renders it.
    """

    def test_a_passing_run_records_the_outcome_its_gate_reached(
        self, harness: Harness
    ) -> None:
        _prepare(harness)

        assert harness.manifest["validation_status"] == "passed"
        assert harness.manifest["validation_passed"] is True
        assert harness.manifest.get("validation_reason") is None

    def test_the_run_names_the_record_and_the_logs_the_gate_wrote(
        self, harness: Harness
    ) -> None:
        artifacts = harness.assets.validation_artifacts

        _prepare(harness)

        assert harness.manifest["validation_record_path"] == str(
            artifacts.record_path
        )
        assert harness.manifest["validation_stdout"] == str(artifacts.stdout_path)
        assert harness.manifest["validation_stderr"] == str(artifacts.stderr_path)

    def test_the_run_appears_in_the_validation_summary_the_dialog_renders(
        self, harness: Harness
    ) -> None:
        """The consumer side: what an operator opening this run actually sees.

        ``load_validation_failure_summary`` reads the typed outcome first and
        returns ``None`` before it looks for any file, so this is the assertion
        that fails when a producer records evidence without recording that it
        validated.
        """
        _prepare(harness)

        summary = load_validation_failure_summary(
            harness.assets.run_dir, include_passed=True
        )

        assert summary is not None
        assert summary.status == "passed"
        assert summary.command == QUICK_SENTINEL
        assert summary.suite == AGENT_GATE_SUITE
        assert summary.exit_code == 0

    def test_a_failing_run_records_the_failure_rather_than_silence(
        self, harness: Harness
    ) -> None:
        """A refused preparation still leaves the run saying what happened.

        The runner discards this checkout, so the manifest goes with it — but
        it must be truthful for as long as it exists, and a *pass* recorded
        here would be the one shape that could outlive the refusal wrongly.
        """
        harness.commands.failing = True

        _prepare(harness)

        summary = load_validation_failure_summary(harness.assets.run_dir)
        assert summary is not None
        assert summary.status == "failed"
        assert harness.manifest["validation_passed"] is False

    def test_a_gate_that_ran_no_command_claims_no_validation(
        self, tmp_path: Path
    ) -> None:
        """No contract, no record — and no manifest saying the run passed."""
        harness = _harness(tmp_path, config=_config(tmp_path, quick=None))

        _prepare(harness)

        assert "validation_status" not in harness.manifest
        assert load_validation_failure_summary(
            harness.assets.run_dir, include_passed=True
        ) is None


class TestAFailedGateLeavesAnExplanationOutsideTheCheckout:
    """#94's loss, on the path that does not even have to lose a race.

    Everything a failing run writes — the record, the two logs — is inside the
    continuation's checkout, and its caller deletes that checkout the moment
    this preparation refuses. So a candidate that exhausts its run allowance on
    a failing suite would return to rework with an exit code and nothing else,
    unless the gate wrote its output somewhere the discard cannot reach.
    """

    def test_the_failing_runs_output_is_kept_in_the_primary_checkout(
        self, harness: Harness
    ) -> None:
        harness.commands.failing = True

        _prepare(harness)

        directories = harness.durable_failures()
        assert len(directories) == 1
        payload = json.loads(
            (directories[0] / DIAGNOSTIC_FILE_NAME).read_text(encoding="utf-8")
        )
        assert payload["type"] == f"{AGENT_GATE_SUITE}_failure"
        assert payload["verdict"]["head_sha"] == SHA_A
        assert payload["verdict"]["command"] == QUICK_SENTINEL
        assert payload["verdict"]["verdict"] == ValidationVerdict.FAILED.value
        assert (directories[0] / STDERR_FILE_NAME).read_text() == "1 failed"
        assert (directories[0] / STDOUT_FILE_NAME).exists()

    def test_the_explanation_survives_the_checkout_it_was_produced_in(
        self, harness: Harness
    ) -> None:
        """Durability proved the only way it can be: by destroying the rest."""
        import shutil

        harness.commands.failing = True

        _prepare(harness)
        shutil.rmtree(harness.worktree)

        assert not harness.assets.validation_artifacts.stderr_path.exists()
        assert len(harness.durable_failures()) == 1

    def test_the_refusal_names_the_command_and_where_its_output_went(
        self, harness: Harness
    ) -> None:
        """What the runner logs has to outlive the directory it refers to."""
        harness.commands.failing = True

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert QUICK_SENTINEL in prepared.reason
        assert str(harness.repo_root / GATE_FAILURES_DIR) in prepared.reason

    def test_a_passing_run_files_no_failure_explanation(
        self, harness: Harness
    ) -> None:
        _prepare(harness)

        assert harness.durable_failures() == []

    def test_a_run_that_executed_nothing_files_no_explanation(
        self, harness: Harness
    ) -> None:
        """An unreadable HEAD ran no command, so it produced no account."""
        harness.working_copy.head = None

        prepared = _prepare(harness)

        assert isinstance(prepared, RefusedQuickValidation)
        assert harness.durable_failures() == []
        assert str(GATE_FAILURES_DIR) not in prepared.reason
