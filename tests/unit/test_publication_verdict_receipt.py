"""The publish gate's verdict outlives the worktree that produced it (#85).

The defect these pin: a ``ValidationRecord`` is the only thing that says the
publication gate passed, and it lives in the run directory inside the coder
worktree. ``Attempt(issue, A)`` survives cleanup and restart, but all it kept
was ``validation_record_path`` — a pointer that dangles once the worktree is
reaped, and in the observed case ``null``. After cleanup no reader could tell
**"A passed"**, **"A failed"** and **"A was never gated"** apart.

Durability is asserted by destroying things — ``git worktree remove``, then a
*new* store instance for the reader — rather than by reasoning about where the
bytes live. ``TestRemovingTheBindingBreaksThesePins`` performs the mutations
§9 of the issue requires: a proof that survives its own mutation has pinned
nothing.

No GitHub adapter, no network, no repository host is constructed anywhere in
this module: the proof performs zero remote mutations by construction.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.publication_gate import (
    PublicationGate,
    RunValidationContracts,
)
from issue_orchestrator.control.publication_verdict import PublicationVerdictReceipts
from issue_orchestrator.control.publish_gate_diagnostics import (
    PublishGateDiagnostics,
)
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.execution_identity import (
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
    ExecutionPrincipal,
    ExecutionProvenance,
    ExecutionRole,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
    ValidationProfileConfig,
)
from issue_orchestrator.infra.validation_profiles import (
    ValidationProfile,
    ValidationProfileRegistry,
)

ISSUE = GitHubIssueKey(repo="acme/repo", external_id="85")
SHA_A = "a" * 40
SHA_PRIME = "b" * 40
PUBLISH_SENTINEL = "run-the-publish-contract"
QUICK_SENTINEL = "run-the-quick-contract"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class StubCommandRunner:
    """Reports a fixed outcome, and remembers what it was asked to run."""

    def __init__(self, returncode: int = 0, timed_out: bool = False) -> None:
        self.returncode = returncode
        self.timed_out = timed_out
        self.commands: list[str] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.commands.append(command)
        return SimpleNamespace(
            returncode=self.returncode,
            stdout="",
            stderr="",
            timed_out=self.timed_out,
        )


class StubWorkingCopy:
    def __init__(self, head_sha: str = SHA_A) -> None:
        self._head_sha = head_sha

    def get_head_sha(self, worktree: Path) -> str:
        return self._head_sha


class StubAttemptKeys:
    """The production derivation: the caller's issue key, at the gate's HEAD."""

    def for_validation_attempt(self, *, issue_key, head_sha: str) -> AttemptKey:
        return AttemptKey(issue_key, head_sha)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _registry(
    *,
    publish_cmd: str | None = PUBLISH_SENTINEL,
    profile_name: str = "default",
) -> ValidationProfileRegistry:
    quick = ValidationCommandConfig(cmd=QUICK_SENTINEL, timeout_seconds=111)
    publish = PublishValidationConfig(cmd=publish_cmd, timeout_seconds=222)
    if profile_name == "default":
        return ValidationProfileRegistry(
            ValidationConfig(quick=quick, publish=publish)
        )
    return ValidationProfileRegistry(
        ValidationConfig(
            profiles={
                profile_name: ValidationProfileConfig(quick=quick, publish=publish)
            }
        )
    )


def _gate(
    *,
    repo_root: Path,
    runner: StubCommandRunner,
    head_sha: str = SHA_A,
    registry: ValidationProfileRegistry | None = None,
) -> PublicationGate:
    """A publication gate whose receipts land in ``repo_root``, not the worktree."""
    return PublicationGate(
        contracts=RunValidationContracts(
            FileSystemSessionOutput(), registry or _registry()
        ),
        command_runner=runner,
        working_copy=StubWorkingCopy(head_sha),
        verdicts=PublicationVerdictReceipts(
            SidecarAttemptStore(repo_root), StubAttemptKeys()
        ),
        # Durable failure output lands in the same root the receipts do (#94);
        # these proofs are about the receipt, not about that artefact.
        diagnostics=PublishGateDiagnostics(repo_root),
    )


def _run(worktree: Path, *, profile: str = "default"):
    return FileSystemSessionOutput().start_run(
        worktree,
        "issue-85",
        issue_number=85,
        validation_profile=profile,
    )


def _read(repo_root: Path, head_sha: str = SHA_A) -> Attempt | None:
    """A *fresh* store instance — the process-restart case, every time."""
    return SidecarAttemptStore(repo_root).for_key(AttemptKey(ISSUE, head_sha))


def _receipt(
    *,
    suite: str = "publish_gate",
    head_sha: str = SHA_A,
    verdict: ValidationVerdict = ValidationVerdict.PASSED,
) -> ValidationVerdictReceipt:
    return ValidationVerdictReceipt(
        suite=suite,
        head_sha=head_sha,
        verdict=verdict,
        command=PUBLISH_SENTINEL,
        profile="default",
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "issue-85"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------


class TestThePublishVerdictIsRecordedDurably:
    """Proof 1: a publish PASS for A is durably recorded on ``Attempt(issue, A)``."""

    def test_a_publish_pass_lands_on_the_attempt_with_its_contract(
        self, repo_root: Path, worktree: Path
    ) -> None:
        runner = StubCommandRunner()

        outcome = _gate(repo_root=repo_root, runner=runner).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        assert outcome.allowed is True
        assert runner.commands == [PUBLISH_SENTINEL]
        attempt = _read(repo_root)
        assert attempt is not None
        receipt = attempt.publication_verdict
        assert receipt is not None
        assert receipt.verdict is ValidationVerdict.PASSED
        # Suite, command and profile together: the provenance that identifies
        # the contract that actually executed, which is what the validation
        # cache's own reuse predicate compares.
        assert receipt.suite == "publish_gate"
        assert receipt.command == PUBLISH_SENTINEL
        assert receipt.profile == "default"
        assert receipt.head_sha == SHA_A
        assert attempt.publication_validation_passed is True

    def test_the_receipt_names_the_profile_the_run_was_frozen_under(
        self, repo_root: Path, worktree: Path
    ) -> None:
        """Two profiles may define the same command and mean different contracts."""
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(),
            registry=_registry(profile_name="foundation"),
        ).check(
            worktree=worktree,
            run_assets=_run(worktree, profile="foundation"),
            issue_key=ISSUE,
        )

        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.publication_verdict is not None
        assert attempt.publication_verdict.profile == "foundation"

    def test_recording_the_verdict_preserves_the_attempts_other_facts(
        self, repo_root: Path, worktree: Path
    ) -> None:
        """Proof 7: no regression in execution-identity or reroute-budget state."""
        key = AttemptKey(ISSUE, SHA_A)
        identities = CandidateExecutionIdentities(
            candidate_sha=SHA_A,
            actor=AgentExecutionIdentity(
                role=ExecutionRole.ACTOR,
                principal=ExecutionPrincipal(agent_label="agent:backend"),
                provenance=ExecutionProvenance(provider="claude-code", model="opus"),
            ),
            reviewer=AgentExecutionIdentity(
                role=ExecutionRole.REVIEWER,
                principal=ExecutionPrincipal(agent_label="agent:reviewer"),
                provenance=ExecutionProvenance(provider="codex", model="gpt-5"),
            ),
            observed_at="2026-08-17T00:00:00+00:00",
        )
        SidecarAttemptStore(repo_root).update(
            key,
            lambda attempt: replace(
                attempt,
                reroute_budget_used=2,
                validation_record_path="/runs/1/validation-record.json",
                execution_identities=identities,
            ),
        )

        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.reroute_budget_used == 2
        assert attempt.validation_record_path == "/runs/1/validation-record.json"
        assert attempt.execution_identities == identities
        assert attempt.publication_validation_passed is True


class TestTheVerdictOutlivesItsWorktree:
    """Proof 2: still readable once the producing worktree is gone."""

    def test_a_pass_survives_worktree_removal_and_a_fresh_reader(
        self, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _git(repo_root, "init", "-q", "-b", "main")
        _git(repo_root, "config", "user.email", "t@example.com")
        _git(repo_root, "config", "user.name", "T")
        (repo_root / "seed.txt").write_text("seed\n")
        _git(repo_root, "add", "seed.txt")
        _git(repo_root, "commit", "-q", "-m", "seed")

        worktree = tmp_path / "issue-85"
        _git(repo_root, "worktree", "add", "-q", "-b", "issue-85", str(worktree))
        (worktree / "work.py").write_text("# candidate\n")
        _git(worktree, "add", "work.py")
        _git(worktree, "commit", "-q", "-m", "candidate")
        candidate_sha = _git(worktree, "rev-parse", "HEAD")

        run = _run(worktree)
        _gate(
            repo_root=repo_root, runner=StubCommandRunner(), head_sha=candidate_sha
        ).check(worktree=worktree, run_assets=run, issue_key=ISSUE)
        # The record the gate wrote is inside the worktree, and dies with it.
        assert run.run_dir.is_relative_to(worktree)

        _git(repo_root, "worktree", "remove", "--force", str(worktree))
        assert not worktree.exists()

        # A fresh store instance: nothing is served from the writer's memory,
        # so this is also the process-restart case.
        attempt = _read(repo_root, candidate_sha)
        assert attempt is not None
        assert attempt.publication_validation_passed is True
        assert attempt.publication_verdict is not None
        assert attempt.publication_verdict.suite == "publish_gate"


class TestOneCandidatesReceiptCannotAnswerForAnother:
    """Proof 3: A's receipt is not readable as A′'s."""

    def test_a_later_candidate_has_no_receipt_of_its_own(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        assert _read(repo_root, SHA_PRIME) is None

    def test_a_receipt_does_not_certify_a_commit_it_does_not_name(self) -> None:
        assert _receipt().certifies_publication(SHA_A) is True
        assert _receipt().certifies_publication(SHA_PRIME) is False

    def test_an_attempt_refuses_to_hold_another_candidates_receipt(self) -> None:
        with pytest.raises(ValueError, match="must name the attempt's own commit"):
            Attempt(
                key=AttemptKey(ISSUE, SHA_A),
                publication_verdict=_receipt(head_sha=SHA_PRIME),
            )


class TestAQuickPassIsNotAPublicationPass:
    """Proof 4: an ``agent_gate`` PASS is not judged a publication PASS."""

    def test_an_agent_gate_receipt_never_certifies_publication(
        self, repo_root: Path
    ) -> None:
        key = AttemptKey(ISSUE, SHA_A)
        SidecarAttemptStore(repo_root).update(
            key,
            lambda attempt: replace(
                attempt,
                publication_verdict=_receipt(suite="agent_gate"),
            ),
        )

        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.publication_verdict is not None
        assert attempt.publication_verdict.verdict is ValidationVerdict.PASSED
        assert attempt.publication_validation_passed is False

    @pytest.mark.parametrize("suite", ["agent_gate", "quick_gate", "made_up_gate"])
    def test_no_other_suite_certifies_publication(self, suite: str) -> None:
        assert _receipt(suite=suite).certifies_publication(SHA_A) is False


class TestAbsenceAndDamageAreNotAPass:
    """Proof 5: missing, never-run, malformed and mismatched are none of them PASS."""

    def test_an_unrecorded_candidate_reads_as_absent(self, repo_root: Path) -> None:
        assert _read(repo_root) is None

    def test_an_attempt_with_no_receipt_is_not_a_pass(self, repo_root: Path) -> None:
        SidecarAttemptStore(repo_root).update(
            AttemptKey(ISSUE, SHA_A),
            lambda attempt: replace(attempt, reroute_budget_used=1),
        )

        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.publication_verdict is None
        assert attempt.publication_validation_passed is False

    def test_an_unconfigured_publish_contract_leaves_no_receipt(
        self, repo_root: Path, worktree: Path
    ) -> None:
        """Never gated is the absence of a receipt, not a receipt saying nothing."""
        runner = StubCommandRunner()

        outcome = _gate(
            repo_root=repo_root, runner=runner, registry=_registry(publish_cmd=None)
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert runner.commands == []
        assert outcome.allowed is True
        assert _read(repo_root) is None

    def test_a_malformed_receipt_raises_rather_than_reading_as_absent(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )
        sidecar = _sidecar(repo_root)
        payload = json.loads(sidecar.read_text())
        del payload["publication_verdict"]["suite"]
        sidecar.write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="suite"):
            _read(repo_root)

    def test_an_unknown_verdict_value_raises(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )
        sidecar = _sidecar(repo_root)
        payload = json.loads(sidecar.read_text())
        payload["publication_verdict"]["verdict"] = "probably_fine"
        sidecar.write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="unknown validation verdict"):
            _read(repo_root)

    def test_a_head_mismatched_receipt_raises_rather_than_certifying(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )
        sidecar = _sidecar(repo_root)
        payload = json.loads(sidecar.read_text())
        payload["publication_verdict"]["head_sha"] = SHA_PRIME
        sidecar.write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="must name the attempt's own commit"):
            _read(repo_root)

    def test_a_receipt_written_by_an_unknown_schema_raises(self) -> None:
        payload = _receipt().to_payload()
        payload["schema_version"] = 2

        with pytest.raises(ValueError, match="schema_version"):
            ValidationVerdictReceipt.from_payload(payload)


class TestFailureAndTimeoutAreDistinguishable:
    """Proof 6: a publish FAIL and a timeout are each distinguishable from PASS."""

    def test_a_failing_publish_contract_records_a_failure(
        self, repo_root: Path, worktree: Path
    ) -> None:
        outcome = _gate(
            repo_root=repo_root, runner=StubCommandRunner(returncode=1)
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert outcome.allowed is False
        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.publication_verdict is not None
        assert attempt.publication_verdict.verdict is ValidationVerdict.FAILED
        assert attempt.publication_validation_passed is False

    def test_a_timed_out_publish_contract_records_a_timeout(
        self, repo_root: Path, worktree: Path
    ) -> None:
        outcome = _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=-1, timed_out=True),
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert outcome.allowed is False
        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.publication_verdict is not None
        assert attempt.publication_verdict.verdict is ValidationVerdict.TIMED_OUT
        assert attempt.publication_validation_passed is False

    def test_a_timeout_never_reads_as_a_pass_however_its_exit_code_landed(
        self,
    ) -> None:
        """A killed command did not finish the contract, whatever it returned."""
        assert (
            ValidationVerdict.observed(passed=True, timed_out=True)
            is ValidationVerdict.TIMED_OUT
        )


class TestRemovingTheBindingBreaksThesePins:
    """Proof 9: each binding is load-bearing, shown by removing it.

    Every test here applies one mutation to production code and asserts the
    *wrong* answer appears. If a mutation stopped changing the answer, the
    corresponding proof above would be pinning nothing and this test would
    fail — which is the point.
    """

    def test_without_the_durable_write_no_receipt_exists(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            PublicationGate,
            "_record_verdict",
            lambda self, record, issue_key: None,
        )

        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        # The verdict is gone even though the gate passed: exactly the state
        # the observed failures were in once the worktree was reaped.
        assert _read(repo_root) is None

    def test_without_the_exact_a_binding_another_candidates_receipt_certifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ValidationVerdictReceipt, "covers", lambda self, head_sha: True
        )

        assert _receipt(head_sha=SHA_PRIME).certifies_publication(SHA_A) is True

    def test_without_the_suite_binding_an_agent_gate_pass_certifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ValidationVerdictReceipt,
            "from_publication_contract",
            property(lambda self: True),
        )

        assert _receipt(suite="agent_gate").certifies_publication(SHA_A) is True

    def test_without_the_verdict_binding_a_failure_certifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ValidationVerdictReceipt,
            "certifies_publication",
            lambda self, head_sha: self.from_publication_contract
            and self.covers(head_sha),
        )

        assert (
            _receipt(verdict=ValidationVerdict.FAILED).certifies_publication(SHA_A)
            is True
        )

    def test_the_receipt_reports_the_contract_that_ran_not_the_one_requested(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Substituting the quick contract at the seam is visible in the receipt.

        The provenance is only worth recording if it tracks execution rather
        than intent: with the resolution mutated to hand back the quick
        contract, the receipt must say so instead of still claiming publish.
        """
        original = ValidationProfile.contract

        def always_quick(self: ValidationProfile, kind):
            from issue_orchestrator.domain.validation_profile import (
                ValidationGateKind,
            )

            return original(self, ValidationGateKind.QUICK)

        monkeypatch.setattr(ValidationProfile, "contract", always_quick)

        _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.publication_verdict is not None
        assert attempt.publication_verdict.command == QUICK_SENTINEL
        assert attempt.publication_verdict.suite != "publish_gate"
        assert attempt.publication_validation_passed is False


# ---------------------------------------------------------------------------
# Helpers used above
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sidecar(repo_root: Path) -> Path:
    return next((repo_root / ".issue-orchestrator" / "attempts").glob("*.json"))
