"""The reviewer worktree's gate-command guard, per provider (#396).

The review-exchange reviewer worktree is deliberately unprovisioned, so a
build/test/validation command run there measures the missing prerequisite
rather than the candidate. ``docs/architecture/hooks.md`` rules that the prompt
saying so is not what the invariant may rest on, and until #396 only a Claude
reviewer got a barrier: a Codex reviewer — which is what this repository's
default mode configures — was protected by prose alone.

These tests measure the installer's half of closing that. The Codex direction
is the one that needs measuring, because its guard is *data*: a file that reads
correctly to a human is worth nothing if the enforcing mechanism classifies it
differently, so "guarded" has to mean the mechanism was asked. Adding ``codex``
to a set of names would pass a set-membership test and refuse nothing, so no
test here asserts membership without also asserting that a policy was written
and verified refusing.

The other half — that a running Codex session loads a linked worktree's
``.codex/rules`` and refuses before process creation — is provider behaviour,
measured against the installed CLI in
``tests/integration/test_codex_reviewer_guard_live.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import (
    CODEX_SAFETY_RULES,
    GUARDABLE_PROVIDERS,
    REVIEW_COMMAND_GUARD_SETTINGS,
    REVIEW_GUARD_RULES,
    CodexReviewCommandGuardInstaller,
    ReviewCommandGuardOutcome,
    install_review_command_guard,
    render_review_rules,
)
from issue_orchestrator.domain.artifact_contracts import AgentProvider
from issue_orchestrator.execution.agent_runner_providers.codex_trust import (
    resolve_codex_common_repository_root,
)
from issue_orchestrator.infra.hooks.review_command_guard import REFUSAL_REASON
from issue_orchestrator.ports.review_command_guard import ReviewCommandGuardError
from issue_orchestrator.resources import get_review_exchange_reviewer_instructions

from tests.codex_execpolicy_fakes import (
    AlwaysAllowingExecPolicy,
    SafetyBlindExecPolicy,
    StarlarkPrefixExecPolicy,
    UnanswerableExecPolicy,
)

CODEX = AgentProvider("codex")
CLAUDE = AgentProvider("claude-code")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_stdout(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def product_checkout(tmp_path: Path) -> Path:
    """A real repository, standing in for the product checkout.

    This is the root the operator approved under #215 — the owner of the git
    *common* directory every worktree below belongs to.
    """
    root = tmp_path / "product"
    root.mkdir()
    _git(root, "init", "-q", ".")
    (root / "AGENTS.md").write_text("authority\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
    return root


@pytest.fixture
def reviewer_worktree(product_checkout: Path, tmp_path: Path) -> Path:
    """A real sibling reviewer worktree, detached, as the exchange creates it."""
    path = tmp_path / "issue-396-review-20260831T000000Z"
    _git(product_checkout, "worktree", "add", "-q", "--detach", str(path))
    return path


def _install(
    worktree: Path, provider: AgentProvider, policy: object = None
) -> ReviewCommandGuardOutcome:
    return install_review_command_guard(
        worktree,
        provider=provider,
        execpolicy=policy or StarlarkPrefixExecPolicy(),
    )


class TestTheCodexGuardIsEstablishedNotClaimed:
    """F1: ``guarded`` means a policy was written *and* verified refusing."""

    def test_a_codex_reviewer_gets_a_verified_worktree_local_policy(
        self, reviewer_worktree: Path
    ) -> None:
        policy = StarlarkPrefixExecPolicy()

        outcome = _install(reviewer_worktree, CODEX, policy)

        assert outcome.guarded is True
        assert outcome.policy_file == reviewer_worktree / REVIEW_GUARD_RULES
        assert outcome.policy_file.exists()
        # Established, not assumed: the enforcing mechanism was asked about the
        # file that was actually written.
        assert reviewer_worktree / REVIEW_GUARD_RULES in policy.asked_files

    def test_the_pinned_gate_commands_are_verified_refused(
        self, reviewer_worktree: Path
    ) -> None:
        """F2, in the deterministic direction."""
        policy = StarlarkPrefixExecPolicy()

        outcome = _install(reviewer_worktree, CODEX, policy)

        assert "make validate-pr-raw" in outcome.refusals()
        assert "pytest -q tests/unit" in outcome.refusals()
        assert "python -m pytest" in outcome.refusals()
        assert ("make", "validate-pr-raw") in policy.asked

    def test_reading_the_candidate_is_verified_still_possible(
        self, reviewer_worktree: Path
    ) -> None:
        """F3: the guard must not turn reviewing into a no-tools role."""
        outcome = _install(reviewer_worktree, CODEX)

        assert "git log --oneline -20" in outcome.allowances()
        assert "cat AGENTS.md" in outcome.allowances()
        assert any(
            allowance.startswith("rg -n") for allowance in outcome.allowances()
        )

    def test_the_rounds_own_way_out_is_verified_still_possible(
        self, reviewer_worktree: Path
    ) -> None:
        """``exchange-respond`` is how this principal records a verdict at all.

        The worktree this guard is installed in belongs to the review exchange,
        where ``reviewer-done`` is forbidden and ``exchange-respond`` is the
        only exit. Refusing it would deadlock the round rather than merely
        inconvenience the reviewer, so it is the allowance whose classification
        has to be measured.
        """
        outcome = _install(reviewer_worktree, CODEX)

        assert any(
            allowance.startswith("exchange-respond ok")
            for allowance in outcome.allowances()
        )

    def test_the_pinned_exit_is_the_one_this_lane_actually_uses(
        self, reviewer_worktree: Path
    ) -> None:
        """The exit is read off the reviewer's instructions, not remembered.

        A sample naming a command this principal must never run would prove
        nothing about the failure it exists to prevent, and no assertion inside
        the installer could notice the substitution — the reviewer's own
        instructions are where this lane's exit is decided, so they are what
        the pinned sample is held against.
        """
        instructions = get_review_exchange_reviewer_instructions()
        assert "**DO NOT** call `reviewer-done`" in instructions

        allowances = _install(reviewer_worktree, CODEX).allowances()

        assert any(
            allowance.startswith("exchange-respond ") for allowance in allowances
        )
        assert not any(
            allowance.startswith("reviewer-done ") for allowance in allowances
        )

    def test_the_policy_carries_the_shared_gate_vocabulary(
        self, reviewer_worktree: Path
    ) -> None:
        """One classifier: the same list the Claude reviewer's hook renders."""
        outcome = _install(reviewer_worktree, CODEX)
        content = outcome.policy_file.read_text(encoding="utf-8")

        assert 'pattern = ["make"]' in content
        assert 'pattern = ["pytest"]' in content
        assert 'decision = "forbidden"' in content

    def test_the_refusal_gives_the_reviewers_reason_not_the_planning_one(
        self, reviewer_worktree: Path
    ) -> None:
        """The principal is told why *its* worktree cannot answer the question."""
        outcome = _install(reviewer_worktree, CODEX)
        content = outcome.policy_file.read_text(encoding="utf-8")

        assert "no virtualenv" in content
        assert REFUSAL_REASON[:60] in content
        # #289's planning prose would be false here: a reviewer worktree is not
        # a session that prepares a bounded issue.
        assert "create_issue" not in content

    def test_rendering_is_deterministic(self) -> None:
        assert render_review_rules() == render_review_rules()


class TestTheShippedSafetyPolicyComposes:
    """F4: the reviewer policy is placed beside ``orchestrator.rules``."""

    def test_the_safety_rules_are_installed_beside_the_reviewer_policy(
        self, reviewer_worktree: Path
    ) -> None:
        _install(reviewer_worktree, CODEX)

        safety = reviewer_worktree / CODEX_SAFETY_RULES
        assert safety.exists()
        assert safety.parent == (reviewer_worktree / REVIEW_GUARD_RULES).parent
        content = safety.read_text(encoding="utf-8")
        assert 'pattern = ["git", "push", "--no-verify"]' in content
        assert 'pattern = ["gh", "pr", "merge"]' in content

    def test_the_safety_policy_is_verified_not_merely_copied(
        self, reviewer_worktree: Path
    ) -> None:
        policy = StarlarkPrefixExecPolicy()

        outcome = _install(reviewer_worktree, CODEX, policy)

        assert reviewer_worktree / CODEX_SAFETY_RULES in policy.asked_files
        assert "git push --no-verify" in outcome.refusals()
        assert "gh pr merge" in outcome.refusals()

    def test_a_safety_policy_that_stopped_refusing_fails_the_installation(
        self, reviewer_worktree: Path
    ) -> None:
        """A reviewer worktree must not be handed over half-protected."""
        with pytest.raises(
            ReviewCommandGuardError, match=r"orchestrator\.rules does not refuse"
        ):
            _install(reviewer_worktree, CODEX, SafetyBlindExecPolicy())


class TestEstablishmentFailsClosed:
    """F5: every direction in which a guard does not take is a failure."""

    def test_a_policy_that_refuses_nothing_does_not_establish(
        self, reviewer_worktree: Path
    ) -> None:
        with pytest.raises(ReviewCommandGuardError, match="does not refuse"):
            _install(reviewer_worktree, CODEX, AlwaysAllowingExecPolicy())

    def test_an_unclassifiable_answer_does_not_establish(
        self, reviewer_worktree: Path
    ) -> None:
        with pytest.raises(ReviewCommandGuardError, match="no classifiable answer"):
            _install(reviewer_worktree, CODEX, UnanswerableExecPolicy())

    def test_an_unwritable_worktree_does_not_establish(self, tmp_path: Path) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("", encoding="utf-8")

        with pytest.raises(ReviewCommandGuardError, match="Failed to write"):
            _install(blocked, CODEX)

    def test_a_safety_policy_that_cannot_be_installed_does_not_establish(
        self, reviewer_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third F5 direction: composition itself failed.

        A scoped policy can be written perfectly and still leave the worktree
        half-protected, because the no-bypass / no-merge rules are *copied in*
        from a shipped template — and a broken install (a moved or missing
        templates root) makes that copy raise where the scoped write did not.
        The real ``CodexAdapter`` is left in place and pointed at a templates
        root that does not exist, so the ``OSError`` this branch translates is
        raised by the production copy rather than by a double standing in for
        it.

        The branch lives in the shared :mod:`._codex_gate_policy` owner, so the
        planning principal is composed the same way and fails closed for the
        same reason.
        """
        monkeypatch.setattr(
            "issue_orchestrator.adapters.hooks.codex.TEMPLATES_DIR",
            tmp_path / "templates-that-did-not-ship",
        )

        with pytest.raises(
            ReviewCommandGuardError, match="Failed to install the Codex safety"
        ):
            _install(reviewer_worktree, CODEX)

        assert not (reviewer_worktree / CODEX_SAFETY_RULES).exists()

    def test_a_failure_is_raised_rather_than_reported_as_unguarded(
        self, reviewer_worktree: Path
    ) -> None:
        """The two facts must not collapse into one.

        "No mechanism for this provider" is a limitation the caller logs and
        decides about; "this provider's guard did not take" is a worktree the
        caller must roll back. Returning the first for the second is how a
        reviewer ends up running in a worktree everyone believes is guarded.
        """
        with pytest.raises(ReviewCommandGuardError):
            _install(reviewer_worktree, CODEX, AlwaysAllowingExecPolicy())


class TestOnlyRegisteredProvidersAreGuardable:
    """F8: no provider is made guardable by implication."""

    def test_every_guardable_provider_really_writes_a_policy(
        self, product_checkout: Path, tmp_path: Path
    ) -> None:
        """Membership cannot be claimed without a registration behind it.

        This is the test a "just add codex to the set" repair fails: the set is
        derived from the registration table, so a name in it must produce a
        file that exists.
        """
        for index, provider in enumerate(sorted(GUARDABLE_PROVIDERS)):
            worktree = tmp_path / f"guardable-{index}"
            _git(product_checkout, "worktree", "add", "-q", "--detach", str(worktree))

            outcome = _install(worktree, AgentProvider(provider))

            assert outcome.guarded is True, provider
            assert outcome.policy_file is not None
            assert outcome.policy_file.exists(), provider

    @pytest.mark.parametrize("provider", ["cursor", "gemini", "aider"])
    def test_an_unregistered_provider_gets_no_file_and_says_so(
        self, reviewer_worktree: Path, provider: str
    ) -> None:
        assert provider not in GUARDABLE_PROVIDERS

        outcome = _install(reviewer_worktree, AgentProvider(provider))

        assert outcome.guarded is False
        assert outcome.policy_file is None
        assert not (reviewer_worktree / ".claude").exists()
        assert not (reviewer_worktree / ".codex").exists()

    def test_a_claude_reviewer_still_gets_its_pinned_hook_and_nothing_codex(
        self, reviewer_worktree: Path
    ) -> None:
        """F7: the Claude registration is unchanged by the Codex one."""
        policy = StarlarkPrefixExecPolicy()

        outcome = _install(reviewer_worktree, CLAUDE, policy)

        assert outcome.policy_file == (
            reviewer_worktree / REVIEW_COMMAND_GUARD_SETTINGS
        )
        settings = json.loads(outcome.policy_file.read_text(encoding="utf-8"))
        assert [
            matcher["matcher"] for matcher in settings["hooks"]["PreToolUse"]
        ] == ["Bash"]
        # Claude's barrier is the orchestrator's own pinned policy module, not
        # a data file a checker has to be asked about.
        assert policy.asked == []
        assert outcome.probes == ()
        assert not (reviewer_worktree / ".codex").exists()


class TestWhatTheGuardLeavesBehind:
    def test_the_policy_lands_in_the_reviewer_worktree_only(
        self, reviewer_worktree: Path, product_checkout: Path
    ) -> None:
        """F6/F9: nothing outside this worktree gains a policy."""
        _install(reviewer_worktree, CODEX)

        assert not (product_checkout / ".codex").exists()
        assert _git_stdout(product_checkout, "status", "--porcelain") == ""

    def test_the_candidate_the_reviewer_reads_stays_clean(
        self, reviewer_worktree: Path
    ) -> None:
        _install(reviewer_worktree, CODEX)

        assert _git_stdout(reviewer_worktree, "status", "--porcelain") == ""
        assert (
            _git_stdout(reviewer_worktree, "ls-files", str(REVIEW_GUARD_RULES)).strip()
            == ""
        )

    def test_the_hiding_entry_lands_in_the_shared_exclude(
        self, reviewer_worktree: Path, product_checkout: Path
    ) -> None:
        """``info/`` is a common-dir path, so only the shared file takes effect."""
        _install(reviewer_worktree, CODEX)

        shared = (product_checkout / ".git" / "info" / "exclude").read_text()
        assert str(REVIEW_GUARD_RULES) in shared
        assert str(CODEX_SAFETY_RULES) in shared

    def test_the_shared_exclude_residue_is_bounded_not_per_exchange(
        self, reviewer_worktree: Path, product_checkout: Path, tmp_path: Path
    ) -> None:
        shared_file = product_checkout / ".git" / "info" / "exclude"
        _install(reviewer_worktree, CODEX)
        after_first = shared_file.read_text()

        second = tmp_path / "issue-396-review-20260831T000001Z"
        _git(product_checkout, "worktree", "add", "-q", "--detach", str(second))
        _install(second, CODEX)

        assert shared_file.read_text() == after_first
        assert after_first.count(str(REVIEW_GUARD_RULES)) == 1
        assert after_first.count(str(CODEX_SAFETY_RULES)) == 1

    def test_the_reviewer_worktree_is_not_provisioned_by_the_guard(
        self, reviewer_worktree: Path
    ) -> None:
        """F9: installing a barrier is not a licence to build an environment."""
        _install(reviewer_worktree, CODEX)

        assert not (reviewer_worktree / ".venv").exists()
        assert not (reviewer_worktree / "node_modules").exists()
        assert not (reviewer_worktree / ".issue-orchestrator").exists()


class TestTrustScopeIsUnchanged:
    """F6: the Codex reviewer guard rides on the existing #215 grant."""

    def test_the_reviewer_worktree_resolves_to_the_approved_common_root(
        self, reviewer_worktree: Path, product_checkout: Path
    ) -> None:
        """A sibling worktree's trust key is the root a human already approved.

        Codex keys workspace trust to the owner of the git *common* directory,
        so a reviewer worktree of an approved product checkout needs no new
        grant — which is what makes a worktree-local policy loadable there
        without widening trust.
        """
        assert resolve_codex_common_repository_root(reviewer_worktree) == (
            product_checkout.resolve()
        )

    def test_installing_the_guard_grants_no_trust_of_its_own(
        self, reviewer_worktree: Path, product_checkout: Path
    ) -> None:
        """The guard writes policy, never trust state.

        ``codex execpolicy check`` is asked about an explicit ``--rules`` file,
        so establishing the barrier neither reads nor writes the project-trust
        store. Nothing the installer touches lives outside the reviewer
        worktree and the repository's own untracked exclude file.
        """
        before = sorted(
            path.relative_to(product_checkout)
            for path in product_checkout.rglob("*")
            if ".git" not in path.parts
        )

        _install(reviewer_worktree, CODEX)

        after = sorted(
            path.relative_to(product_checkout)
            for path in product_checkout.rglob("*")
            if ".git" not in path.parts
        )
        assert after == before
        assert not (product_checkout / ".git" / "config.toml").exists()


class TestTheInstallerTheExchangeHolds:
    """The port implementation, and the checker it is pinned to.

    ``create_reviewer_worktree`` asks a
    :class:`~issue_orchestrator.ports.review_command_guard.ReviewCommandGuardInstaller`
    rather than reaching for this module, so the exchange can be exercised
    where the Codex CLI is absent without the reviewer worktree quietly going
    unguarded there. What must not follow from that seam is a checker the
    caller did not choose: an installer that ignored its own ``execpolicy``
    would answer every question with the operator's installed CLI while
    appearing to be under test.
    """

    def test_the_installer_establishes_the_same_verified_policy(
        self, reviewer_worktree: Path
    ) -> None:
        installer = CodexReviewCommandGuardInstaller(StarlarkPrefixExecPolicy())

        outcome = installer.establish(reviewer_worktree, provider=CODEX)

        assert outcome.guarded is True
        assert outcome.policy_file == reviewer_worktree / REVIEW_GUARD_RULES
        assert "make validate-pr-raw" in outcome.refusals()
        assert "cat AGENTS.md" in outcome.allowances()

    def test_the_injected_checker_is_the_one_asked(
        self, reviewer_worktree: Path
    ) -> None:
        policy = StarlarkPrefixExecPolicy()

        CodexReviewCommandGuardInstaller(policy).establish(
            reviewer_worktree, provider=CODEX
        )

        assert reviewer_worktree / REVIEW_GUARD_RULES in policy.asked_files
        assert reviewer_worktree / CODEX_SAFETY_RULES in policy.asked_files

    def test_the_installer_fails_closed_the_way_the_function_does(
        self, reviewer_worktree: Path
    ) -> None:
        installer = CodexReviewCommandGuardInstaller(AlwaysAllowingExecPolicy())

        with pytest.raises(ReviewCommandGuardError, match="does not refuse"):
            installer.establish(reviewer_worktree, provider=CODEX)

    def test_an_unregistered_provider_is_still_reported_unguarded(
        self, reviewer_worktree: Path
    ) -> None:
        installer = CodexReviewCommandGuardInstaller(StarlarkPrefixExecPolicy())

        outcome = installer.establish(reviewer_worktree, provider=AgentProvider("gemini"))

        assert outcome.guarded is False
        assert outcome.policy_file is None
