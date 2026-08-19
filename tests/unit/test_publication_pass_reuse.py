"""A durable exact-A publication PASS is reused, not re-run (#159).

The defect, from the terminal canary the issue was filed against::

    eval[0] FAILED  # the original gate run
    eval[1] PASSED  # #139's bounded same-SHA revalidation
    eval[2] FAILED  # a fresh continuation worktree re-ran the publish gate

The third evaluation should never have existed. ``PublicationGate`` built its
``ValidationGate`` without an attempt identity, so its only cache was the
publish record store *inside the worktree it was running in*. A continuation
re-enters completion from a checkout that has never run anything, finds an
empty cache, and re-executes the whole publication contract — appending a
result that displaces the legitimate PASS as latest authority and locks the
candidate out of review.

What is pinned here is that the gate consults the candidate's durable
evaluation history through the owner that already exists
(:class:`~issue_orchestrator.control.candidate_evaluations.CandidateEvaluations`),
and every direction in which that reuse must *not* happen: another commit,
another contract, a later non-PASS, a caller with no canonical identity, and a
failure.

No GitHub adapter, no network and no repository host is constructed anywhere in
this module.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.publication_gate import (
    PublicationGate,
    PublicationGateOutcome,
    RunValidationContracts,
)
from issue_orchestrator.control.publish_gate_diagnostics import PublishGateDiagnostics
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
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
)
from issue_orchestrator.infra.validation_profiles import ValidationProfileRegistry

ISSUE = GitHubIssueKey(repo="acme/repo", external_id="159")
SHA_A = "a" * 40
SHA_PRIME = "b" * 40
PUBLISH_SENTINEL = "run-the-publish-contract"
QUICK_SENTINEL = "run-the-quick-contract"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class StubCommandRunner:
    """Reports a fixed outcome, and remembers what it was asked to run."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[str] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.commands.append(command)
        return SimpleNamespace(
            returncode=self.returncode, stdout="", stderr="", timed_out=False
        )


class StubWorkingCopy:
    def __init__(self, head_sha: str = SHA_A) -> None:
        self._head_sha = head_sha

    def get_head_sha(self, worktree: Path) -> str:
        return self._head_sha


class RecordingAttemptKeys:
    """The production derivation, and a record of every identity asked for.

    Asked-for identities are the observable that separates "consulted nothing"
    from "consulted and missed": a caller with no canonical issue key must not
    synthesize one, and the only way to see that is to watch this port.
    """

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []

    def for_validation_attempt(self, *, issue_key, head_sha: str) -> AttemptKey:
        key = AttemptKey(issue_key, head_sha)
        self.requested.append((str(key.issue_key), key.head_sha))
        return key


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _registry(
    *, publish_cmd: str | None = PUBLISH_SENTINEL
) -> ValidationProfileRegistry:
    return ValidationProfileRegistry(
        ValidationConfig(
            quick=ValidationCommandConfig(cmd=QUICK_SENTINEL, timeout_seconds=111),
            publish=PublishValidationConfig(cmd=publish_cmd, timeout_seconds=222),
        )
    )


def _gate(
    *,
    repo_root: Path,
    runner: StubCommandRunner,
    head_sha: str = SHA_A,
    registry: ValidationProfileRegistry | None = None,
    attempt_keys: RecordingAttemptKeys | None = None,
) -> PublicationGate:
    """A gate whose durable evidence lands in ``repo_root``, not a worktree.

    A *new* gate per run on purpose. Every reuse this module asserts has to
    come out of durable state, not out of an object that happens to have run
    the contract earlier in the same test.
    """
    return PublicationGate(
        contracts=RunValidationContracts(
            FileSystemSessionOutput(), registry or _registry()
        ),
        command_runner=runner,
        working_copy=StubWorkingCopy(head_sha),
        attempts=SidecarAttemptStore(repo_root),
        attempt_keys=attempt_keys or RecordingAttemptKeys(),
        diagnostics=PublishGateDiagnostics(repo_root),
    )


def _worktree(tmp_path: Path, name: str) -> Path:
    """A checkout that has never validated anything, every time."""
    path = tmp_path / name
    path.mkdir()
    return path


def _run(worktree: Path):
    return FileSystemSessionOutput().start_run(
        worktree,
        worktree.name,
        issue_number=159,
        validation_profile="default",
    )


def _check(
    *,
    repo_root: Path,
    worktree: Path,
    runner: StubCommandRunner,
    head_sha: str = SHA_A,
    registry: ValidationProfileRegistry | None = None,
    issue_key=ISSUE,
    attempt_keys: RecordingAttemptKeys | None = None,
) -> PublicationGateOutcome:
    """One publication-gate pass over one checkout."""
    gate = _gate(
        repo_root=repo_root,
        runner=runner,
        head_sha=head_sha,
        registry=registry,
        attempt_keys=attempt_keys,
    )
    return gate.check(
        worktree=worktree, run_assets=_run(worktree), issue_key=issue_key
    )


def _read(repo_root: Path, head_sha: str = SHA_A) -> Attempt | None:
    """A *fresh* store instance — the process-restart case, every time."""
    return SidecarAttemptStore(repo_root).for_key(AttemptKey(ISSUE, head_sha))


def _verdicts(repo_root: Path, head_sha: str = SHA_A) -> list[str]:
    attempt = _read(repo_root, head_sha)
    if attempt is None:
        return []
    return [
        f"{receipt.suite}:{receipt.verdict.value}"
        for receipt in attempt.completed_evaluations
    ]


def _seed(
    repo_root: Path,
    *,
    verdict: ValidationVerdict,
    head_sha: str = SHA_A,
    suite: str = "publish_gate",
    command: str = PUBLISH_SENTINEL,
    profile: str = "default",
) -> None:
    """Put one completed evaluation on the durable record, as a gate would."""
    receipt = ValidationVerdictReceipt(
        suite=suite,
        head_sha=head_sha,
        verdict=verdict,
        command=command,
        profile=profile,
    )
    SidecarAttemptStore(repo_root).update(
        AttemptKey(ISSUE, head_sha),
        lambda attempt: attempt.with_completed_evaluation(receipt),
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """The primary checkout: what the attempt sidecars survive in."""
    path = tmp_path / "repo"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 1-3. The live defect
# ---------------------------------------------------------------------------


class TestTheLiveRegressionShape:
    """FAIL(A) -> #139 PASS(A) -> fresh continuation checkout at A."""

    @staticmethod
    def _through_the_canary(repo_root: Path, tmp_path: Path) -> StubCommandRunner:
        """Reproduce eval[0] and eval[1], then destroy what produced them.

        The revalidation checkout is removed rather than left in place because
        that is what #139 does with it: it is materialized for the gate and
        released immediately after. Every byte of evidence the PASS produced
        goes with it, which is precisely why the receipt has to be enough.
        """
        coder = _worktree(tmp_path, "coder")
        first = _check(
            repo_root=repo_root, worktree=coder, runner=StubCommandRunner(returncode=1)
        )
        assert first.allowed is False

        revalidation = _worktree(tmp_path, "revalidate-159")
        second = _check(
            repo_root=repo_root,
            worktree=revalidation,
            runner=StubCommandRunner(returncode=0),
        )
        assert second.allowed is True
        assert second.cache_hit is False
        shutil.rmtree(revalidation)
        shutil.rmtree(coder)
        return StubCommandRunner(returncode=1)

    def test_a_fresh_continuation_checkout_reuses_the_durable_pass(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 1: the third completed evaluation must not exist."""
        continuation_runner = self._through_the_canary(repo_root, tmp_path)
        continuation = _worktree(tmp_path, "continuation-159")

        outcome = _check(
            repo_root=repo_root, worktree=continuation, runner=continuation_runner
        )

        # The gate allowed publication on the durable PASS alone...
        assert outcome.allowed is True
        assert outcome.cache_hit is True
        # ...without executing the publication contract. The runner is rigged
        # to FAIL, so a single execution here would both prove the re-run and
        # reproduce the canary's eval[2].
        assert continuation_runner.commands == []
        # Two evaluations, both from before this pass: the canary's eval[2] has
        # no counterpart here.
        assert len(_verdicts(repo_root)) == 2

    def test_the_history_still_reads_exactly_fail_then_pass(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 2: reuse appends nothing, and erases nothing."""
        continuation_runner = self._through_the_canary(repo_root, tmp_path)

        _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, "continuation-159"),
            runner=continuation_runner,
        )

        assert _verdicts(repo_root) == [
            "publish_gate:failed",
            "publish_gate:passed",
        ]

    def test_the_record_the_pass_was_reached_in_is_gone_and_that_is_not_a_miss(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 3: a dead record pointer does not erase a receipt.

        The attempt still points at the validation record the PASS was reached
        in, and that file went with the revalidation checkout. "Nothing to
        materialise" and "nothing was decided" are different facts, and reading
        the first as the second is what would send the contract off to run
        again.
        """
        continuation_runner = self._through_the_canary(repo_root, tmp_path)
        attempt = _read(repo_root)
        assert attempt is not None
        assert attempt.validation_record_path is not None
        assert not Path(attempt.validation_record_path).exists()

        outcome = _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, "continuation-159"),
            runner=continuation_runner,
        )

        assert outcome.allowed is True
        assert outcome.cache_hit is True
        assert continuation_runner.commands == []
        # A cache hit carrying no record: the verdict is the authority, and
        # inventing a record from it would claim exit codes no gate reported.
        assert outcome.record is None


# ---------------------------------------------------------------------------
# 4-6. What a PASS does not reach
# ---------------------------------------------------------------------------


class TestAPassAuthorizesExactlyOneCandidate:
    """Reuse is bounded by the two halves of ``(contract, commit)``."""

    def test_a_pass_on_a_does_not_satisfy_a_prime(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 4: A′ never inherits A's receipt."""
        _seed(repo_root, verdict=ValidationVerdict.PASSED)
        runner = StubCommandRunner()

        outcome = _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, "a-prime"),
            runner=runner,
            head_sha=SHA_PRIME,
        )

        assert runner.commands == [PUBLISH_SENTINEL]
        assert outcome.cache_hit is False
        # A's history is untouched, and A' now has one of its own.
        assert _verdicts(repo_root, SHA_A) == ["publish_gate:passed"]
        assert _verdicts(repo_root, SHA_PRIME) == ["publish_gate:passed"]

    @pytest.mark.parametrize(
        ("drift", "seeded"),
        [
            ("command", {"command": "run-some-other-publish-contract"}),
            ("suite", {"suite": "quick_gate", "command": QUICK_SENTINEL}),
            ("profile", {"profile": "foundation"}),
        ],
    )
    def test_contract_drift_is_a_miss_not_a_reuse(
        self, repo_root: Path, tmp_path: Path, drift: str, seeded: dict[str, str]
    ) -> None:
        """Requirement 5: a PASS answers for the contract that produced it.

        Including the ``suite`` case, which is the older defect seen from this
        side: the quick gate runs against the same candidate and files its
        receipt into the same history, and it must never read as publication
        authority however recent it is.
        """
        _seed(repo_root, verdict=ValidationVerdict.PASSED, **seeded)
        runner = StubCommandRunner()

        outcome = _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, f"drift-{drift}"),
            runner=runner,
        )

        assert runner.commands == [PUBLISH_SENTINEL]
        assert outcome.cache_hit is False

    def test_a_later_non_pass_is_not_hidden_by_an_older_pass(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 6: the *latest* matching evaluation decides.

        The history is append-only, so an older entry is still there after a
        newer one lands. A reader that took the first PASS it found would let a
        candidate publish on an evaluation that has since been superseded.
        """
        _seed(repo_root, verdict=ValidationVerdict.PASSED)
        _seed(repo_root, verdict=ValidationVerdict.FAILED)
        runner = StubCommandRunner(returncode=1)

        outcome = _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, "superseded"),
            runner=runner,
        )

        assert runner.commands == [PUBLISH_SENTINEL]
        assert outcome.allowed is False
        assert _verdicts(repo_root) == [
            "publish_gate:passed",
            "publish_gate:failed",
            "publish_gate:failed",
        ]


# ---------------------------------------------------------------------------
# 7-8. The directions that must not change
# ---------------------------------------------------------------------------


class TestExistingSemanticsAreUnchanged:
    """What #159 must leave exactly as it found it."""

    def test_a_caller_with_no_issue_identity_keeps_local_cache_semantics(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 7: no canonical identity, no synthesized AttemptKey.

        The manual-reprocess route holds an issue *number* from a URL path, and
        deriving a key from one is what #40 removed. So this route neither
        reads the durable history — the seeded PASS below is right there and
        must not be reached for — nor writes to it.
        """
        _seed(repo_root, verdict=ValidationVerdict.PASSED)
        keys = RecordingAttemptKeys()
        worktree = _worktree(tmp_path, "manual")
        runner = StubCommandRunner()

        first = _check(
            repo_root=repo_root,
            worktree=worktree,
            runner=runner,
            issue_key=None,
            attempt_keys=keys,
        )

        assert keys.requested == []
        assert runner.commands == [PUBLISH_SENTINEL]
        assert first.cache_hit is False
        # The durable history saw neither a read nor a write.
        assert _verdicts(repo_root) == ["publish_gate:passed"]

        # ...and the SHA-scoped local cache this route has always used still
        # answers the second pass, from records inside its own worktree.
        second = _check(
            repo_root=repo_root,
            worktree=worktree,
            runner=runner,
            issue_key=None,
            attempt_keys=keys,
        )

        assert second.cache_hit is True
        assert runner.commands == [PUBLISH_SENTINEL]
        assert keys.requested == []

    def test_a_durable_failure_still_re_runs_the_contract(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 8: a FAIL is never reusable as a decision.

        A failure can be about the environment rather than the candidate, which
        is the whole premise of #139's revalidation route. Reuse is for passes.
        """
        _seed(repo_root, verdict=ValidationVerdict.FAILED)
        runner = StubCommandRunner(returncode=0)

        outcome = _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, "after-failure"),
            runner=runner,
        )

        assert runner.commands == [PUBLISH_SENTINEL]
        assert outcome.cache_hit is False
        assert outcome.allowed is True
        assert _verdicts(repo_root) == [
            "publish_gate:failed",
            "publish_gate:passed",
        ]

    @pytest.mark.parametrize(
        "verdict", [ValidationVerdict.FAILED, ValidationVerdict.TIMED_OUT]
    )
    def test_no_non_pass_verdict_is_reusable(
        self, repo_root: Path, tmp_path: Path, verdict: ValidationVerdict
    ) -> None:
        """A timeout is a failure the route exists to disambiguate, not a pass."""
        _seed(repo_root, verdict=verdict)
        runner = StubCommandRunner()

        _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, f"non-pass-{verdict.value}"),
            runner=runner,
        )

        assert runner.commands == [PUBLISH_SENTINEL]

    def test_an_ordinary_completion_still_executes_its_publish_contract(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """Requirement 10: a managed worktree with nothing to reuse is unchanged.

        One command, one completed evaluation, and the gate's own evidence in
        the gate's own directory — the ordinary path, which #159 must cost
        nothing.
        """
        worktree = _worktree(tmp_path, "ordinary")
        runner = StubCommandRunner()

        outcome = _check(repo_root=repo_root, worktree=worktree, runner=runner)

        assert runner.commands == [PUBLISH_SENTINEL]
        assert outcome.allowed is True
        assert outcome.cache_hit is False
        assert outcome.record is not None
        assert outcome.record.suite == "publish_gate"
        assert outcome.record.command == PUBLISH_SENTINEL
        assert outcome.record.head_sha == SHA_A
        assert outcome.evidence.paths.record_path.exists()
        assert _verdicts(repo_root) == ["publish_gate:passed"]

    def test_an_unconfigured_publish_contract_binds_to_no_candidate(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """A gate that decides nothing files nothing and asks for no identity."""
        keys = RecordingAttemptKeys()
        runner = StubCommandRunner()

        outcome = _check(
            repo_root=repo_root,
            worktree=_worktree(tmp_path, "no-contract"),
            runner=runner,
            registry=_registry(publish_cmd=None),
            attempt_keys=keys,
        )

        assert outcome.allowed is True
        assert runner.commands == []
        assert keys.requested == []
        assert _read(repo_root) is None


# ---------------------------------------------------------------------------
# 9. One owner
# ---------------------------------------------------------------------------


class TestThereIsOneOwnerOfTheEvaluationHistory:
    """Requirement 9: nothing outside the owner interprets the history."""

    def test_the_continuation_runner_reads_no_evaluation_history(self) -> None:
        """The runner executes control operations; it decides no admission.

        A continuation that read the evaluations itself to work out whether the
        candidate had passed would be a second reader of the same evidence,
        free to answer differently from the gate that consults it and the
        admission that consumes it. Reuse belongs to the gate.
        """
        import issue_orchestrator.control.continuation_runner as runner_module

        source = Path(runner_module.__file__).read_text(encoding="utf-8")

        for accessor in (
            "completed_evaluations",
            "publication_evaluations",
            "latest_publication_evaluation",
            "publication_validation_passed",
        ):
            assert accessor not in source

    def test_the_gate_consults_the_history_through_the_shared_owner(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """The reuse is the owner's answer, not a second lookup of its own.

        Removing the owner's answer must remove the reuse: if the gate had any
        other way to reach the same conclusion, this mutation would leave the
        cache hit standing and the pin above would be pinning nothing.
        """
        from issue_orchestrator.control.candidate_evaluations import (
            CandidateEvaluations,
        )

        _seed(repo_root, verdict=ValidationVerdict.PASSED)
        runner = StubCommandRunner()

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(CandidateEvaluations, "prior", lambda self, head_sha: None)
            outcome = _check(
                repo_root=repo_root,
                worktree=_worktree(tmp_path, "no-owner"),
                runner=runner,
            )

        assert outcome.cache_hit is False
        assert runner.commands == [PUBLISH_SENTINEL]
