"""One worktree's provisioning run must not reach another checkout (#53).

This is the failure-direction proof for the active blocker. Every collaborator
here is the real one — the worktree runtime setup owner, `WorktreeProvisioner`,
`LocalCommandRunner`, `GitWorkingCopy`, and real Git — because the defect lived
in how they compose, not in any one of them:

1. worktree setup planted a `.venv` **symlink to the repository's**, and
2. provisioning runs the repository's own setup recipe *inside the worktree*.

Any recipe that populates `.venv` therefore wrote through that link into the
environment the primary checkout and every other worktree use. `uv sync` does
exactly this, rewriting the editable install's source path to the syncing
worktree on every run — whether or not the lockfile changed, and at any
concurrency. Removing the worktree afterwards left the shared environment
pointing at a directory that no longer exists, so the *next* thing that needed
a Python environment was what discovered it.

`SETUP_RECIPE` below is that mechanic in one line: write `$PWD` into the
environment's record of where its editable source lives. Restore the symlink
step and these tests fail, which is the point of them.
"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import WorktreeRuntimeSetup
from issue_orchestrator.control.worktree_provisioning import WorktreeProvisioner
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.infra.config import Config
from tests.unit.worktree_git_helpers import GitWorktree, make_git_worktree

# The file an environment uses to record which source tree its editable install
# resolves imports from — `_editable_impl_<project>.pth` under `site-packages`
# for a `uv`-managed environment.
EDITABLE_SOURCE = Path(".venv") / "editable-source"

# What a real recipe does to that record: create the environment if it is not
# there, then stamp the syncing project's own path into it.
SETUP_RECIPE = f'mkdir -p .venv && printf "%s" "$PWD" > {EDITABLE_SOURCE}'


def _provisioner(config_path: Path) -> WorktreeProvisioner:
    """A provisioner running the recipe for real, from operator configuration."""
    config = Config()
    config.setup_worktree = [SETUP_RECIPE]
    config.config_path = config_path
    return WorktreeProvisioner(
        config=config,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
    )


def _editable_source(checkout: Path) -> str:
    return (checkout / EDITABLE_SOURCE).read_text()


def _install_repository_environment(repo_root: Path) -> None:
    """Give the primary checkout a working environment of its own."""
    (repo_root / EDITABLE_SOURCE).parent.mkdir(parents=True)
    (repo_root / EDITABLE_SOURCE).write_text(str(repo_root))


def _add_worktree(repo: GitWorktree, name: str, branch: str) -> Path:
    worktree_path = repo.main_repo.parent / name
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch],
        cwd=repo.main_repo,
        check=True,
        capture_output=True,
    )
    WorktreeRuntimeSetup(enforce_hooks=False).apply(worktree_path)
    return worktree_path


def _remove_worktree(repo: GitWorktree, worktree_path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo.main_repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> GitWorktree:
    """A repository with its own environment, plus one provisioned worktree."""
    worktree = make_git_worktree(tmp_path, name="repo-53")
    _install_repository_environment(worktree.main_repo)
    WorktreeRuntimeSetup(enforce_hooks=False).apply(worktree.worktree_path)
    return worktree


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Operator configuration, outside every worktree it provisions."""
    path = tmp_path / "config" / "selfhost.yaml"
    path.parent.mkdir()
    return path


def test_provisioning_a_worktree_leaves_the_primary_checkout_working(
    repo, config_path
):
    """The incident, end to end: provision, remove the worktree, still import.

    This is the assertion the 2026-08-15 incident would have failed. Before the
    fix the recipe's write followed the `.venv` symlink into the primary
    checkout's environment and repointed it at the worktree; removing the
    worktree then left that path gone, and the primary checkout could no longer
    import the package or run its pre-push gate.
    """
    _provisioner(config_path).provision(repo.worktree_path)

    _remove_worktree(repo, repo.worktree_path)

    assert _editable_source(repo.main_repo) == str(repo.main_repo)
    assert Path(_editable_source(repo.main_repo)).is_dir()


def test_provisioning_builds_the_worktree_its_own_environment(repo, config_path):
    """The worktree is runnable, and runnable from *itself*.

    Isolation would be no improvement if it left worktrees without an
    environment: the gate would then fail on the missing prerequisite and blame
    the candidate, which is #48's failure mode.
    """
    _provisioner(config_path).provision(repo.worktree_path)

    assert _editable_source(repo.worktree_path) == str(repo.worktree_path)
    assert not (repo.worktree_path / ".venv").is_symlink()
    assert _editable_source(repo.main_repo) == str(repo.main_repo)


def test_a_worktree_created_before_the_fix_is_repaired_before_provisioning(
    repo, config_path
):
    """Worktrees are long-lived; the ones already carrying the link get reused.

    Declining to plant a new link is not enough on its own — a worktree set up
    by an older orchestrator still has one, and the next session in it would
    write through it exactly as before.
    """
    (repo.worktree_path / ".venv").symlink_to(
        repo.main_repo / ".venv", target_is_directory=True
    )

    WorktreeRuntimeSetup(enforce_hooks=False).apply(repo.worktree_path)
    _provisioner(config_path).provision(repo.worktree_path)

    assert _editable_source(repo.main_repo) == str(repo.main_repo)
    assert _editable_source(repo.worktree_path) == str(repo.worktree_path)


def test_concurrent_provisioning_cannot_corrupt_another_environment(
    repo, config_path
):
    """Two provisioning runs in flight at once, and no shared thing to race for.

    There is no coordination primitive here to trust, which is the point:
    isolation removes the shared environment rather than serialising access to
    it. The barrier only guarantees both runs are genuinely in flight — no
    sleeps, no timing assumptions.
    """
    second = _add_worktree(repo, "repo-53-b", "feature-b")
    provisioner = _provisioner(config_path)
    both_ready = threading.Barrier(2)

    def provision(worktree_path: Path) -> None:
        both_ready.wait(timeout=30)
        provisioner.provision(worktree_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [
            pool.submit(provision, repo.worktree_path),
            pool.submit(provision, second),
        ]:
            future.result(timeout=30)

    assert _editable_source(repo.worktree_path) == str(repo.worktree_path)
    assert _editable_source(second) == str(second)
    assert _editable_source(repo.main_repo) == str(repo.main_repo)
