"""The launch-scoped Codex planning guard installer (#289).

R22 Pilot 4 was told, in its prompt, that a planning_investigation does not run
the repository's code-candidate validation gate. It ran ``make validate-pr-raw``
anyway, for seventeen minutes, inside a sandbox that could not satisfy it, and
returned BLOCKED without the bounded ``create_issue`` it was launched to
produce. These tests pin the barrier that replaces the instruction.

What they measure is the *installer's* half: where the policy is written, that
its content really is the shared gate vocabulary, that the shipped safety rules
are put beside it rather than replaced, and — the property #289 insists on —
that "guarded" is established by asking the enforcing mechanism rather than
inferred from a file existing. A policy the checker does not classify as
refusing fails to establish, loudly.

The other half — that a running Codex session loads a linked worktree's
``.codex/rules`` and refuses before process creation — is provider behaviour,
and is measured against the installed CLI in
``tests/integration/test_codex_planning_guard_live.py``.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import (
    PLANNING_GUARD_RULES,
    CodexPlanningCommandGuardInstaller,
    render_planning_rules,
)
from issue_orchestrator.adapters.hooks import (
    ExecPolicyOutcome,
    ExecPolicyResultError,
)
from issue_orchestrator.domain.artifact_contracts import AgentProvider
from issue_orchestrator.ports.planning_command_guard import (
    PlanningCommandGuardError,
)

CODEX = AgentProvider("codex")
CLAUDE = AgentProvider("claude-code")

_PATTERN_LINE = re.compile(r"^\s*pattern = (\[.*\]),\s*$")


class StarlarkPrefixExecPolicy:
    """Classifies a command against the rules file that was actually written.

    Reads the generated ``prefix_rule`` patterns back out of the file and
    applies Codex's documented prefix semantics (literal argv tokens, a list in
    one position meaning "any of these"). Nothing is stubbed per command, so
    these tests cannot pass by agreeing with themselves about what the policy
    says — only about what Codex would do with it, which the live module
    re-measures against the real CLI.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []
        self.asked_files: list[Path] = []

    @staticmethod
    def _patterns(rules_file: Path) -> list[list[object]]:
        patterns: list[list[object]] = []
        for line in rules_file.read_text(encoding="utf-8").splitlines():
            match = _PATTERN_LINE.match(line)
            if match:
                patterns.append(eval(match.group(1)))  # noqa: S307 - generated literal
        return patterns

    @staticmethod
    def _matches(pattern: list[object], command: Sequence[str]) -> bool:
        if len(pattern) > len(command):
            return False
        for expected, actual in zip(pattern, command):
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        self.asked.append(tuple(command))
        self.asked_files.append(rules_file)
        for pattern in self._patterns(rules_file):
            if self._matches(pattern, command):
                return ExecPolicyOutcome.FORBIDDEN
        return ExecPolicyOutcome.NO_MATCH


class SafetyBlindExecPolicy(StarlarkPrefixExecPolicy):
    """Classifies the planning policy normally; the safety policy refuses nothing.

    The shape of a shipped ``orchestrator.rules`` that arrived empty, truncated
    or superseded — the case a copy-and-return installer cannot distinguish
    from a working one.
    """

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        if rules_file.name == "orchestrator.rules":
            self.asked.append(tuple(command))
            self.asked_files.append(rules_file)
            return ExecPolicyOutcome.NO_MATCH
        return super().check(rules_file, command)


class AlwaysAllowingExecPolicy:
    """A mechanism that enforces nothing — the decorative-guard direction."""

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        return ExecPolicyOutcome.NO_MATCH


class UnanswerableExecPolicy:
    """A mechanism that cannot classify at all."""

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        raise ExecPolicyResultError("codex execpolicy check exited 1")


@pytest.fixture
def product_checkout(tmp_path: Path) -> Path:
    """A real repository, standing in for the product checkout."""
    root = tmp_path / "product"
    root.mkdir()
    _git(root, "init", "-q", ".")
    (root / "AGENTS.md").write_text("authority\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
    return root


@pytest.fixture
def worktree(product_checkout: Path, tmp_path: Path) -> Path:
    """A real linked worktree, the shape a focused Tech Lead run launches in."""
    path = tmp_path / "product-tech-lead-289-abc123"
    _git(
        product_checkout,
        "worktree",
        "add",
        "-q",
        str(path),
        "-b",
        "tech-lead-planning-289-abc123",
    )
    return path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_stdout(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _status(cwd: Path) -> str:
    return _git_stdout(cwd, "status", "--porcelain")


def _git_path(cwd: Path, relative: str) -> Path:
    """Resolve what git itself would read for ``relative`` from ``cwd``."""
    return Path(_git_stdout(cwd, "rev-parse", "--path-format=absolute",
                            "--git-path", relative).strip())


def _installer(policy: object = None) -> CodexPlanningCommandGuardInstaller:
    return CodexPlanningCommandGuardInstaller(
        execpolicy=policy or StarlarkPrefixExecPolicy()
    )


class TestWhereThePolicyLands:
    def test_the_policy_is_written_into_this_launch_worktree_only(
        self, worktree: Path, product_checkout: Path
    ) -> None:
        guard = _installer().establish(worktree, provider=CODEX)

        assert guard.policy_file == worktree / PLANNING_GUARD_RULES
        assert guard.policy_file.exists()
        # #289: no repository-global planning rule. An ordinary Codex Actor
        # working in the product checkout reads its rules, not this launch's.
        assert not (product_checkout / ".codex" / "rules").exists()

    def test_the_product_checkout_tracked_tree_stays_clean(
        self, worktree: Path, product_checkout: Path
    ) -> None:
        _installer().establish(worktree, provider=CODEX)

        assert _status(product_checkout) == ""

    def test_the_shipped_safety_rules_are_placed_beside_it_not_instead_of_it(
        self, worktree: Path
    ) -> None:
        _installer().establish(worktree, provider=CODEX)

        safety = worktree / ".codex" / "rules" / "orchestrator.rules"
        assert safety.exists()
        content = safety.read_text(encoding="utf-8")
        assert 'pattern = ["git", "push", "--no-verify"]' in content
        assert 'pattern = ["gh", "pr", "merge"]' in content

    def test_the_composed_safety_policy_is_verified_not_just_copied(
        self, worktree: Path
    ) -> None:
        """#289 acceptance 7 held to this module's own standard.

        The planning half is not allowed to call a written file a barrier, so
        the safety half is not either: the checker is asked about the safety
        rules file itself, and its answers ride in the guard's probe record.
        """
        policy = StarlarkPrefixExecPolicy()
        guard = _installer(policy).establish(worktree, provider=CODEX)

        safety = worktree / ".codex" / "rules" / "orchestrator.rules"
        assert ("git", "push", "--no-verify") in policy.asked
        assert ("gh", "pr", "merge") in policy.asked
        assert safety in policy.asked_files
        assert "git push --no-verify" in guard.refusals()
        assert "gh pr merge" in guard.refusals()

    def test_a_safety_policy_that_stopped_refusing_fails_the_launch(
        self, worktree: Path
    ) -> None:
        """A safety copy that no longer refuses must not ride along.

        The planning policy is classified normally here, so the only reason
        establishment fails is the safety file — which is the point:
        composition is claimed by #289 acceptance 7, so it is measured. Without
        this the guard would report a launch as fully protected while
        ``git push --no-verify`` was allowed in it.
        """
        with pytest.raises(
            PlanningCommandGuardError, match=r"orchestrator\.rules does not refuse"
        ):
            _installer(SafetyBlindExecPolicy()).establish(worktree, provider=CODEX)

    def test_the_policy_is_hidden_from_plain_git_status(
        self, worktree: Path
    ) -> None:
        _installer().establish(worktree, provider=CODEX)

        assert _status(worktree) == ""

    def test_the_hiding_entry_lands_in_the_shared_exclude_not_the_worktrees_one(
        self, worktree: Path, product_checkout: Path
    ) -> None:
        """Where the entry lands is the whole mechanism, so it is measured.

        ``info/`` is a common-dir path: ``git rev-parse --git-path
        info/exclude`` inside a linked worktree resolves to the *shared*
        ``.git/info/exclude``, and the per-worktree
        ``.git/worktrees/<name>/info/exclude`` is never read. Asserting on the
        per-worktree file would pass while proving nothing — the hiding would
        be coming from the shared write the assertion never mentioned.
        """
        _installer().establish(worktree, provider=CODEX)

        assert _git_path(worktree, "info/exclude") == (
            product_checkout / ".git" / "info" / "exclude"
        )
        shared = product_checkout / ".git" / "info" / "exclude"
        assert ".codex/rules/planning-gate.rules" in shared.read_text()
        assert ".codex/rules/orchestrator.rules" in shared.read_text()

    def test_the_shared_exclude_residue_is_bounded_not_per_launch(
        self, product_checkout: Path, worktree: Path, tmp_path: Path
    ) -> None:
        """#289 acceptance 9: the entry outlives the run, so it must not accrue.

        The shared exclude is repository-wide and this leaf does not remove its
        lines at teardown (removing them would unhide a concurrently live
        planning launch). What keeps that honest is that the write is
        idempotent: a second launch in a second worktree of the same repository
        adds nothing.
        """
        shared = product_checkout / ".git" / "info" / "exclude"
        _installer().establish(worktree, provider=CODEX)
        after_first = shared.read_text()

        second = tmp_path / "product-tech-lead-290-def456"
        _git(
            product_checkout,
            "worktree", "add", "-q", str(second), "-b", "tech-lead-planning-290",
        )
        _installer().establish(second, provider=CODEX)

        assert shared.read_text() == after_first
        assert after_first.count(".codex/rules/planning-gate.rules") == 1
        assert after_first.count(".codex/rules/orchestrator.rules") == 1

    def test_the_product_checkouts_own_codex_rules_are_still_reported(
        self, worktree: Path, product_checkout: Path
    ) -> None:
        """The exclude entry names two orchestrator-owned files, nothing wider.

        A prefix-shaped entry (``.codex/`` or ``.codex/rules/``) would hide any
        future file an operator or another tool put there, repository-wide.
        """
        _installer().establish(worktree, provider=CODEX)
        (product_checkout / ".codex" / "rules").mkdir(parents=True)
        (product_checkout / ".codex" / "rules" / "operator.rules").write_text(
            "mine\n", encoding="utf-8"
        )

        # ``--untracked-files=all`` is what the worktree owner's removal-safety
        # check uses, so it is the enumeration that has to keep seeing the file.
        assert "?? .codex/rules/operator.rules" in _git_stdout(
            product_checkout, "status", "--porcelain", "--untracked-files=all"
        )

    def test_rendering_is_deterministic(self) -> None:
        assert render_planning_rules() == render_planning_rules()


class TestOnlyCodexIsRegisteredFor:
    def test_a_provider_with_no_mechanism_gets_no_file_and_says_so(
        self, worktree: Path
    ) -> None:
        guard = _installer().establish(worktree, provider=CLAUDE)

        assert guard.enforced is False
        assert guard.policy_file is None
        assert not (worktree / ".codex").exists()


class TestGuardedIsEstablishedNotAssumed:
    def test_the_pinned_gate_commands_are_verified_refused(
        self, worktree: Path
    ) -> None:
        policy = StarlarkPrefixExecPolicy()
        guard = _installer(policy).establish(worktree, provider=CODEX)

        assert guard.enforced is True
        assert "make validate-pr-raw" in guard.refusals()
        assert "pytest -q tests/unit" in guard.refusals()
        assert "python -m pytest" in guard.refusals()
        assert ("make", "validate-pr-raw") in policy.asked

    def test_reading_the_code_is_verified_still_possible(
        self, worktree: Path
    ) -> None:
        guard = _installer().establish(worktree, provider=CODEX)

        assert "git log --oneline -20" in guard.allowances()
        assert "cat AGENTS.md" in guard.allowances()

    def test_the_runs_own_way_out_is_verified_still_possible(
        self, worktree: Path
    ) -> None:
        """``coding-done`` stays allowed, and that composition still matters.

        A planning run records its work through ``coding-done``, which no
        longer runs the code-candidate quick gate for it
        (``control/completion_gate_routing.py``). A guard that refused the
        completion command would turn the barrier into a run with no exit.
        """
        guard = _installer().establish(worktree, provider=CODEX)

        assert any(
            command.startswith("coding-done completed")
            for command in guard.allowances()
        )

    def test_a_policy_that_refuses_nothing_does_not_establish(
        self, worktree: Path
    ) -> None:
        with pytest.raises(PlanningCommandGuardError, match="does not refuse"):
            _installer(AlwaysAllowingExecPolicy()).establish(
                worktree, provider=CODEX
            )

    def test_an_unclassifiable_answer_does_not_establish(
        self, worktree: Path
    ) -> None:
        with pytest.raises(PlanningCommandGuardError, match="no classifiable answer"):
            _installer(UnanswerableExecPolicy()).establish(worktree, provider=CODEX)

    def test_an_unwritable_worktree_does_not_establish(self, tmp_path: Path) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("", encoding="utf-8")

        with pytest.raises(PlanningCommandGuardError, match="Failed to write"):
            _installer().establish(blocked, provider=CODEX)

    def test_a_provider_with_no_mechanism_is_not_an_error(
        self, worktree: Path
    ) -> None:
        # The distinction the port draws: "no mechanism for this provider" is
        # a limitation the caller decides about, not a failed establishment.
        assert _installer().establish(worktree, provider=CLAUDE).enforced is False


class TestThePolicyIsTheSharedVocabulary:
    def test_the_written_policy_carries_the_shared_gate_entry_points(
        self, worktree: Path
    ) -> None:
        guard = _installer().establish(worktree, provider=CODEX)
        content = guard.policy_file.read_text(encoding="utf-8")

        assert 'pattern = ["make"]' in content
        assert 'pattern = ["pytest"]' in content
        assert 'decision = "forbidden"' in content

    def test_the_refusal_explains_the_planning_contract_not_a_missing_venv(
        self, worktree: Path
    ) -> None:
        guard = _installer().establish(worktree, provider=CODEX)
        content = guard.policy_file.read_text(encoding="utf-8")

        assert "prepares a bounded issue" in content
        assert "create_issue" in content
        # The reviewer's reason is false here: a planning worktree IS
        # provisioned. Reusing that prose would tell the principal something
        # untrue about its own environment.
        assert "no virtualenv" not in content
