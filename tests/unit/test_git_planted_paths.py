"""The one question every dirty surface asks about planted CLI tools.

Answered against real repositories: the whole point of the seam is what git's
*index* says about ``src/issue_orchestrator/entrypoints/cli_tools``, and a
stubbed git can only prove that some arguments were passed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.git.git_cli import GitCLI
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_planted_paths import repo_owns_planted_cli_tools
from issue_orchestrator.infra.runtime_artifacts import ORCHESTRATOR_CLI_TOOLS_DIR
from issue_orchestrator.ports.git import GitError

CLI_TOOLS_DIR = ORCHESTRATOR_CLI_TOOLS_DIR.as_posix()


def _git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "seed").write_text("seed\n")
    _git("add", "seed", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    return repo


def _git_port() -> GitCLI:
    return GitCLI(runner=LocalCommandRunner())


def test_foreign_repository_does_not_own_the_planted_path(tmp_path) -> None:
    assert repo_owns_planted_cli_tools(_git_port(), _repo(tmp_path)) is False


def test_an_untracked_file_there_does_not_transfer_ownership(tmp_path) -> None:
    """Ownership is what the repository *tracks*, not what happens to be on disk.

    This is the foreign-repo case after planting: the files exist, and they are
    still the orchestrator's.
    """
    repo = _repo(tmp_path)
    planted = repo / CLI_TOOLS_DIR / "coding_done.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("# planted\n")

    assert repo_owns_planted_cli_tools(_git_port(), repo) is False


def test_repository_that_tracks_the_path_owns_it(tmp_path) -> None:
    repo = _repo(tmp_path)
    tool = repo / CLI_TOOLS_DIR / "coding_done.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# product source\n")
    _git("add", f"{CLI_TOOLS_DIR}/coding_done.py", cwd=repo)
    _git("commit", "-m", "cli tools", cwd=repo)

    assert repo_owns_planted_cli_tools(_git_port(), repo) is True


def test_a_staged_but_uncommitted_cli_tool_already_counts_as_owned(tmp_path) -> None:
    """The index is the authority, so ``git add`` alone transfers ownership."""
    repo = _repo(tmp_path)
    tool = repo / CLI_TOOLS_DIR / "new_tool.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# candidate's new tool\n")
    _git("add", f"{CLI_TOOLS_DIR}/new_tool.py", cwd=repo)

    assert repo_owns_planted_cli_tools(_git_port(), repo) is True


def test_no_repository_raises_rather_than_answering(tmp_path) -> None:
    """Neither answer is safe to assume, so callers must be told git failed."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    with pytest.raises(GitError):
        repo_owns_planted_cli_tools(_git_port(), plain_dir)
