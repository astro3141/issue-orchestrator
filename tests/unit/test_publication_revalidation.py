"""The bounded same-SHA revalidation route, proved by its refusals (#139).

The route exists to admit *one* mechanical re-evaluation of an artifact that
already carries durable evidence, without that becoming a way around the gate.
So the interesting proofs are the seven directions in which it must refuse, and
each is asserted by observing that the publish command was never executed —
``StubCommandRunner.commands`` — rather than by inspecting the route's
reasoning.

Real git, real sidecar store, real session output. The exact-SHA claim cannot
be proved against a fake worktree: the candidate commit and its branch head are
genuinely different commits here, the checkout is a real detached worktree, and
the SHA in the appended receipt is read out of it by the real working copy.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.publication_gate import (
    PublicationGate,
    RunValidationContracts,
)
from issue_orchestrator.control.publication_revalidation import (
    PublicationRevalidation,
    RevalidationOutcome,
)
from issue_orchestrator.control.publication_verdict import PublicationVerdictReceipts
from issue_orchestrator.control.publish_gate_diagnostics import PublishGateDiagnostics
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_candidate_checkouts import (
    GitCandidateCheckouts,
    build_candidate_checkouts,
)
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
    ValidationProfileConfig,
)
from issue_orchestrator.infra.validation_profiles import ValidationProfileRegistry
from issue_orchestrator.ports.candidate_checkout import (
    CandidateCheckoutError,
    MaterializedCandidate,
)

ISSUE = GitHubIssueKey(repo="acme/repo", external_id="139")
PUBLISH_SENTINEL = "run-the-publish-contract"
QUICK_SENTINEL = "run-the-quick-contract"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class StubCommandRunner:
    """Reports a fixed outcome for the gate's command, and remembers the asks."""

    def __init__(self, returncode: int = 0, timed_out: bool = False) -> None:
        self.returncode = returncode
        self.timed_out = timed_out
        self.commands: list[str] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.commands.append(command)
        return SimpleNamespace(
            returncode=self.returncode, stdout="", stderr="", timed_out=self.timed_out
        )


class StubAttemptKeys:
    def for_validation_attempt(self, *, issue_key, head_sha: str) -> AttemptKey:
        return AttemptKey(issue_key, head_sha)


class ExplodingCheckouts:
    """Materialization that dies after the allowance has been reserved.

    Stands in for every way execution can end between step 3 (reserve) and
    step 7 (append): a crash, a kill, an orchestrator restart.
    """

    def __init__(self, attempts: SidecarAttemptStore, key: AttemptKey) -> None:
        self._attempts = attempts
        self._key = key
        self.budget_at_materialize: int | None = None

    def materialize(self, head_sha: str) -> MaterializedCandidate:
        durable = self._attempts.for_key(self._key)
        self.budget_at_materialize = (
            durable.revalidation_budget_used if durable is not None else None
        )
        raise CandidateCheckoutError("interrupted before the gate could run")

    def release(self, candidate: MaterializedCandidate) -> None:  # pragma: no cover
        raise AssertionError("nothing was materialized")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    """A repo whose branch head has moved past the recorded candidate."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "t@example.com")
    _git(repo_root, "config", "user.name", "T")
    (repo_root / "candidate.txt").write_text("the evaluated artifact\n")
    _git(repo_root, "add", "candidate.txt")
    _git(repo_root, "commit", "-q", "-m", "candidate")
    candidate_sha = _git(repo_root, "rev-parse", "HEAD")

    (repo_root / "later.txt").write_text("work done after the candidate\n")
    _git(repo_root, "add", "later.txt")
    _git(repo_root, "commit", "-q", "-m", "later")
    branch_head = _git(repo_root, "rev-parse", "HEAD")

    assert candidate_sha != branch_head
    return SimpleNamespace(
        root=repo_root,
        candidate_sha=candidate_sha,
        branch_head=branch_head,
        checkout_base=repo_root.parent / f"{repo_root.name}-revalidations",
    )


def _registry(
    *,
    publish_cmd: str | None = PUBLISH_SENTINEL,
    profile_name: str = "default",
) -> ValidationProfileRegistry:
    quick = ValidationCommandConfig(cmd=QUICK_SENTINEL, timeout_seconds=111)
    publish = PublishValidationConfig(cmd=publish_cmd, timeout_seconds=222)
    if profile_name == "default":
        return ValidationProfileRegistry(ValidationConfig(quick=quick, publish=publish))
    return ValidationProfileRegistry(
        ValidationConfig(
            profiles={profile_name: ValidationProfileConfig(quick=quick, publish=publish)}
        )
    )


def _receipt(
    head_sha: str,
    *,
    verdict: ValidationVerdict = ValidationVerdict.FAILED,
    suite: str = "publish_gate",
    command: str = PUBLISH_SENTINEL,
    profile: str = "default",
) -> ValidationVerdictReceipt:
    return ValidationVerdictReceipt(
        suite=suite,
        head_sha=head_sha,
        verdict=verdict,
        command=command,
        profile=profile,
    )


def _file(
    repo: SimpleNamespace,
    receipt: ValidationVerdictReceipt | None,
    *,
    budget_used: int = 0,
    issue=ISSUE,
) -> AttemptKey:
    """Durably record one candidate, exactly as the orchestrator would."""
    key = AttemptKey(issue, repo.candidate_sha)
    store = SidecarAttemptStore(repo.root)

    def seed(attempt: Attempt) -> Attempt:
        seeded = attempt if receipt is None else attempt.with_completed_evaluation(receipt)
        for _ in range(budget_used):
            seeded = seeded.with_revalidation_reserved()
        return seeded

    store.update(key, seed)
    return key


def _checkouts(repo: SimpleNamespace) -> GitCandidateCheckouts:
    """Exactly what the composition root builds, pointed at this fixture."""
    checkouts = build_candidate_checkouts(
        repo_root=repo.root, command_runner=LocalCommandRunner()
    )
    assert isinstance(checkouts, GitCandidateCheckouts)
    return checkouts


def _route(
    repo: SimpleNamespace,
    runner: StubCommandRunner,
    *,
    registry: ValidationProfileRegistry | None = None,
    checkouts=None,
) -> PublicationRevalidation:
    """The route, assembled exactly as ``build_publication_revalidation`` does."""
    profiles = registry or _registry()
    store = SidecarAttemptStore(repo.root)
    return PublicationRevalidation(
        attempts=store,
        profiles=lambda: profiles,
        checkouts=checkouts
        if checkouts is not None
        else _checkouts(repo),
        session_output=FileSystemSessionOutput(),
        publication_gate=PublicationGate(
            contracts=RunValidationContracts(FileSystemSessionOutput(), profiles),
            command_runner=runner,
            # The real working copy: the SHA the receipt claims is the SHA the
            # materialized checkout actually sits at, not one a stub supplied.
            working_copy=GitWorkingCopy(),
            verdicts=PublicationVerdictReceipts(store, StubAttemptKeys()),
            diagnostics=PublishGateDiagnostics(repo.root),
        ),
    )


def _read(repo: SimpleNamespace, key: AttemptKey) -> Attempt | None:
    """A *fresh* store instance — the process-restart case, every time."""
    return SidecarAttemptStore(repo.root).for_key(key)


# ---------------------------------------------------------------------------
# The route does its job
# ---------------------------------------------------------------------------


class TestOneRevalidationIsAdmitted:
    def test_the_unchanged_contract_reruns_against_the_exact_recorded_sha(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.started is True
        assert outcome.reason == "revalidation_completed"
        assert runner.commands == [PUBLISH_SENTINEL]
        assert outcome.evaluation is not None
        assert outcome.evaluation.verdict is ValidationVerdict.PASSED
        assert outcome.evaluation.head_sha == repo.candidate_sha

    def test_the_checkout_is_released_when_the_route_is_done(
        self, repo: SimpleNamespace
    ) -> None:
        """A disposable checkout that outlives its run blocks the next one."""
        key = _file(repo, _receipt(repo.candidate_sha))

        _route(repo, StubCommandRunner()).revalidate(Attempt(key))

        leftovers = (
            sorted(repo.checkout_base.iterdir())
            if repo.checkout_base.exists()
            else []
        )
        assert leftovers == []

    def test_the_prior_failure_is_still_readable_in_order_afterwards(
        self, repo: SimpleNamespace
    ) -> None:
        """Direction 5: no path rewrites, reorders or drops the earlier FAIL."""
        prior = _receipt(repo.candidate_sha)
        key = _file(repo, prior)

        _route(repo, StubCommandRunner()).revalidate(Attempt(key))

        attempt = _read(repo, key)
        assert attempt is not None
        assert len(attempt.completed_evaluations) == 2
        assert attempt.completed_evaluations[0] == prior
        assert attempt.completed_evaluations[1].verdict is ValidationVerdict.PASSED

    def test_downstream_admission_consumes_the_latest_evaluation(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))
        before = _read(repo, key)
        assert before is not None and before.publication_validation_passed is False

        _route(repo, StubCommandRunner()).revalidate(Attempt(key))

        after = _read(repo, key)
        assert after is not None
        assert after.publication_validation_passed is True

    def test_a_failing_revalidation_is_appended_as_a_failure(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))

        outcome = _route(repo, StubCommandRunner(returncode=1)).revalidate(Attempt(key))

        assert outcome.evaluation is not None
        assert outcome.evaluation.verdict is ValidationVerdict.FAILED
        attempt = _read(repo, key)
        assert attempt is not None
        assert attempt.publication_validation_passed is False
        assert len(attempt.completed_evaluations) == 2


class TestTheCandidateIsMaterializedAtItsOwnCommit:
    """Direction 6: a moved branch is not revalidated in the candidate's place."""

    def test_the_receipt_names_the_candidate_not_the_advanced_branch_head(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))

        outcome = _route(repo, StubCommandRunner()).revalidate(Attempt(key))

        assert outcome.evaluation is not None
        assert outcome.evaluation.head_sha == repo.candidate_sha
        assert outcome.evaluation.head_sha != repo.branch_head

    def test_the_checkout_holds_the_candidate_tree_not_the_branch_tree(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))
        seen: dict[str, bool] = {}
        checkouts = _checkouts(repo)
        original = checkouts.materialize

        def observing(head_sha: str) -> MaterializedCandidate:
            candidate = original(head_sha)
            seen["later_present"] = (candidate.path / "later.txt").exists()
            seen["candidate_present"] = (candidate.path / "candidate.txt").exists()
            return candidate

        checkouts.materialize = observing  # type: ignore[method-assign]

        _route(repo, StubCommandRunner(), checkouts=checkouts).revalidate(Attempt(key))

        assert seen == {"later_present": False, "candidate_present": True}

    def test_a_commit_the_repository_does_not_hold_cannot_be_materialized(
        self, repo: SimpleNamespace
    ) -> None:
        missing = "c" * 40

        with pytest.raises(CandidateCheckoutError):
            _checkouts(repo).materialize(missing)


# ---------------------------------------------------------------------------
# The seven failure directions
# ---------------------------------------------------------------------------


class TestTheCompositionRootAssemblesTheRoute:
    def test_the_production_wiring_builds_a_working_route(
        self, repo: SimpleNamespace
    ) -> None:
        """The route production gets, not one this module hand-assembled."""
        from issue_orchestrator.entrypoints.bootstrap_revalidation import (
            build_publication_revalidation,
        )
        from issue_orchestrator.infra.config import Config

        config = Config(repo_root=repo.root)
        config.validation.publish.cmd = PUBLISH_SENTINEL
        key = _file(repo, _receipt(repo.candidate_sha, command=PUBLISH_SENTINEL))

        route = build_publication_revalidation(
            config,
            attempt_store=SidecarAttemptStore(repo.root),
            session_output=FileSystemSessionOutput(),
            command_runner=LocalCommandRunner(),
            working_copy=GitWorkingCopy(),
        )
        outcome = route.revalidate(Attempt(key))

        assert isinstance(route, PublicationRevalidation)
        assert outcome.started is True
        assert outcome.evaluation is not None
        assert outcome.evaluation.head_sha == repo.candidate_sha


class TestOnlyADurableCanonicalCandidateAdmits:
    """Direction 1: identity spoof / downgrade. Preserves #40."""

    def test_the_route_accepts_no_issue_number_url_or_title(self) -> None:
        parameters = inspect.signature(
            PublicationRevalidation.revalidate, eval_str=True
        ).parameters

        assert list(parameters) == ["self", "candidate"]
        assert parameters["candidate"].annotation is Attempt

    def test_a_key_reconstructed_from_an_issue_number_is_refused(
        self, repo: SimpleNamespace
    ) -> None:
        """The durable record is filed under the parsed external id, not #274."""
        filed_under = GitHubIssueKey(repo="acme/repo", external_id="M1-011")
        _file(repo, _receipt(repo.candidate_sha), issue=filed_under)
        reconstructed = AttemptKey(
            GitHubIssueKey(repo="acme/repo", external_id="274"), repo.candidate_sha
        )
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(reconstructed))

        assert outcome == RevalidationOutcome(
            started=False, reason="revalidation_candidate_not_durable"
        )
        assert runner.commands == []

    def test_a_candidate_with_no_durable_record_never_starts_a_gate(
        self, repo: SimpleNamespace
    ) -> None:
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(
            Attempt(AttemptKey(ISSUE, repo.candidate_sha))
        )

        assert outcome.started is False
        assert outcome.reason == "revalidation_candidate_not_durable"
        assert runner.commands == []
        assert not repo.checkout_base.exists()

    def test_the_supplied_records_own_fields_are_not_trusted(
        self, repo: SimpleNamespace
    ) -> None:
        """A caller cannot hand in a record claiming an unspent allowance."""
        key = _file(repo, _receipt(repo.candidate_sha), budget_used=1)
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(
            Attempt(key, revalidation_budget_used=0)
        )

        assert outcome.reason == "revalidation_allowance_consumed"
        assert runner.commands == []


class TestTheAllowanceIsExactlyOne:
    """Direction 2: second retry."""

    def test_a_spent_allowance_refuses_before_any_gate_work(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha), budget_used=1)
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.started is False
        assert outcome.reason == "revalidation_allowance_consumed"
        assert runner.commands == []

    def test_a_second_revalidation_of_the_same_candidate_is_refused(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))
        first_runner = StubCommandRunner(returncode=1)
        _route(repo, first_runner).revalidate(Attempt(key))
        assert first_runner.commands == [PUBLISH_SENTINEL]

        second_runner = StubCommandRunner()
        outcome = _route(repo, second_runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_allowance_consumed"
        assert second_runner.commands == []

    def test_a_passing_candidate_is_not_revalidated_at_all(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(
            repo,
            _receipt(repo.candidate_sha, verdict=ValidationVerdict.PASSED),
        )
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_latest_evaluation_passed"
        assert runner.commands == []


class TestADriftedContractIsADifferentQuestion:
    """Direction 3: contract / profile / command drift."""

    def test_a_changed_publish_command_refuses(self, repo: SimpleNamespace) -> None:
        key = _file(repo, _receipt(repo.candidate_sha, command="the-old-command"))
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_contract_changed:command"
        assert runner.commands == []

    def test_a_profile_whose_contract_changed_refuses(
        self, repo: SimpleNamespace
    ) -> None:
        """The profile name still resolves; what it names is not what ran.

        A profile is a *name* for a contract, and the name outliving a change
        to the contract behind it is precisely why the receipt carries the
        command as well as the profile (#7059). Judged against the receipt's
        own frozen profile, never against the default or a re-selected one.
        """
        key = _file(
            repo,
            _receipt(
                repo.candidate_sha, profile="foundation", command="the-old-command"
            ),
        )
        runner = StubCommandRunner()

        outcome = _route(
            repo, runner, registry=_registry(profile_name="foundation")
        ).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_contract_changed:command"
        assert runner.commands == []

    def test_a_retired_profile_refuses(self, repo: SimpleNamespace) -> None:
        key = _file(repo, _receipt(repo.candidate_sha, profile="retired"))
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_profile_retired"
        assert runner.commands == []

    def test_a_quick_contract_evaluation_is_not_a_publication_evaluation(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha, suite="agent_gate"))
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_no_completed_evaluation"
        assert runner.commands == []

    def test_a_never_gated_candidate_is_not_revalidated(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, None)
        runner = StubCommandRunner()

        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_no_completed_evaluation"
        assert runner.commands == []


class TestDiagnosticExecutionsNeverBecomeAuthority:
    """Direction 4: a prepush / keyless execution appends nothing."""

    def test_a_keyless_gate_run_appends_no_evaluation(
        self, repo: SimpleNamespace
    ) -> None:
        prior = _receipt(repo.candidate_sha)
        key = _file(repo, prior)
        runner = StubCommandRunner()
        checkout = _checkouts(repo).materialize(repo.candidate_sha)

        # The manual-reprocess shape: the full gate, on the very candidate the
        # history is about, holding no canonical identity to file under.
        outcome = _keyless_gate(repo, runner).check(
            worktree=checkout.path,
            run_assets=FileSystemSessionOutput().start_run(
                checkout.path, "manual-reprocess", validation_profile="default"
            ),
            issue_key=None,
        )

        assert outcome.allowed is True
        assert runner.commands == [PUBLISH_SENTINEL]
        attempt = _read(repo, key)
        assert attempt is not None
        assert attempt.completed_evaluations == (prior,)
        assert attempt.publication_validation_passed is False

    def test_a_quick_gate_pass_never_satisfies_publication_authority(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))
        SidecarAttemptStore(repo.root).update(
            key,
            lambda attempt: attempt.with_completed_evaluation(
                _receipt(
                    repo.candidate_sha,
                    verdict=ValidationVerdict.PASSED,
                    suite="agent_gate",
                    command=QUICK_SENTINEL,
                )
            ),
        )

        attempt = _read(repo, key)
        assert attempt is not None
        assert attempt.publication_validation_passed is False
        assert attempt.latest_publication_evaluation is not None
        assert attempt.latest_publication_evaluation.verdict is ValidationVerdict.FAILED


class TestAnInterruptedRevalidationFailsClosed:
    """Direction 7: crash / restart after reservation."""

    def test_the_allowance_is_durable_before_the_gate_is_reached(
        self, repo: SimpleNamespace
    ) -> None:
        key = _file(repo, _receipt(repo.candidate_sha))
        checkouts = ExplodingCheckouts(SidecarAttemptStore(repo.root), key)
        runner = StubCommandRunner()

        outcome = _route(repo, runner, checkouts=checkouts).revalidate(Attempt(key))

        # Read from the durable sidecar at the moment materialization began:
        # the reservation is on disk before any external gate work starts.
        assert checkouts.budget_at_materialize == 1
        assert outcome.started is True
        assert outcome.reason == "revalidation_candidate_unmaterializable"
        assert runner.commands == []

    def test_after_restart_no_second_revalidation_can_start(
        self, repo: SimpleNamespace
    ) -> None:
        prior = _receipt(repo.candidate_sha)
        key = _file(repo, prior)
        _route(
            repo,
            StubCommandRunner(),
            checkouts=ExplodingCheckouts(SidecarAttemptStore(repo.root), key),
        ).revalidate(Attempt(key))

        # Everything below reads through fresh instances: the restart case.
        runner = StubCommandRunner()
        outcome = _route(repo, runner).revalidate(Attempt(key))

        assert outcome.reason == "revalidation_allowance_consumed"
        assert runner.commands == []
        attempt = _read(repo, key)
        assert attempt is not None
        assert attempt.completed_evaluations == (prior,)
        assert attempt.latest_publication_evaluation == prior


def _keyless_gate(repo: SimpleNamespace, runner: StubCommandRunner) -> PublicationGate:
    profiles = _registry()
    return PublicationGate(
        contracts=RunValidationContracts(FileSystemSessionOutput(), profiles),
        command_runner=runner,
        working_copy=GitWorkingCopy(),
        verdicts=PublicationVerdictReceipts(
            SidecarAttemptStore(repo.root), StubAttemptKeys()
        ),
        diagnostics=PublishGateDiagnostics(repo.root),
    )
