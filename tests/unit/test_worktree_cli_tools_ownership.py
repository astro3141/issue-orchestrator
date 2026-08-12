"""The worktree filesystem must equal the commit it claims to be.

``sync_cli_tools`` plants the orchestrator's runtime CLI helpers into a
worktree. ``src/issue_orchestrator/entrypoints/cli_tools/`` means two different
things depending on the target repository:

* in a foreign repository it is orchestrator runtime and nothing else, so
  planting there invents files the repository has no opinion about;
* in Issue-Orchestrator's own repository it is product source the candidate
  branch may be changing, so a planted copy shadows the commit that validation
  and review are supposed to be reading.

These tests pin the discriminator (does the repository track that path?) and
the resulting invariant: after runtime setup, a self-hosting worktree's CLI
tools are byte-identical to its own ``HEAD``.

Real repositories throughout — ``--skip-worktree`` semantics, index restores,
and ``info/exclude`` lookup for linked worktrees cannot be proven against a
hand-built ``.git``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import (
    WorktreeError,
    WorktreeRuntimeSetup,
    can_remove_without_user_changes,
    sync_cli_tools,
)
from issue_orchestrator.entrypoints import cli_tools as orchestrator_cli_tools
from issue_orchestrator.infra.runtime_artifacts import ORCHESTRATOR_CLI_TOOLS_DIR
from tests.unit.worktree_git_helpers import GitWorktree, make_git_worktree

CLI_TOOLS_DIR = ORCHESTRATOR_CLI_TOOLS_DIR.as_posix()
CANDIDATE_TOOL = f"{CLI_TOOLS_DIR}/coding_done.py"
CANDIDATE_SOURCE = "# candidate branch version of coding_done\n"

# The copies the orchestrator plants from — the only content a repair may treat
# as provably orchestrator-owned.
PACKAGE_CLI_TOOLS = Path(str(orchestrator_cli_tools.__file__)).parent
# A tool the package ships that the fixture repository does not track, which is
# what "planted, and the index cannot restore it" looks like.
UNTRACKED_PACKAGE_TOOL = f"{CLI_TOOLS_DIR}/reviewer_done.py"


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _index_flags(worktree_path: Path) -> dict[str, str]:
    """Return ``{path: index flag letter}`` for the tracked CLI tools."""
    listed = _git("ls-files", "-v", "--", CLI_TOOLS_DIR, cwd=worktree_path)
    return {line[2:]: line[0] for line in listed.splitlines() if line}


def _self_hosting_worktree(tmp_path: Path) -> GitWorktree:
    """A repository that tracks cli_tools itself, plus a linked worktree.

    The CLI tool is committed *on the worktree's own branch*, which is what
    "the candidate owns this path" means in the situation being reproduced.
    """
    git_worktree = make_git_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    tool = worktree_path / CANDIDATE_TOOL
    tool.parent.mkdir(parents=True)
    tool.write_text(CANDIDATE_SOURCE)
    _git("add", CANDIDATE_TOOL, cwd=worktree_path)
    _git("commit", "-m", "candidate cli tools", cwd=worktree_path)
    return git_worktree


def _exclude_files(git_worktree: GitWorktree) -> tuple[Path, ...]:
    return (
        git_worktree.gitdir / "info" / "exclude",
        git_worktree.main_repo / ".git" / "info" / "exclude",
    )


def _exclude(git_worktree: GitWorktree, entry: str) -> None:
    for exclude in _exclude_files(git_worktree):
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{entry}\n")


def _plant_stale_overlay(git_worktree: GitWorktree) -> None:
    """Reproduce the pre-fix state: planted copy, hidden bit, exclude entry."""
    worktree_path = git_worktree.worktree_path
    (worktree_path / CANDIDATE_TOOL).write_text("# orchestrator runtime copy\n")
    _git("update-index", "--skip-worktree", "--", CANDIDATE_TOOL, cwd=worktree_path)
    _exclude(git_worktree, CANDIDATE_TOOL)


def _plant_untracked_leftover(git_worktree: GitWorktree, rel_path: str, body: bytes) -> None:
    """Plant a file the branch does not track, hidden only by the exclude entry.

    No ``--skip-worktree`` bit exists for an untracked path, so this is the
    overlay state the index cannot repair.
    """
    target = git_worktree.worktree_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    _exclude(git_worktree, rel_path)


# ---------------------------------------------------------------------------
# Foreign repository — the orchestrator owns the path
# ---------------------------------------------------------------------------


def test_foreign_repo_still_gets_planted_cli_tools(tmp_path) -> None:
    worktree_path = make_git_worktree(tmp_path).worktree_path

    synced = sync_cli_tools(worktree_path)

    assert Path(CANDIDATE_TOOL) in synced
    assert (worktree_path / CANDIDATE_TOOL).exists()


# ---------------------------------------------------------------------------
# Self-hosting repository — the candidate commit owns the path
# ---------------------------------------------------------------------------


def test_self_hosting_repo_is_not_overlaid(tmp_path) -> None:
    """The candidate's own CLI tools survive setup byte-for-byte."""
    worktree_path = _self_hosting_worktree(tmp_path).worktree_path

    synced = sync_cli_tools(worktree_path)

    assert synced == []
    assert (worktree_path / CANDIDATE_TOOL).read_text() == CANDIDATE_SOURCE


def test_self_hosting_repo_never_hides_cli_tools_from_git_status(tmp_path) -> None:
    """No ``--skip-worktree`` on repo-owned paths: it is what made this silent."""
    worktree_path = _self_hosting_worktree(tmp_path).worktree_path

    sync_cli_tools(worktree_path)

    assert set(_index_flags(worktree_path).values()) == {"H"}


def test_stale_overlay_from_an_earlier_run_is_undone(tmp_path) -> None:
    """Worktrees are reused, so declining to plant is not enough on its own."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _plant_stale_overlay(git_worktree)
    # Precondition: the divergence really is invisible to plain git.
    assert (worktree_path / CANDIDATE_TOOL).read_text() != CANDIDATE_SOURCE
    assert _git("status", "--porcelain", cwd=worktree_path).strip() == ""

    sync_cli_tools(worktree_path)

    assert (worktree_path / CANDIDATE_TOOL).read_text() == CANDIDATE_SOURCE
    assert set(_index_flags(worktree_path).values()) == {"H"}


def test_stale_exclude_entries_are_removed_from_every_exclude_file(tmp_path) -> None:
    """A leftover exclude entry would hide a CLI tool the candidate *adds*.

    The common git dir's copy matters most: it is shared, so a stale entry
    there leaks into every other worktree of the repository.
    """
    git_worktree = _self_hosting_worktree(tmp_path)
    _plant_stale_overlay(git_worktree)

    sync_cli_tools(git_worktree.worktree_path)

    for exclude in (
        git_worktree.gitdir / "info" / "exclude",
        git_worktree.main_repo / ".git" / "info" / "exclude",
    ):
        assert CLI_TOOLS_DIR not in exclude.read_text()


def test_agent_edits_to_cli_tools_are_left_alone(tmp_path) -> None:
    """Only content git was told to ignore is restored; real work is not."""
    worktree_path = _self_hosting_worktree(tmp_path).worktree_path
    (worktree_path / CANDIDATE_TOOL).write_text("# agent's in-progress edit\n")

    sync_cli_tools(worktree_path)

    assert (worktree_path / CANDIDATE_TOOL).read_text() == "# agent's in-progress edit\n"
    assert CANDIDATE_TOOL in _git("status", "--porcelain", cwd=worktree_path)


def test_a_path_git_has_to_quote_is_still_restored(tmp_path) -> None:
    """Parsing ``ls-files`` output without ``-z`` would hand git a quoted name.

    Git quotes any path holding a control character or a non-ASCII byte, and
    that quoted string fed back to ``update-index``/``checkout`` names a file
    that does not exist — the repair then fails on a path git reported
    perfectly well.
    """
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    quoted_tool = f"{CLI_TOOLS_DIR}/needs\tquoting.py"
    (worktree_path / quoted_tool).write_text(CANDIDATE_SOURCE)
    _git("add", "--", quoted_tool, cwd=worktree_path)
    _git("commit", "-m", "tool with a space", cwd=worktree_path)
    (worktree_path / quoted_tool).write_text("# orchestrator runtime copy\n")
    _git("update-index", "--skip-worktree", "--", quoted_tool, cwd=worktree_path)

    sync_cli_tools(worktree_path)

    assert (worktree_path / quoted_tool).read_text() == CANDIDATE_SOURCE
    assert set(_index_flags(worktree_path).values()) == {"H"}


# ---------------------------------------------------------------------------
# Leftovers the index cannot restore
#
# A planted file the branch does not track never carried a --skip-worktree bit;
# the exclude entry alone hid it. Dropping that entry makes it visible, so the
# repair has to finish the job or the candidate inherits a permanently dirty
# path nobody on the branch wrote.
# ---------------------------------------------------------------------------


def test_untracked_planted_leftover_is_discarded(tmp_path) -> None:
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    packaged = (PACKAGE_CLI_TOOLS / "reviewer_done.py").read_bytes()
    _plant_untracked_leftover(git_worktree, UNTRACKED_PACKAGE_TOOL, packaged)

    sync_cli_tools(worktree_path)

    assert not (worktree_path / UNTRACKED_PACKAGE_TOOL).exists()
    assert _git("status", "--porcelain", cwd=worktree_path).strip() == ""


def test_untracked_file_the_orchestrator_cannot_claim_is_kept_and_reported(
    tmp_path, caplog
) -> None:
    """Same name, different bytes: it is the candidate's until proven otherwise."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _plant_untracked_leftover(
        git_worktree, UNTRACKED_PACKAGE_TOOL, b"# the candidate's own new tool\n"
    )

    with caplog.at_level("WARNING"):
        sync_cli_tools(worktree_path)

    assert (
        worktree_path / UNTRACKED_PACKAGE_TOOL
    ).read_bytes() == b"# the candidate's own new tool\n"
    assert UNTRACKED_PACKAGE_TOOL in _git("status", "--porcelain", cwd=worktree_path)
    assert UNTRACKED_PACKAGE_TOOL in caplog.text


def test_a_new_tool_the_package_does_not_ship_is_never_touched(tmp_path) -> None:
    """The candidate is free to add a CLI tool the orchestrator has no copy of."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    new_tool = f"{CLI_TOOLS_DIR}/candidate_only_tool.py"
    _plant_untracked_leftover(git_worktree, new_tool, b"# brand new\n")

    sync_cli_tools(worktree_path)

    assert (worktree_path / new_tool).read_bytes() == b"# brand new\n"
    assert new_tool in _git("status", "--porcelain", cwd=worktree_path)


# ---------------------------------------------------------------------------
# The invariant, through the owner that composes the whole sequence
# ---------------------------------------------------------------------------


def test_runtime_setup_leaves_self_hosting_worktree_equal_to_head(tmp_path) -> None:
    """``validated candidate filesystem == candidate Git HEAD``, end to end."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _plant_stale_overlay(git_worktree)

    WorktreeRuntimeSetup(
        repo_root=git_worktree.main_repo, enforce_hooks=False
    ).apply(worktree_path)

    head_source = _git("show", f"HEAD:{CANDIDATE_TOOL}", cwd=worktree_path)
    assert (worktree_path / CANDIDATE_TOOL).read_text() == head_source
    assert set(_index_flags(worktree_path).values()) == {"H"}
    dirty = _git("status", "--porcelain", cwd=worktree_path)
    assert CLI_TOOLS_DIR not in dirty


# ---------------------------------------------------------------------------
# The same discriminator, consumed by forced worktree removal
#
# "Discardable orchestrator planting" and "uncommitted product source" are the
# same path in the two repositories, so cleanup has to ask the same question
# the planting step does — otherwise it deletes a CLI tool the agent wrote.
# ---------------------------------------------------------------------------


def test_forced_removal_keeps_an_uncommitted_cli_tool_the_candidate_added(
    tmp_path,
) -> None:
    worktree_path = _self_hosting_worktree(tmp_path).worktree_path
    (worktree_path / f"{CLI_TOOLS_DIR}/new_tool.py").write_text("# new tool\n")

    assert can_remove_without_user_changes(worktree_path) is False


def test_forced_removal_still_discards_planted_cli_tools_in_a_foreign_repo(
    tmp_path,
) -> None:
    worktree_path = make_git_worktree(tmp_path).worktree_path
    sync_cli_tools(worktree_path)

    assert can_remove_without_user_changes(worktree_path) is True


# ---------------------------------------------------------------------------
# Fail loudly rather than guess
# ---------------------------------------------------------------------------


def test_unreadable_repository_fails_instead_of_guessing(tmp_path) -> None:
    """Without an answer from git, either choice can break the invariant."""
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    with pytest.raises(WorktreeError, match="list tracked CLI tool paths"):
        sync_cli_tools(not_a_repo)
