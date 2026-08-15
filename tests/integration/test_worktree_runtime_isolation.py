"""One worktree's provisioning run must not reach another checkout (#53, #61).

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

The second half is #61, and it needs no symlink at all. A `.venv` that is a real
directory was trusted for being a directory, so a worktree carrying what an
earlier failed run left behind kept it, and `uv sync` was handed an environment
holding an install record that named **another checkout**. uv reported
`Requirement installed, but mismatched` and reconciled it by reinstalling
editable where the record pointed — moving that checkout's `.pth`.

`SETUP_RECIPE` below is both mechanics in one recipe: record `$PWD` as the
source this environment resolves imports from, and — when the environment
already records a *different* checkout — reconcile the mismatch where uv
reconciles it, in the environment the record names. Restore either the symlink
step or the trust in a bare directory and these tests fail, which is the point
of them.
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

VENV = Path(".venv")
SITE_PACKAGES = VENV / "lib" / "python3.14" / "site-packages"

# The file an environment uses to record which source tree its editable install
# resolves imports from — `_editable_impl_<project>.pth` under `site-packages`
# for a `uv`-managed environment. Its real path matters here: it is the same
# file the recipe writes and worktree setup reads provenance from, so the
# mechanic and the fix meet on one artifact rather than two models of one.
EDITABLE_SOURCE = SITE_PACKAGES / "_editable_impl_project.pth"

# What a real recipe does to that record. Two branches, because the installer
# has two: an environment recording someone else is a *mismatch*, and uv
# reconciles a mismatch by reinstalling editable into the environment the record
# names — not into the one it was pointed at.
SETUP_RECIPE = f"""
set -e
if [ -f "{EDITABLE_SOURCE}" ]; then
  recorded=$(cat "{EDITABLE_SOURCE}")
  if [ "$recorded" != "$PWD" ]; then
    printf "%s" "$PWD" > "$recorded/{EDITABLE_SOURCE}"
    exit 0
  fi
fi
mkdir -p "{SITE_PACKAGES}"
printf "%s" "$PWD" > "{EDITABLE_SOURCE}"
"""


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


def _install_environment(checkout: Path, *, editable_source: Path) -> None:
    """Give ``checkout`` an environment that records ``editable_source``.

    Everything that makes the directory an environment rather than a directory
    named ``.venv`` is here — the marker file and an interpreter — because that
    is exactly what the stale-environment case has to be able to fake. A
    reproduction that skipped them would be caught by the health check and would
    never reach the provenance rule it is meant to prove.
    """
    (checkout / SITE_PACKAGES).mkdir(parents=True, exist_ok=True)
    (checkout / VENV / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (checkout / VENV / "bin").mkdir(parents=True, exist_ok=True)
    (checkout / VENV / "bin" / "python").write_text("#!/bin/sh\n")
    (checkout / EDITABLE_SOURCE).write_text(str(editable_source))


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
    _install_environment(worktree.main_repo, editable_source=worktree.main_repo)
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


def test_a_reused_worktree_carrying_a_stale_environment_reaches_no_other_checkout(
    repo, config_path
):
    """The #61 shape, with no symlink anywhere in it.

    The worktree holds a ``.venv`` that is a **real directory**: it satisfies
    ``[ -d .venv ]``, it is structurally an environment — marker file,
    interpreter, populated ``site-packages`` — and the one thing wrong with it
    is the install record, which names the primary checkout. That is what a
    previous run leaves behind, and it is what reuse used to hand straight to
    the recipe, because a directory was trusted for being a directory.

    Provisioning it must leave the primary checkout exactly as it was. Restore
    that trust and the recipe finds the stale record, reconciles the mismatch in
    the environment the record names, and this test fails.
    """
    _install_environment(repo.worktree_path, editable_source=repo.main_repo)
    assert (repo.worktree_path / VENV).is_dir()
    assert not (repo.worktree_path / VENV).is_symlink()

    WorktreeRuntimeSetup(enforce_hooks=False).apply(repo.worktree_path)
    _provisioner(config_path).provision(repo.worktree_path)

    assert _editable_source(repo.main_repo) == str(repo.main_repo)
    assert _editable_source(repo.worktree_path) == str(repo.worktree_path)


def test_a_reused_worktrees_own_environment_is_not_rebuilt(repo, config_path):
    """Reuse still costs what reuse costs — the fix may not buy isolation with a
    full install per session.

    An environment recording *this* worktree is this worktree's own, so setup
    leaves it alone and the recipe syncs it rather than recreating it. The
    installed marker is what proves nothing was thrown away.
    """
    _install_environment(repo.worktree_path, editable_source=repo.worktree_path)
    installed = repo.worktree_path / SITE_PACKAGES / "installed-package"
    installed.write_text("kept")

    WorktreeRuntimeSetup(enforce_hooks=False).apply(repo.worktree_path)
    _provisioner(config_path).provision(repo.worktree_path)

    assert installed.read_text() == "kept"
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
