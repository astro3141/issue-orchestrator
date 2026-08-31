"""Reviewer worktree manager: create, fast-forward, remove."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import (
    CODEX_SAFETY_RULES,
    GUARDABLE_PROVIDERS,
    REVIEW_COMMAND_GUARD_PATHS,
    REVIEW_COMMAND_GUARD_SETTINGS,
    REVIEW_GUARD_RULES,
    WorktreeError,
    install_review_command_guard,
    read_reviewer_head_ownership,
    review_command_guard_command,
)
from issue_orchestrator.domain.artifact_contracts import AgentProvider
from issue_orchestrator.infra.hooks.review_command_guard import (
    orchestrator_source_root,
)
from issue_orchestrator.domain.review_exchange import (
    REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER,
)
from issue_orchestrator.execution.reviewer_worktree import (
    ReviewerCandidatePresentation,
    ReviewerWorktreeError,
    create_reviewer_worktree,
    fast_forward_reviewer_worktree,
    remove_reviewer_worktree,
)
from issue_orchestrator.ports.worktree_manager import REVIEWER_OWNED_HEAD_MARKER

from tests.codex_execpolicy_fakes import StarlarkPrefixExecPolicy


#: The provider the reviewer runs under in these tests. Required by
#: ``create_reviewer_worktree``: the command guard is installed through one
#: provider's hook mechanism, so creation has to know which one will run.
CLAUDE_CODE = AgentProvider("claude-code")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _bootstrap_repo_with_branch(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a tiny git repo with a feature branch checked out in a coder worktree."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    (repo_root / "README").write_text("hello\n")
    _git(repo_root, "add", "README")
    _git(repo_root, "commit", "-q", "-m", "initial")

    coder_worktree = tmp_path / "coder-wt"
    branch = "feature/widget"
    _git(repo_root, "worktree", "add", "-b", branch, str(coder_worktree))
    (coder_worktree / "work.py").write_text("print('first')\n")
    _git(coder_worktree, "add", "work.py")
    _git(coder_worktree, "commit", "-q", "-m", "first commit")
    return repo_root, coder_worktree, branch


class TestReviewerWorktreeLifecycle:
    def test_create_attaches_sibling_at_coder_branch_tip(self, tmp_path: Path) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="20260502T000000Z",
            reviewer_provider=CLAUDE_CODE,
        )

        assert reviewer.path == coder.parent / f"{coder.name}-review-20260502T000000Z"
        assert reviewer.path.exists()
        assert reviewer.path.is_dir()
        marker = reviewer.path / ".issue-orchestrator" / "worktree-id"
        assert marker.read_text().startswith("wt-")
        # Detached HEAD: HEAD points at the same SHA as the coder branch tip.
        coder_tip = _git(repo_root, "rev-parse", branch)
        reviewer_head = _git(reviewer.path, "rev-parse", "HEAD")
        assert reviewer_head == coder_tip
        assert (reviewer.path / REVIEWER_OWNED_HEAD_MARKER).read_text().strip() == (
            coder_tip
        )
        # And HEAD is detached, not on the coder's branch.
        symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=reviewer.path, capture_output=True, text=True,
        )
        assert symbolic.returncode != 0, "reviewer worktree must be detached"

    def test_create_refuses_to_clobber_existing_path(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        sibling = coder.parent / f"{coder.name}-review-T"
        sibling.mkdir()

        with pytest.raises(ReviewerWorktreeError, match="already exists"):
            create_reviewer_worktree(
                coder_worktree=coder,
                coder_branch=branch,
                timestamp="T",
                reviewer_provider=CLAUDE_CODE,
            )

    def test_create_rolls_back_when_identity_installation_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        sibling = coder.parent / f"{coder.name}-review-T"

        def fail_identity_install(_path: Path) -> str:
            raise WorktreeError("identity unavailable")

        monkeypatch.setattr(
            "issue_orchestrator.execution.reviewer_worktree.install_worktree_identity",
            fail_identity_install,
        )

        with pytest.raises(ReviewerWorktreeError, match="Failed to install"):
            create_reviewer_worktree(
                coder_worktree=coder,
                coder_branch=branch,
                timestamp="T",
                reviewer_provider=CLAUDE_CODE,
            )

        assert not sibling.exists()
        assert str(sibling) not in _git(repo_root, "worktree", "list", "--porcelain")

    def test_fast_forward_picks_up_new_coder_commits(self, tmp_path: Path) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        original_tip = _git(repo_root, "rev-parse", branch)

        # Coder commits more work after the reviewer was created.
        (coder / "work.py").write_text("print('second')\n")
        _git(coder, "add", "work.py")
        _git(coder, "commit", "-q", "-m", "second commit")
        new_tip = _git(repo_root, "rev-parse", branch)
        assert new_tip != original_tip

        fast_forward_reviewer_worktree(reviewer)

        reviewer_head = _git(reviewer.path, "rev-parse", "HEAD")
        assert reviewer_head == new_tip
        assert (reviewer.path / REVIEWER_OWNED_HEAD_MARKER).read_text().strip() == (
            new_tip
        )

    def test_malformed_owned_head_marker_is_unknown_not_legacy(
        self, tmp_path: Path
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        (reviewer.path / REVIEWER_OWNED_HEAD_MARKER).write_text(
            "partial-write",
            encoding="utf-8",
        )

        evidence = read_reviewer_head_ownership(reviewer.path)

        assert evidence.marker_present is True
        assert evidence.expected_head is None

    def test_remove_deletes_the_worktree(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        assert reviewer.path.exists()

        remove_reviewer_worktree(reviewer)

        assert not reviewer.path.exists()

    def test_remove_is_noop_when_path_already_gone(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        # External cleanup beats us to it.
        import shutil
        shutil.rmtree(reviewer.path)

        # Must not raise — orchestrator shutdown paths rely on idempotence.
        remove_reviewer_worktree(reviewer)


class TestReviewerWorktreeRefusesGateCommands:
    """The unprovisioned worktree carries a barrier, not just an instruction.

    A gate command run in this worktree fails on the missing prerequisite and
    the failure is attributed to the candidate (#48). `docs/architecture/
    hooks.md` rules that a prompt cannot be what prevents that, so creation
    installs a `PreToolUse` policy that refuses the command before it runs.
    """

    def test_creation_registers_a_bash_pre_tool_use_guard(
        self, tmp_path: Path
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        settings = json.loads(
            (reviewer.path / REVIEW_COMMAND_GUARD_SETTINGS).read_text()
        )
        matchers = settings["hooks"]["PreToolUse"]
        assert [m["matcher"] for m in matchers] == ["Bash"]
        assert _guard_command(reviewer.path) == review_command_guard_command()

    def test_the_guard_runs_the_orchestrators_own_policy_not_the_candidates(
        self, tmp_path: Path
    ) -> None:
        """A worktree that supplied its own policy would be judging itself."""
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        command = _guard_command(reviewer.path)
        assert str(orchestrator_source_root()) in command
        assert sys.executable in command
        assert str(reviewer.path) not in command

    def test_the_installed_hook_actually_refuses_a_gate_command(
        self, tmp_path: Path
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        refused = _run_guard(reviewer.path, "make validate-pr-raw")

        assert refused.returncode == 2
        assert "BLOCKED" in refused.stderr

    def test_the_installed_hook_still_lets_the_reviewer_read_the_code(
        self, tmp_path: Path
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        assert _run_guard(reviewer.path, "git log --oneline").returncode == 0

    def test_the_guard_leaves_the_candidate_untouched(self, tmp_path: Path) -> None:
        """It lands in the never-tracked local layer and is hidden from status."""
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        tracked = subprocess.run(
            ["git", "ls-files", str(REVIEW_COMMAND_GUARD_SETTINGS)],
            cwd=reviewer.path,
            capture_output=True,
            text=True,
        )
        assert tracked.stdout.strip() == ""
        assert str(REVIEW_COMMAND_GUARD_SETTINGS) not in _git(
            reviewer.path, "status", "--short"
        )

    def test_removal_still_succeeds_with_the_guard_installed(
        self, tmp_path: Path
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        remove_reviewer_worktree(reviewer)

        assert not reviewer.path.exists()

    def test_create_rolls_back_when_the_guard_cannot_be_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unguarded reviewer worktree must not exist at all."""
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        sibling = coder.parent / f"{coder.name}-review-T"

        def fail_guard_install(_path: Path, *, provider: AgentProvider) -> None:
            raise WorktreeError(f"settings unwritable for {provider.value}")

        monkeypatch.setattr(
            "issue_orchestrator.execution.reviewer_worktree.install_review_command_guard",
            fail_guard_install,
        )

        with pytest.raises(ReviewerWorktreeError, match="command guard"):
            create_reviewer_worktree(
                coder_worktree=coder,
                coder_branch=branch,
                timestamp="T",
                reviewer_provider=CLAUDE_CODE,
            )

        assert not sibling.exists()
        assert str(sibling) not in _git(repo_root, "worktree", "list", "--porcelain")

    @pytest.mark.parametrize("provider", ["cursor", "gemini", "aider"])
    def test_no_guard_file_is_planted_for_a_provider_that_cannot_read_it(
        self, tmp_path: Path, provider: str,
    ) -> None:
        """A guard file the reviewer never reads is worse than none.

        Each provider's guard is registered through that provider's own
        mechanism — Claude Code's ``.claude/settings.local.json``, Codex's
        ``.codex/rules``. A reviewer launched on a provider that reads neither
        would be handed a worktree that *looks* guarded — a file is present —
        while nothing refuses anything, which is how a written claim comes to
        stand in for enforcement (``docs/architecture/hooks.md``). So for such
        a provider the installer writes nothing at all.
        """
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        assert provider not in GUARDABLE_PROVIDERS

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=AgentProvider(provider),
        )

        for planted in REVIEW_COMMAND_GUARD_PATHS:
            assert not (reviewer.path / planted).exists()

    def test_an_unguardable_provider_is_reported_as_unguarded_not_installed(
        self, tmp_path: Path,
    ) -> None:
        """The caller is told, in words, which worktree has no barrier."""
        _, coder, _branch = _bootstrap_repo_with_branch(tmp_path)

        outcome = install_review_command_guard(
            coder, provider=AgentProvider("cursor"),
        )

        assert outcome.guarded is False
        assert outcome.policy_file is None
        assert outcome.provider == AgentProvider("cursor")

    def test_a_guardable_provider_reports_the_file_it_installed(
        self, tmp_path: Path,
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        outcome = install_review_command_guard(reviewer.path, provider=CLAUDE_CODE)

        assert outcome.guarded is True
        assert outcome.policy_file == reviewer.path / REVIEW_COMMAND_GUARD_SETTINGS

    def test_the_unguarded_case_is_logged_loudly_enough_to_notice(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An operator reading logs must be able to see the barrier is absent."""
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        with caplog.at_level(logging.WARNING):
            create_reviewer_worktree(
                coder_worktree=coder,
                coder_branch=branch,
                timestamp="T",
                reviewer_provider=AgentProvider("cursor"),
            )

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("UNGUARDED" in r.getMessage() for r in warnings)
        assert any("cursor" in r.getMessage() for r in warnings)


class TestTheCodexReviewerWorktreeLifecycle:
    """A Codex reviewer is guarded too, and its worktree still comes apart.

    #396 gave the Codex reviewer a real worktree-local exec policy instead of
    the prompt-only arrangement it had. That plants two more untracked files in
    a worktree whose removal refuses to run while untracked files are present,
    so the lifecycle owner has to lift exactly what the guard plants — a list
    it asks the guard for rather than keeping its own copy of.
    """

    def test_removal_lifts_every_path_a_guard_registration_can_plant(
        self, tmp_path: Path
    ) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        # The Codex registration's files, established exactly as a Codex
        # reviewer's worktree would carry them.
        outcome = install_review_command_guard(
            reviewer.path,
            provider=AgentProvider("codex"),
            execpolicy=StarlarkPrefixExecPolicy(),
        )
        assert outcome.guarded is True
        assert outcome.policy_file.exists()

        remove_reviewer_worktree(reviewer)

        assert not reviewer.path.exists()

    def test_creation_fails_closed_when_the_codex_guard_cannot_be_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Codex reviewer worktree that cannot be guarded must not exist.

        The whole production path runs here — creation, the real installer, the
        real ``codex execpolicy check`` adapter — with the CLI it consults
        replaced on ``PATH`` by one that cannot answer. That is the direction
        #396 F5 pins: an unverifiable guard is a failed launch, not a quiet
        ``guarded=False``, and the worktree it was for is rolled back with no
        owned orphan left behind.
        """
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        sibling = coder.parent / f"{coder.name}-review-T"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        unanswerable_codex = fake_bin / "codex"
        unanswerable_codex.write_text(
            "#!/bin/sh\necho 'not json' >&2\nexit 1\n", encoding="utf-8"
        )
        unanswerable_codex.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

        with pytest.raises(ReviewerWorktreeError, match="command guard"):
            create_reviewer_worktree(
                coder_worktree=coder,
                coder_branch=branch,
                timestamp="T",
                reviewer_provider=AgentProvider("codex"),
            )

        assert not sibling.exists()
        assert str(sibling) not in _git(repo_root, "worktree", "list", "--porcelain")

    def test_the_lifted_paths_cover_what_the_codex_registration_writes(
        self, tmp_path: Path
    ) -> None:
        """The removal list is the guard's answer, so it cannot fall behind."""
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        install_review_command_guard(
            reviewer.path,
            provider=AgentProvider("codex"),
            execpolicy=StarlarkPrefixExecPolicy(),
        )

        written = {
            path.relative_to(reviewer.path)
            for path in reviewer.path.rglob("*")
            if path.is_file() and ".git" not in path.parts and path.name != ".git"
        }
        guard_files = {
            path for path in written if path.parts[0] in {".claude", ".codex"}
        }

        assert guard_files <= set(REVIEW_COMMAND_GUARD_PATHS)
        assert guard_files == {
            REVIEW_COMMAND_GUARD_SETTINGS,
            REVIEW_GUARD_RULES,
            CODEX_SAFETY_RULES,
        }


def _guard_command(reviewer_path: Path) -> str:
    settings = json.loads((reviewer_path / REVIEW_COMMAND_GUARD_SETTINGS).read_text())
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _run_guard(reviewer_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    """Run the registered hook exactly as the agent CLI would."""
    return subprocess.run(
        _guard_command(reviewer_path),
        shell=True,
        cwd=reviewer_path,
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
    )


class TestReviewerCandidatePresentation:
    """What the reviewer is given, and what the orchestrator says it gave.

    The reported SHA becomes ``review-verdict.json``'s ``reviewed_sha``
    (``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4), so it has to be the
    commit the reviewer's worktree actually holds — not wherever the coder's
    branch has got to by the time anyone asks.
    """

    def test_presents_the_branch_tip_and_reports_it(self, tmp_path: Path) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        (coder / "work.py").write_text("print('second')\n")
        _git(coder, "add", "work.py")
        _git(coder, "commit", "-q", "-m", "second commit")

        presented = ReviewerCandidatePresentation(
            reviewer_worktree_path=reviewer.path,
            coder_branch=branch,
        ).present(round_index=1)

        assert presented == _git(repo_root, "rev-parse", branch)
        assert presented == _git(reviewer.path, "rev-parse", "HEAD")

    def test_reports_what_it_checked_out_when_the_branch_then_moves(
        self, tmp_path: Path
    ) -> None:
        """A commit landing after the checkout does not change the answer.

        This is the race the binding exists to survive: the coder's branch is
        advanced by the per-round hook, i.e. immediately after the reviewer was
        given the previous tip. The reviewer's filesystem still holds that tip,
        so that is what must be reported.
        """
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        sha_a = _git(reviewer.path, "rev-parse", "HEAD")

        def _advance_branch(_round_index: int) -> None:
            (coder / "work.py").write_text("print('later')\n")
            _git(coder, "add", "work.py")
            _git(coder, "commit", "-q", "-m", "landed mid-review")

        presented = ReviewerCandidatePresentation(
            reviewer_worktree_path=reviewer.path,
            coder_branch=branch,
            before_round=_advance_branch,
        ).present(round_index=1)

        assert presented == sha_a
        assert _git(coder, "rev-parse", "HEAD") != sha_a
        assert _git(reviewer.path, "rev-parse", "HEAD") == sha_a

    def test_without_a_branch_reports_the_reviewer_worktrees_own_head(
        self, tmp_path: Path
    ) -> None:
        """Nothing re-points the worktree, so it still holds what it was made at."""
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        created_at = _git(reviewer.path, "rev-parse", "HEAD")
        # The coder moves on; with no branch to track, that must not leak in.
        (coder / "work.py").write_text("print('later')\n")
        _git(coder, "add", "work.py")
        _git(coder, "commit", "-q", "-m", "later")

        presented = ReviewerCandidatePresentation(
            reviewer_worktree_path=reviewer.path,
            coder_branch=None,
        ).present(round_index=1)

        assert presented == created_at

    def test_unestablishable_commit_is_reported_as_unknown(
        self, tmp_path: Path
    ) -> None:
        """Not a git worktree at all: None, never a stand-in commit."""
        bare = tmp_path / "not-a-worktree"
        bare.mkdir()

        presented = ReviewerCandidatePresentation(
            reviewer_worktree_path=bare,
            coder_branch=None,
        ).present(round_index=1)

        assert presented is None

    def test_caller_hook_runs_for_every_round(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )
        seen: list[int] = []

        presentation = ReviewerCandidatePresentation(
            reviewer_worktree_path=reviewer.path,
            coder_branch=branch,
            before_round=seen.append,
        )
        presentation.present(round_index=1)
        presentation.present(round_index=2)

        assert seen == [1, 2]


class TestReviewerWorktreeDiagnostics:
    """Checkout failures must surface Git command/cwd/returncode/stdout/stderr.

    The #6594 incident raised a bare ``CalledProcessError`` whose message hid
    *why* the reviewer-worktree checkout failed (dirty runtime files vs missing
    commit vs lock contention). The enriched error must carry that context.
    """

    def test_fast_forward_checkout_failure_preserves_git_context(
        self, tmp_path: Path
    ) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="T",
            reviewer_provider=CLAUDE_CODE,
        )

        # Coder advances the branch tip by committing a new tracked file.
        artifact = ".issue-orchestrator/review-response.json"
        (coder / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)
        (coder / artifact).write_text('{"committed": true}\n')
        _git(coder, "add", artifact)
        _git(coder, "commit", "-q", "-m", "commit runtime artifact")
        new_tip = _git(repo_root, "rev-parse", branch)

        # The reviewer worktree has an UNTRACKED file at the same path; git
        # refuses to overwrite it on checkout, exactly like a committed
        # runtime artifact colliding with a live runtime write.
        (reviewer.path / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)
        (reviewer.path / artifact).write_text('{"local": "dirty runtime write"}\n')

        with pytest.raises(ReviewerWorktreeError) as excinfo:
            fast_forward_reviewer_worktree(reviewer)

        err = excinfo.value
        # The checkout-failure marker lets completion failure-reporting attach
        # runtime-artifact recovery guidance to exactly this class (#6659).
        assert REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER in str(err)
        # Rich git failure context.
        assert err.git_failure is not None
        assert err.git_failure.returncode != 0
        assert err.git_failure.args[:3] == ("git", "checkout", "--detach")
        assert err.git_failure.args[-1] == new_tip
        assert err.git_failure.cwd == str(reviewer.path)
        # Git explains the path-level reason on stderr.
        assert "would be overwritten" in err.git_failure.stderr
        # Review-exchange specifics.
        assert err.context["reviewer_worktree"] == str(reviewer.path)
        assert err.context["coder_branch"] == branch
        assert err.context["target_sha"] == new_tip
        # The structured diagnostic bundles both for the failure record/log.
        diagnostic = err.diagnostic()
        assert "git" in diagnostic
        assert diagnostic["coder_branch"] == branch
        assert diagnostic["target_sha"] == new_tip

    def test_missing_branch_tip_raises_rich_error(self, tmp_path: Path) -> None:
        _, coder, _ = _bootstrap_repo_with_branch(tmp_path)

        with pytest.raises(ReviewerWorktreeError) as excinfo:
            create_reviewer_worktree(
                coder_worktree=coder,
                coder_branch="does/not/exist",
                timestamp="T",
                reviewer_provider=CLAUDE_CODE,
            )

        err = excinfo.value
        assert err.git_failure is not None
        assert err.git_failure.returncode != 0
        assert err.git_failure.args[:2] == ("git", "rev-parse")
        # A missing branch tip is not a checkout collision, so it must NOT be
        # tagged with the runtime-artifact recovery marker.
        assert REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER not in str(err)
