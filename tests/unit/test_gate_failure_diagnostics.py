"""A failed gate leaves its own explanation behind (#94).

The defect these pin: the publication gate's stdout and stderr are written into
the session run directory, which lives inside the coder worktree. Twice on #93 a
managed publish gate failed and its output was deleted with the worktree — 14 s
after the first verdict, 4 s after the second — and the second failure does not
reproduce in a clean detached environment, so the deleted output was the only
artefact that could ever have explained it.

#85 made the *verdict* durable, and that half is not in question here: the
receipt on ``Attempt(issue, A)`` still says which candidate failed. What these
prove is the other half — that after ordinary immediate cleanup a reader with no
prior context can still determine *why*.

Durability is proved by destroying things: ``git worktree remove``, then a
reader handed nothing but the primary checkout. ``TestRemovingTheDurableWrite
BreaksThesePins`` performs the failure-direction mutation the issue requires —
a test that survives its own mutation has pinned nothing.

The publication gate is the caller under test here because it was the first
one. It is no longer the only one: the store is now shared by every gate whose
output would otherwise die with its checkout, and what the continuation's quick
gate files into it is proved in ``control/test_continuation_quick_validation``
(#173).

No GitHub adapter, no network and no repository host is constructed anywhere in
this module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control import validation as validation_module
from issue_orchestrator.control.publication_evidence import (
    CandidatePublicationEvidence,
)
from issue_orchestrator.control.validation import (
    VALIDATION_SCHEMA_VERSION,
    ValidationGate,
)
from issue_orchestrator.control.publication_gate import (
    PublicationGate,
    RunValidationContracts,
    publish_gate_output_dir,
)
from issue_orchestrator.control.gate_failure_diagnostics import (
    DIAGNOSTIC_FILE_NAME,
    FAILURE_LOG_TAIL_BYTES,
    GATE_FAILURES_DIR,
    STDERR_FILE_NAME,
    STDOUT_FILE_NAME,
    CandidateGateDiagnostics,
    GateFailureDiagnostics,
    GateFailureOutput,
    needs_durable_diagnostic,
)
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.session_run import (
    VALIDATION_STDERR_NAME,
    VALIDATION_STDOUT_NAME,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_key_codec import issue_key_path_part
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import ValidationVerdict
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
    ValidationProfileConfig,
)
from issue_orchestrator.infra.validation_profiles import ValidationProfileRegistry
from issue_orchestrator.ports.session_output import ValidationRecord

ISSUE = GitHubIssueKey(repo="acme/repo", external_id="94")
OTHER_ISSUE = GitHubIssueKey(repo="acme/repo", external_id="93")
SHA_A = "a" * 40
SHA_PRIME = "b" * 40
PUBLISH_SENTINEL = "make validate-pr-raw"
QUICK_SENTINEL = "make validate-quick"

# The distinctive text a fresh reader has to be able to recover. Two streams,
# because a failure explained only by its stdout is a failure whose traceback
# was on the other one.
FAILING_STDOUT = "FAILED tests/unit/test_thing.py::test_it - AssertionError: boom"
FAILING_STDERR = "make: *** [validate-pr-raw] Error 1"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class StubCommandRunner:
    """Reports a fixed outcome, with output a reader could recognise."""

    def __init__(
        self,
        returncode: int = 0,
        timed_out: bool = False,
        stdout: str = FAILING_STDOUT,
        stderr: str = FAILING_STDERR,
    ) -> None:
        self.returncode = returncode
        self.timed_out = timed_out
        self.stdout = stdout
        self.stderr = stderr
        self.commands: list[str] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.commands.append(command)
        return SimpleNamespace(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


class StubWorkingCopy:
    def __init__(self, head_sha: str = SHA_A) -> None:
        self._head_sha = head_sha

    def get_head_sha(self, worktree: Path) -> str:
        return self._head_sha


class StubAttemptKeys:
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
    """A gate whose durable evidence lands in ``repo_root``, not the worktree."""
    return PublicationGate(
        contracts=RunValidationContracts(
            FileSystemSessionOutput(), registry or _registry()
        ),
        command_runner=runner,
        working_copy=StubWorkingCopy(head_sha),
        attempts=SidecarAttemptStore(repo_root),
        attempt_keys=StubAttemptKeys(),
        diagnostics=GateFailureDiagnostics(repo_root),
    )


def _run(worktree: Path, *, profile: str = "default"):
    return FileSystemSessionOutput().start_run(
        worktree,
        "issue-94",
        issue_number=94,
        validation_profile=profile,
    )


def _record(
    *,
    passed: bool = False,
    timed_out: bool = False,
    exit_code: int = 1,
    suite: str = "publish_gate",
    head_sha: str = SHA_A,
) -> ValidationRecord:
    return ValidationRecord(
        schema_version=VALIDATION_SCHEMA_VERSION,
        suite=suite,
        head_sha=head_sha,
        passed=passed,
        exit_code=exit_code,
        command=PUBLISH_SENTINEL,
        started_at="2026-08-17T00:00:00+00:00",
        ended_at="2026-08-17T00:01:00+00:00",
        timed_out=timed_out,
        stdout_path=f".issue-orchestrator/sessions/r1/publish-gate/{VALIDATION_STDOUT_NAME}",
        stderr_path=f".issue-orchestrator/sessions/r1/publish-gate/{VALIDATION_STDERR_NAME}",
        profile="default",
    )


# ---------------------------------------------------------------------------
# The fresh reader
# ---------------------------------------------------------------------------


class DurableDiagnostics:
    """What a reader holding only the primary checkout can find.

    Deliberately given nothing but ``repo_root``: no run directory, no session
    id, no gate object, no in-process state. If a proof below can answer a
    question through this class, an operator can answer it after cleanup.
    """

    def __init__(self, repo_root: Path) -> None:
        self._failures_dir = repo_root / GATE_FAILURES_DIR

    def for_candidate(self, issue_key, head_sha: str) -> list[Path]:
        prefix = f"{issue_key_path_part(issue_key)}--{head_sha}--"
        if not self._failures_dir.exists():
            return []
        return sorted(
            path
            for path in self._failures_dir.iterdir()
            if path.is_dir() and path.name.startswith(prefix)
        )

    def only_for_candidate(self, issue_key, head_sha: str) -> Path:
        found = self.for_candidate(issue_key, head_sha)
        assert len(found) == 1, f"expected exactly one diagnostic, got {found}"
        return found[0]

    def all_directories(self) -> list[Path]:
        if not self._failures_dir.exists():
            return []
        return sorted(path for path in self._failures_dir.iterdir() if path.is_dir())

    @staticmethod
    def read(directory: Path) -> dict[str, object]:
        payload = json.loads((directory / DIAGNOSTIC_FILE_NAME).read_text())
        assert isinstance(payload, dict)
        return payload

    @staticmethod
    def stdout(directory: Path) -> str:
        return (directory / STDOUT_FILE_NAME).read_text()

    @staticmethod
    def stderr(directory: Path) -> str:
        return (directory / STDERR_FILE_NAME).read_text()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "issue-94"
    path.mkdir()
    return path


@pytest.fixture
def durable(repo_root: Path) -> DurableDiagnostics:
    return DurableDiagnostics(repo_root)


# ---------------------------------------------------------------------------
# Proof 1 — the artefact is bound to exactly one candidate, and names its contract
# ---------------------------------------------------------------------------


class TestTheDiagnosticIsBoundToTheExactCandidate:
    """Acceptance 1: exact candidate A, plus suite, profile and command."""

    def test_a_failure_is_filed_under_the_candidate_it_ran_against(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        outcome = _gate(
            repo_root=repo_root, runner=StubCommandRunner(returncode=1)
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert outcome.allowed is False
        directory = durable.only_for_candidate(ISSUE, SHA_A)
        payload = durable.read(directory)
        # The identity half: the same (issue, commit) pair the attempt record is
        # keyed by, in the payload as well as in the path.
        assert payload["issue_key"] == {"scope": "acme/repo", "stable_id": "94"}
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["head_sha"] == SHA_A
        # The contract half: what ran, under which profile, and what it decided.
        # The artefact's own type names it too, and is taken from the record
        # rather than from whoever wired the destination — the store now holds
        # more than one gate's failures (#173).
        assert payload["type"] == "publish_gate_failure"
        assert verdict["suite"] == "publish_gate"
        assert verdict["command"] == PUBLISH_SENTINEL
        assert verdict["profile"] == "default"
        assert verdict["verdict"] == ValidationVerdict.FAILED.value
        assert payload["exit_code"] == 1
        assert payload["timed_out"] is False
        # And the output itself, on both streams.
        assert durable.stdout(directory) == FAILING_STDOUT
        assert durable.stderr(directory) == FAILING_STDERR

    def test_the_artefact_is_named_like_the_attempt_it_belongs_to(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """A reader holding the receipt can find the explanation by name.

        Nothing points from the attempt to the diagnostic — deliberately, so no
        admission predicate can follow such a pointer. The candidate's name on
        disk is what makes it findable instead, so the two spellings have to be
        one spelling.
        """
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        directory = durable.only_for_candidate(ISSUE, SHA_A)
        sidecar = _sidecar(repo_root)
        assert directory.name.startswith(f"{sidecar.stem}--")

    def test_the_diagnostic_names_the_profile_the_run_was_frozen_under(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1),
            registry=_registry(profile_name="foundation"),
        ).check(
            worktree=worktree,
            run_assets=_run(worktree, profile="foundation"),
            issue_key=ISSUE,
        )

        payload = durable.read(durable.only_for_candidate(ISSUE, SHA_A))
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["profile"] == "foundation"

    def test_another_candidates_failure_lands_under_its_own_identity(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """A′'s explanation is not readable as A's, and neither erases the other."""
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="A failed here"),
            head_sha=SHA_A,
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="A-prime failed there"),
            head_sha=SHA_PRIME,
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert durable.stdout(durable.only_for_candidate(ISSUE, SHA_A)) == (
            "A failed here"
        )
        assert durable.stdout(durable.only_for_candidate(ISSUE, SHA_PRIME)) == (
            "A-prime failed there"
        )

    def test_two_issues_at_one_commit_do_not_share_an_artefact(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """The binding is (issue, commit), exactly as the attempt key is."""
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="issue 94 failed"),
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="issue 93 failed"),
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=OTHER_ISSUE)

        assert durable.stdout(durable.only_for_candidate(ISSUE, SHA_A)) == (
            "issue 94 failed"
        )
        assert durable.stdout(durable.only_for_candidate(OTHER_ISSUE, SHA_A)) == (
            "issue 93 failed"
        )

    def test_a_second_failure_on_one_candidate_does_not_erase_the_first(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """A gate that failed twice failed for two reasons worth keeping.

        The cached-failure path re-runs rather than trusting a cached failure,
        so this is the ordinary shape of a retried publish, not an exotic one.
        """
        first = _run(worktree)
        second = _run(worktree)
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="first reason"),
        ).check(worktree=worktree, run_assets=first, issue_key=ISSUE)
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="second reason"),
        ).check(worktree=worktree, run_assets=second, issue_key=ISSUE)

        kept = {
            durable.stdout(path) for path in durable.for_candidate(ISSUE, SHA_A)
        }
        assert kept == {"first reason", "second reason"}


# ---------------------------------------------------------------------------
# Proof 2 — it survives ordinary immediate cleanup
# ---------------------------------------------------------------------------


class TestTheExplanationOutlivesItsWorktree:
    """Acceptance 2: the worktree is gone and the failure is still explainable."""

    def test_a_fresh_reader_can_still_say_why_the_gate_failed(
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

        worktree = tmp_path / "issue-94"
        _git(repo_root, "worktree", "add", "-q", "-b", "issue-94", str(worktree))
        (worktree / "work.py").write_text("# candidate\n")
        _git(worktree, "add", "work.py")
        _git(worktree, "commit", "-q", "-m", "candidate")
        candidate_sha = _git(worktree, "rev-parse", "HEAD")

        run = _run(worktree)
        outcome = _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1),
            head_sha=candidate_sha,
        ).check(worktree=worktree, run_assets=run, issue_key=ISSUE)

        assert outcome.allowed is False
        # The gate's own output is inside the worktree, and dies with it.
        gate_stdout = publish_gate_output_dir(run.run_dir) / VALIDATION_STDOUT_NAME
        assert gate_stdout.read_text() == FAILING_STDOUT
        assert gate_stdout.is_relative_to(worktree)

        # Ordinary immediate cleanup. No copy step, no grace period, no human
        # racing it: the runtime already wrote the evidence somewhere else.
        _git(repo_root, "worktree", "remove", "--force", str(worktree))
        assert not worktree.exists()

        # A reader with no prior context: only the primary checkout.
        durable = DurableDiagnostics(repo_root)
        directory = durable.only_for_candidate(ISSUE, candidate_sha)
        payload = durable.read(directory)
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["verdict"] == ValidationVerdict.FAILED.value
        assert verdict["command"] == PUBLISH_SENTINEL
        assert verdict["head_sha"] == candidate_sha
        assert payload["exit_code"] == 1
        assert FAILING_STDOUT in durable.stdout(directory)
        assert FAILING_STDERR in durable.stderr(directory)
        # And the authority half still agrees about what happened.
        attempt = SidecarAttemptStore(repo_root).for_key(
            AttemptKey(ISSUE, candidate_sha)
        )
        assert attempt is not None
        assert attempt.latest_publication_evaluation is not None
        assert attempt.latest_publication_evaluation.verdict is ValidationVerdict.FAILED

    def test_a_timeout_is_explainable_too(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """A killed contract is not a pass, so its output is kept as well.

        The same seam covers it, with no timeout-specific semantics: the trigger
        is the verdict the domain derives, and a timeout is not ``PASSED``.
        """
        outcome = _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=-1, timed_out=True),
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert outcome.allowed is False
        directory = durable.only_for_candidate(ISSUE, SHA_A)
        payload = durable.read(directory)
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["verdict"] == ValidationVerdict.TIMED_OUT.value
        assert payload["timed_out"] is True
        # The timeout marker the runner appends is part of the explanation.
        assert "[TIMEOUT after 222s]" in durable.stderr(directory)


# ---------------------------------------------------------------------------
# Proof 3 — the artefact authorizes nothing
# ---------------------------------------------------------------------------


class TestTheDiagnosticIsNotAnAuthority:
    """Acceptance 3: ``publication_verdict`` decides; the diagnostic does not."""

    def test_the_failure_is_refused_by_admission_with_the_diagnostic_present(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        registry = _registry()
        _gate(
            repo_root=repo_root, runner=StubCommandRunner(returncode=1)
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert durable.for_candidate(ISSUE, SHA_A)
        certification = CandidatePublicationEvidence(
            SidecarAttemptStore(repo_root), StubAttemptKeys()
        ).certification(issue_key=ISSUE, head_sha=SHA_A, profiles=registry)
        assert certification.admitted is False
        assert certification.reason == "publication_verdict_not_passed"

    def test_nothing_on_the_attempt_points_at_the_diagnostic(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """The evidence admission reads cannot reach the artefact at all.

        A pointer would be the shortest route from "diagnostic" to "input to a
        decision", so there is none: discovery is by the candidate's name.
        """
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        directory = durable.only_for_candidate(ISSUE, SHA_A)
        sidecar_text = _sidecar(repo_root).read_text()
        assert directory.name not in sidecar_text
        assert "diagnostic" not in sidecar_text

    def test_a_diagnostic_without_a_receipt_admits_nothing(
        self, repo_root: Path, durable: DurableDiagnostics
    ) -> None:
        """Even hand-planted, the artefact cannot make a candidate reviewable."""
        GateFailureDiagnostics(repo_root).for_candidate(ISSUE).record_failure(
            GateFailureOutput(
                record=_record(passed=False), stdout="anything", stderr="at all"
            )
        )

        assert durable.for_candidate(ISSUE, SHA_A)
        certification = CandidatePublicationEvidence(
            SidecarAttemptStore(repo_root), StubAttemptKeys()
        ).certification(issue_key=ISSUE, head_sha=SHA_A, profiles=_registry())
        assert certification.admitted is False
        assert certification.reason == "publication_receipt_missing"


# ---------------------------------------------------------------------------
# Proof 3b — the one reader, and what it refuses to answer
# ---------------------------------------------------------------------------


class TestReadingTheExplanationBackByName:
    """#297: the store answers "why did this candidate fail", after cleanup.

    The read is monotone in the refusing direction — see the module docstring
    of ``control/gate_failure_diagnostics``. Finding a bundle authorizes
    nothing the receipt did not already authorize; failing to find one only
    refuses. So what has to be pinned is that it never answers about the WRONG
    candidate or the WRONG contract, and that "nothing survives" is answered
    rather than approximated.
    """

    def test_the_failing_output_is_read_back_after_the_worktree_is_gone(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "issue-94-worktree"
        worktree.mkdir()
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )
        _remove_tree(worktree)

        failure = (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        )

        assert failure is not None
        assert failure.receipt.head_sha == SHA_A
        assert failure.receipt.verdict is ValidationVerdict.FAILED
        assert failure.receipt.command == PUBLISH_SENTINEL
        assert failure.exit_code == 1
        assert failure.timed_out is False
        assert failure.stdout.tail == FAILING_STDOUT
        assert failure.stderr.tail == FAILING_STDERR
        assert failure.stdout.truncated is False
        assert failure.explains_the_failure is True
        assert failure.directory.is_relative_to(repo_root)

    def test_nothing_filed_reads_as_nothing_found(self, repo_root: Path) -> None:
        assert (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        ) is None

    def test_a_store_that_does_not_exist_reads_as_nothing_found(
        self, tmp_path: Path
    ) -> None:
        assert (
            GateFailureDiagnostics(tmp_path / "no-such-checkout")
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        ) is None

    def test_another_candidates_explanation_is_never_returned(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1),
            head_sha=SHA_PRIME,
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        reader = GateFailureDiagnostics(repo_root).for_candidate(ISSUE)

        assert (
            reader.latest_failure(
                head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite
            )
            is None
        )
        assert (
            reader.latest_failure(
                head_sha=SHA_PRIME, suite=ValidationGateKind.PUBLISH.suite
            )
            is not None
        )

    def test_another_issues_explanation_is_never_returned(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=OTHER_ISSUE
        )

        assert (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        ) is None

    def test_another_contracts_explanation_is_never_returned(
        self, repo_root: Path
    ) -> None:
        """Two contracts can both fail one candidate; neither answers for the other."""
        GateFailureDiagnostics(repo_root).for_candidate(ISSUE).record_failure(
            GateFailureOutput(
                record=_record(suite=ValidationGateKind.QUICK.suite),
                stdout="the quick gate failed",
                stderr="",
            )
        )

        reader = GateFailureDiagnostics(repo_root).for_candidate(ISSUE)

        assert (
            reader.latest_failure(
                head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite
            )
            is None
        )
        assert (
            reader.latest_failure(
                head_sha=SHA_A, suite=ValidationGateKind.QUICK.suite
            )
            is not None
        )

    def test_the_newest_of_several_failures_is_the_one_returned(
        self, repo_root: Path, worktree: Path
    ) -> None:
        """A retried publish files one bundle per run; the last one is current."""
        for stdout in ("first reason", "second reason", "third reason"):
            _gate(
                repo_root=repo_root,
                runner=StubCommandRunner(returncode=1, stdout=stdout),
            ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        failure = (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        )

        assert failure is not None
        assert failure.stdout.tail == "third reason"

    def test_an_upper_case_sha_names_the_same_candidate(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        assert (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(
                head_sha=SHA_A.upper(), suite=ValidationGateKind.PUBLISH.suite
            )
        ) is not None

    def test_a_corrupt_bundle_falls_through_to_the_one_before_it(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """An unreadable artefact costs detail, never all of the evidence."""
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="the older reason"),
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="the newer reason"),
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)
        newest = durable.for_candidate(ISSUE, SHA_A)[-1]
        (newest / DIAGNOSTIC_FILE_NAME).write_text("{ not json", encoding="utf-8")

        failure = (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        )

        assert failure is not None
        assert failure.stdout.tail == "the older reason"

    def test_a_bundle_whose_payload_names_another_candidate_is_refused(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """The name and the payload have to agree, or the bundle explains nothing.

        Hand-editing one and not the other is how an explanation about A′ ends
        up sitting where a reader looking for A would find it.
        """
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )
        directory = durable.only_for_candidate(ISSUE, SHA_A)
        payload = durable.read(directory)
        verdict = payload["verdict"]
        assert isinstance(verdict, dict)
        verdict["head_sha"] = SHA_PRIME
        (directory / DIAGNOSTIC_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

        assert (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        ) is None

    def test_a_large_log_is_tailed_and_says_it_was(self, repo_root: Path) -> None:
        head = "the beginning nobody will see"
        tail_marker = "FAILED tests/unit/test_thing.py::test_it"
        noise = "x" * (FAILURE_LOG_TAIL_BYTES * 3)
        GateFailureDiagnostics(repo_root).for_candidate(ISSUE).record_failure(
            GateFailureOutput(
                record=_record(),
                stdout=f"{head}\n{noise}\n{tail_marker}\n",
                stderr="short",
            )
        )

        failure = (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        )

        assert failure is not None
        assert failure.stdout.truncated is True
        assert tail_marker in failure.stdout.tail
        assert head not in failure.stdout.tail
        assert len(failure.stdout.tail) <= FAILURE_LOG_TAIL_BYTES
        # The other stream is untouched by the bound.
        assert failure.stderr.truncated is False
        assert failure.stderr.tail == "short"

    def test_a_bundle_with_no_output_says_it_explains_nothing(
        self, repo_root: Path
    ) -> None:
        GateFailureDiagnostics(repo_root).for_candidate(ISSUE).record_failure(
            GateFailureOutput(record=_record(), stdout="", stderr="  \n")
        )

        failure = (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        )

        assert failure is not None
        assert failure.explains_the_failure is False

    def test_a_deleted_log_leaves_the_other_stream_readable(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        _gate(repo_root=repo_root, runner=StubCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )
        directory = durable.only_for_candidate(ISSUE, SHA_A)
        (directory / STDOUT_FILE_NAME).unlink()

        failure = (
            GateFailureDiagnostics(repo_root)
            .for_candidate(ISSUE)
            .latest_failure(head_sha=SHA_A, suite=ValidationGateKind.PUBLISH.suite)
        )

        assert failure is not None
        assert failure.stdout.tail == ""
        assert failure.stderr.tail == FAILING_STDERR
        assert failure.explains_the_failure is True


# ---------------------------------------------------------------------------
# Proof 4 — the PASS lane and the run directory are untouched
# ---------------------------------------------------------------------------


class TestThePassLaneIsUnchanged:
    """Acceptance 4: only a failure leaves a durable diagnostic."""

    def test_a_passing_gate_writes_no_durable_diagnostic(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        outcome = _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
            worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
        )

        assert outcome.allowed is True
        assert durable.all_directories() == []

    def test_an_unconfigured_publish_contract_writes_nothing(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        runner = StubCommandRunner(returncode=1)

        outcome = _gate(
            repo_root=repo_root, runner=runner, registry=_registry(publish_cmd=None)
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        assert runner.commands == []
        assert outcome.allowed is True
        assert durable.all_directories() == []

    def test_a_failure_still_writes_the_run_directory_evidence_it_always_did(
        self, repo_root: Path, worktree: Path
    ) -> None:
        """The durable copy is an addition, not a redirection.

        The outcome still carries the run-directory paths the manifest and the
        UI read, and they still hold the output.
        """
        run = _run(worktree)

        outcome = _gate(
            repo_root=repo_root, runner=StubCommandRunner(returncode=1)
        ).check(worktree=worktree, run_assets=run, issue_key=ISSUE)

        output_dir = publish_gate_output_dir(run.run_dir)
        assert outcome.evidence.paths.stdout_path == (
            output_dir / VALIDATION_STDOUT_NAME
        )
        assert (output_dir / VALIDATION_STDOUT_NAME).read_text() == FAILING_STDOUT
        assert (output_dir / VALIDATION_STDERR_NAME).read_text() == FAILING_STDERR

    def test_a_gate_with_no_candidate_identity_files_nothing(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """The manual-reprocess route, which holds no canonical issue key.

        The same answer #85 gives for the receipt, for the same reason: an
        artefact filed under an identity nothing else uses could not be found
        from the verdict it explains. It is warned about, not silently skipped.
        """
        outcome = _gate(
            repo_root=repo_root, runner=StubCommandRunner(returncode=1)
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=None)

        assert outcome.allowed is False
        assert durable.all_directories() == []

    def test_the_quick_gate_keeps_no_durable_failure_output(
        self, repo_root: Path, worktree: Path, durable: DurableDiagnostics
    ) -> None:
        """Only the publication gate is wired to the durable destination.

        A gate constructed without one behaves exactly as it did before: the
        run-directory write, and nothing else.
        """
        contract = _registry().resolve("default").contract(ValidationGateKind.QUICK)
        gate = ValidationGate(
            worktree=worktree,
            command_runner=StubCommandRunner(returncode=1),
            working_copy=StubWorkingCopy(),
            contract=contract,
        )

        result = gate.check(session_output_dir=worktree / "quick-out")

        assert result.allowed is False
        assert durable.all_directories() == []


# ---------------------------------------------------------------------------
# Proof 5 — the writer refuses to mislabel a pass
# ---------------------------------------------------------------------------


class TestTheWriterOnlyDescribesFailures:
    def test_the_trigger_is_the_verdict_not_the_exit_code_alone(self) -> None:
        assert needs_durable_diagnostic(_record(passed=False)) is True
        assert needs_durable_diagnostic(_record(passed=True, exit_code=0)) is False
        # A killed command that happened to exit 0 finished no contract.
        assert (
            needs_durable_diagnostic(
                _record(passed=True, exit_code=0, timed_out=True)
            )
            is True
        )

    def test_a_passing_run_handed_to_the_writer_is_refused(
        self, repo_root: Path
    ) -> None:
        writer = GateFailureDiagnostics(repo_root).for_candidate(ISSUE)

        with pytest.raises(ValueError, match="failed run"):
            writer.record_failure(
                GateFailureOutput(
                    record=_record(passed=True, exit_code=0),
                    stdout="",
                    stderr="",
                )
            )

    def test_an_unwritable_destination_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        """A gate that already failed must not fail differently instead."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        writer = CandidateGateDiagnostics(
            failures_dir=blocked / "failures", issue_key=ISSUE
        )

        assert (
            writer.record_failure(
                GateFailureOutput(record=_record(), stdout="x", stderr="y")
            )
            is None
        )


# ---------------------------------------------------------------------------
# Proof 6 — the mutations these proofs depend on
# ---------------------------------------------------------------------------


class TestRemovingTheDurableWriteBreaksThesePins:
    """Acceptance 5: each binding is load-bearing, shown by removing it.

    Every test here applies one mutation to production code and asserts the
    *wrong* answer appears — the state the observed #93 failures were left in.
    If a mutation stopped changing the answer, the proof above it would be
    pinning nothing and this test would fail, which is the point.
    """

    def test_without_the_durable_write_cleanup_destroys_the_explanation(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutation the issue names: remove the write, lose the evidence."""
        monkeypatch.setattr(
            validation_module, "needs_durable_diagnostic", lambda record: False
        )
        run = _run(worktree)

        outcome = _gate(
            repo_root=repo_root, runner=StubCommandRunner(returncode=1)
        ).check(worktree=worktree, run_assets=run, issue_key=ISSUE)

        assert outcome.allowed is False
        durable = DurableDiagnostics(repo_root)
        assert durable.all_directories() == []
        # Ordinary cleanup, and the only account of the failure goes with it.
        _remove_tree(worktree)
        assert durable.for_candidate(ISSUE, SHA_A) == []
        with pytest.raises(AssertionError):
            durable.only_for_candidate(ISSUE, SHA_A)

    def test_without_the_exact_candidate_binding_one_failure_erases_another(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drop the commit from the artefact's name and A reads as A′."""
        monkeypatch.setattr(
            CandidateGateDiagnostics,
            "_destination_for",
            lambda self, head_sha, suite: (
                repo_root / GATE_FAILURES_DIR / "the-only-failure"
            ),
        )

        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="A failed here"),
            head_sha=SHA_A,
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)
        _gate(
            repo_root=repo_root,
            runner=StubCommandRunner(returncode=1, stdout="A-prime failed there"),
            head_sha=SHA_PRIME,
        ).check(worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE)

        durable = DurableDiagnostics(repo_root)
        surviving = durable.all_directories()
        assert len(surviving) == 1
        # A's explanation is gone, and what is left claims to be about A′ while
        # sitting where a reader looking for A would find it.
        assert durable.stdout(surviving[0]) == "A-prime failed there"
        assert durable.for_candidate(ISSUE, SHA_A) == []

    def test_without_the_verdict_trigger_a_pass_is_described_as_a_failure(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The PASS-lane guard is load-bearing, not decoration."""
        monkeypatch.setattr(
            validation_module, "needs_durable_diagnostic", lambda record: True
        )

        with pytest.raises(ValueError, match="failed run"):
            _gate(repo_root=repo_root, runner=StubCommandRunner()).check(
                worktree=worktree, run_assets=_run(worktree), issue_key=ISSUE
            )


# ---------------------------------------------------------------------------
# Helpers used above
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


def _sidecar(repo_root: Path, head_sha: str = SHA_A) -> Path:
    attempts = repo_root / ".issue-orchestrator" / "attempts"
    found = sorted(attempts.glob(f"*--{head_sha}.json"))
    assert len(found) == 1, f"expected one attempt sidecar, got {found}"
    return found[0]
