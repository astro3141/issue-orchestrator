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

import re
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import (
    WorktreeError,
    WorktreeRuntimeSetup,
    can_remove_without_user_changes,
    create_worktree,
    sync_cli_tools,
)
from issue_orchestrator.entrypoints import cli_tools as orchestrator_cli_tools
from issue_orchestrator.infra.runtime_artifacts import ORCHESTRATOR_CLI_TOOLS_DIR
from issue_orchestrator.ports.worktree_manager import WorktreeReuseOptions
from tests.unit.worktree_git_helpers import GitWorktree, make_git_worktree

CLI_TOOLS_DIR = ORCHESTRATOR_CLI_TOOLS_DIR.as_posix()
CANDIDATE_TOOL = f"{CLI_TOOLS_DIR}/coding_done.py"
CANDIDATE_SOURCE = "# candidate branch version of coding_done\n"
# An edit made while the path was hidden: neither the committed version nor any
# copy the orchestrator plants, and invisible to ``git status`` until unhidden.
HIDDEN_AGENT_WORK = "# agent's in-progress edit, written while hidden\n"

# The copies the orchestrator plants from — the only content a repair may treat
# as provably orchestrator-owned.
PACKAGE_CLI_TOOLS = Path(str(orchestrator_cli_tools.__file__)).parent
# What planting actually writes over CANDIDATE_TOOL: a byte copy of the package
# file of that name. Anything else under that path is unproven provenance.
PLANTED_COPY = (PACKAGE_CLI_TOOLS / Path(CANDIDATE_TOOL).name).read_text()
# A tool the package ships that the fixture repository does not track, which is
# what "planted, and the index cannot restore it" looks like.
UNTRACKED_PACKAGE_TOOL = f"{CLI_TOOLS_DIR}/reviewer_done.py"

# Reuse discards uncommitted work on purpose: ``git reset --hard HEAD`` reverts
# tracked files and ``git clean -fd`` removes untracked ones. These two are how
# a test tells "the reset has not run yet" from "the reset ran and happened to
# leave this file alone" — the distinction the whole ordering requirement is
# about.
RESET_WITNESS_TRACKED = "seed"
RESET_WITNESS_TRACKED_EDIT = "seed edited after the last commit\n"
RESET_WITNESS_UNTRACKED = "untracked-witness.txt"


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


def _reusable_self_hosting_worktree(tmp_path: Path) -> GitWorktree:
    """A self-hosting worktree the production reuse path will actually take up.

    Reuse needs a real ``origin``: the update step fetches the base branch
    first and treats a failed fetch as "delete this worktree and start over",
    which would prove nothing about the reset it never reached.

    Both reset witnesses are placed here, so every test using this fixture can
    say whether the destructive half of reuse ran.
    """
    git_worktree = _self_hosting_worktree(tmp_path)
    main_repo = git_worktree.main_repo
    worktree_path = git_worktree.worktree_path

    origin = tmp_path / "origin.git"
    _git("clone", "--bare", str(main_repo), str(origin), cwd=tmp_path)
    # Named ``main`` regardless of what this git installation calls a first
    # branch, because that is the base branch the reuse call asks for.
    base_commit = _git("rev-parse", "HEAD", cwd=main_repo).strip()
    _git("update-ref", "refs/heads/main", base_commit, cwd=origin)
    _git("remote", "add", "origin", str(origin), cwd=main_repo)

    (worktree_path / RESET_WITNESS_TRACKED).write_text(RESET_WITNESS_TRACKED_EDIT)
    (worktree_path / RESET_WITNESS_UNTRACKED).write_text("witness\n")
    return git_worktree


def _reuse_worktree(
    git_worktree: GitWorktree,
) -> tuple[Path, str, str, str | None, bool, int, int]:
    """Drive the production entry point down its reuse path.

    ``create_worktree`` is what the orchestrator calls; the reuse branch of it
    is reached because the fixture's worktree is already registered against the
    branch being asked for. Nothing here reaches into the CLI-tools step — that
    is the point: the ordering under test is whether the lifecycle consults it
    before it starts resetting.
    """
    return create_worktree(
        git_worktree.main_repo,
        6,
        "self-hosting reuse",
        worktree_base=git_worktree.worktree_path.parent,
        base_branch="main",
        branch_name="feature",
        enforce_hooks=False,
        reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
    )


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


def _hide_tracked_path(git_worktree: GitWorktree, rel_path: str, body: str) -> None:
    """Write ``body`` over a tracked path and set ``--skip-worktree`` on it.

    The bit does not make the file read-only, it makes writes to it invisible —
    which is how an agent's edit ends up hidden, not just a planted copy.
    """
    worktree_path = git_worktree.worktree_path
    (worktree_path / rel_path).write_text(body)
    _git("update-index", "--skip-worktree", "--", rel_path, cwd=worktree_path)


def _plant_stale_overlay(git_worktree: GitWorktree) -> None:
    """Reproduce the pre-fix state: planted copy, hidden bit, exclude entry.

    The body is the packaged file itself, because that is what planting copies.
    A stand-in string would reproduce the shape of the overlay but not its
    provenance, and provenance is the whole basis on which a repair is allowed.
    """
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, PLANTED_COPY)
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


def test_a_path_git_has_to_quote_is_still_handled(tmp_path) -> None:
    """Parsing git's output without ``-z`` would hand git back a quoted name.

    Git quotes any path holding a control character or a non-ASCII byte, and
    that quoted string fed to ``update-index`` or matched against ``status``
    output names a file that does not exist — the path is then never unhidden
    and never reported, on a name git described perfectly well.

    The package ships no tool by this name, so no repair can prove it planted
    this file; the outcome asserted is therefore the conservative one, reached
    through the exact path git named.
    """
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    quoted_tool = f"{CLI_TOOLS_DIR}/needs\tquoting.py"
    (worktree_path / quoted_tool).write_text(CANDIDATE_SOURCE)
    _git("add", "--", quoted_tool, cwd=worktree_path)
    _git("commit", "-m", "tool with a tab in its name", cwd=worktree_path)
    _hide_tracked_path(git_worktree, quoted_tool, HIDDEN_AGENT_WORK)

    with pytest.raises(WorktreeError, match="no orchestrator copy explains"):
        sync_cli_tools(worktree_path)

    assert (worktree_path / quoted_tool).read_text() == HIDDEN_AGENT_WORK
    assert set(_index_flags(worktree_path).values()) == {"H"}
    # ``-z`` here for the same reason production uses it: plain porcelain would
    # quote this name, which is not the name anything else works with.
    assert quoted_tool in _git("status", "--porcelain", "-z", cwd=worktree_path)


def test_a_path_git_would_read_as_a_glob_is_still_handled(tmp_path) -> None:
    """A name holding ``[…]``, ``*`` or ``?`` reaches the same outcome.

    Pathspecs glob, and every path here is handed back to git as one. Git
    compares the whole string before it globs, so such a file is always found
    under its own name and this outcome holds either way — what the
    ``:(literal)`` prefix in production removes is the *surplus* matching, on
    the restore path, where ``checkout -- 'tool[1].py'`` would revert
    ``tool1.py`` alongside it. Only a name the orchestrator's own package ships
    can reach that call, so it is not reproducible from here; the outcome for a
    glob-shaped name is, and is what this pins.
    """
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    glob_tool = f"{CLI_TOOLS_DIR}/needs[1]literal.py"
    (worktree_path / glob_tool).write_text(CANDIDATE_SOURCE)
    # Literal here too: as a pattern this name matches nothing that exists.
    _git("add", "--", f":(literal){glob_tool}", cwd=worktree_path)
    _git("commit", "-m", "tool with a glob character in its name", cwd=worktree_path)
    _hide_tracked_path(git_worktree, glob_tool, HIDDEN_AGENT_WORK)

    with pytest.raises(WorktreeError, match=re.escape(glob_tool)):
        sync_cli_tools(worktree_path)

    assert (worktree_path / glob_tool).read_text() == HIDDEN_AGENT_WORK
    assert set(_index_flags(worktree_path).values()) == {"H"}
    assert glob_tool in _git("status", "--porcelain", cwd=worktree_path)


# ---------------------------------------------------------------------------
# Hidden is not the same as worthless
#
# --skip-worktree does not make a path read-only; it makes writes to it
# invisible. A session that died mid-edit therefore leaves real work that
# ``git status`` calls clean, and a repair that reverts every hidden path to the
# index destroys it exactly as invisibly as it was made.
# ---------------------------------------------------------------------------


def test_hidden_agent_work_is_preserved_rather_than_reverted(tmp_path) -> None:
    """Hidden, and neither the index version nor the copy the orchestrator plants."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)
    # Precondition: this is the dangerous combination, not either half of it.
    assert HIDDEN_AGENT_WORK not in (CANDIDATE_SOURCE, PLANTED_COPY)
    assert _git("status", "--porcelain", cwd=worktree_path).strip() == ""

    with pytest.raises(WorktreeError, match=CANDIDATE_TOOL):
        sync_cli_tools(worktree_path)

    assert (worktree_path / CANDIDATE_TOOL).read_text() == HIDDEN_AGENT_WORK


def test_preserved_work_is_left_visible_to_git(tmp_path) -> None:
    """Preserving it while still hidden would only postpone the same loss."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)

    with pytest.raises(WorktreeError):
        sync_cli_tools(worktree_path)

    assert set(_index_flags(worktree_path).values()) == {"H"}
    assert CANDIDATE_TOOL in _git("status", "--porcelain", cwd=worktree_path)


def test_setup_proceeds_once_the_work_is_no_longer_hidden(tmp_path) -> None:
    """The failure is one-shot: nothing is hidden any more, so a rerun is normal.

    Without this the escalation would be a dead end — every later run would
    refuse the same worktree over a file git is now reporting like any other.
    """
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)
    with pytest.raises(WorktreeError):
        sync_cli_tools(worktree_path)

    assert sync_cli_tools(worktree_path) == []

    assert (worktree_path / CANDIDATE_TOOL).read_text() == HIDDEN_AGENT_WORK


def test_a_hidden_file_matching_the_index_is_not_an_escalation(tmp_path) -> None:
    """Nothing diverges, so there is nothing for a human to decide."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, CANDIDATE_SOURCE)

    assert sync_cli_tools(worktree_path) == []

    assert (worktree_path / CANDIDATE_TOOL).read_text() == CANDIDATE_SOURCE
    assert set(_index_flags(worktree_path).values()) == {"H"}


def test_a_hidden_deletion_is_preserved_rather_than_undone(tmp_path) -> None:
    """An absent file is as unexplained as unexpected content, and as final."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)
    (worktree_path / CANDIDATE_TOOL).unlink()

    with pytest.raises(WorktreeError, match=CANDIDATE_TOOL):
        sync_cli_tools(worktree_path)

    assert not (worktree_path / CANDIDATE_TOOL).exists()


def test_a_planted_copy_is_still_undone_next_to_work_that_is_not(tmp_path) -> None:
    """Per file, not per directory: one proven overlay, one file that is not."""
    git_worktree = _self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    second_tool = f"{CLI_TOOLS_DIR}/reviewer_done.py"
    (worktree_path / second_tool).write_text(CANDIDATE_SOURCE)
    _git("add", "--", second_tool, cwd=worktree_path)
    _git("commit", "-m", "second candidate tool", cwd=worktree_path)
    _plant_stale_overlay(git_worktree)
    _hide_tracked_path(git_worktree, second_tool, HIDDEN_AGENT_WORK)

    with pytest.raises(WorktreeError, match=second_tool):
        sync_cli_tools(worktree_path)

    assert (worktree_path / CANDIDATE_TOOL).read_text() == CANDIDATE_SOURCE
    assert (worktree_path / second_tool).read_text() == HIDDEN_AGENT_WORK


# ---------------------------------------------------------------------------
# Ordering: the check is a precondition of reuse, not a step inside setup
#
# Reuse rebases, hard-resets and cleans the worktree *before* runtime setup runs
# at all. Preserving hidden work only once setup is reached would therefore be
# preserving whatever the reset happened to leave — so these drive the
# production entry point, ``create_worktree``, rather than the CLI-tools helper,
# and assert on the two witnesses the reset would have destroyed.
# ---------------------------------------------------------------------------


def test_reuse_stops_before_it_resets_over_hidden_agent_work(tmp_path) -> None:
    """The whole ordering requirement, through the call the orchestrator makes."""
    git_worktree = _reusable_self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)
    # Precondition: hidden, and holding neither the index version nor a copy
    # the orchestrator plants — so no repair can claim it.
    assert HIDDEN_AGENT_WORK not in (CANDIDATE_SOURCE, PLANTED_COPY)
    assert CLI_TOOLS_DIR not in _git("status", "--porcelain", cwd=worktree_path)

    with pytest.raises(WorktreeError, match=CANDIDATE_TOOL):
        _reuse_worktree(git_worktree)

    # The work itself, byte for byte.
    assert (worktree_path / CANDIDATE_TOOL).read_text() == HIDDEN_AGENT_WORK
    # And nothing destructive ran on the way to refusing: ``reset --hard``
    # would have reverted the tracked witness, ``clean -fd`` removed the
    # untracked one.
    assert (worktree_path / RESET_WITNESS_TRACKED).read_text() == RESET_WITNESS_TRACKED_EDIT
    assert (worktree_path / RESET_WITNESS_UNTRACKED).exists()


def test_reuse_leaves_the_work_it_refused_to_reset_visible_to_git(tmp_path) -> None:
    """Preserved but still hidden would leave the operator nothing to act on.

    The escalation asks a human to commit or discard the file. That is only a
    request they can answer if git reports it, which means the
    ``--skip-worktree`` bit has to be gone by the time reuse stops.
    """
    git_worktree = _reusable_self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)

    with pytest.raises(WorktreeError):
        _reuse_worktree(git_worktree)

    assert set(_index_flags(worktree_path).values()) == {"H"}
    assert CANDIDATE_TOOL in _git("status", "--porcelain", cwd=worktree_path)


def test_reuse_is_not_downgraded_to_a_recreate(tmp_path) -> None:
    """Recreating deletes the worktree — with the file that was just preserved.

    Everywhere else in the lifecycle a worktree that cannot be prepared is
    deleted and rebuilt. Here that policy is exactly wrong, so the refusal has
    to leave the lifecycle rather than be reported back into it.
    """
    git_worktree = _reusable_self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _hide_tracked_path(git_worktree, CANDIDATE_TOOL, HIDDEN_AGENT_WORK)

    with pytest.raises(WorktreeError):
        _reuse_worktree(git_worktree)

    assert worktree_path.exists()
    assert (worktree_path / CANDIDATE_TOOL).read_text() == HIDDEN_AGENT_WORK


def test_reuse_proceeds_normally_when_nothing_is_hidden(tmp_path) -> None:
    """The guard must gate reuse, not end it: the ordinary case still reuses.

    Here the reset is expected to run, so the same two witnesses are asserted
    the other way round — they are what proves the lifecycle got past the
    precondition instead of quietly failing somewhere before it.
    """
    git_worktree = _reusable_self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path

    reused_path, branch, reuse_status, *_ = _reuse_worktree(git_worktree)

    assert (reused_path, branch, reuse_status) == (worktree_path, "feature", "reused")
    assert (worktree_path / CANDIDATE_TOOL).read_text() == CANDIDATE_SOURCE
    assert (worktree_path / RESET_WITNESS_TRACKED).read_text() != RESET_WITNESS_TRACKED_EDIT
    assert not (worktree_path / RESET_WITNESS_UNTRACKED).exists()


def test_reuse_repairs_a_stale_overlay_and_carries_on(tmp_path) -> None:
    """A provable planted copy is repaired in the precondition, not escalated.

    Provenance decides between the two outcomes, and it decides it before the
    reset either way: this is the branch where the answer is "the orchestrator
    wrote that", so reuse continues with the candidate's own file restored.
    """
    git_worktree = _reusable_self_hosting_worktree(tmp_path)
    worktree_path = git_worktree.worktree_path
    _plant_stale_overlay(git_worktree)

    _, _, reuse_status, *_ = _reuse_worktree(git_worktree)

    assert reuse_status == "reused"
    assert (worktree_path / CANDIDATE_TOOL).read_text() == CANDIDATE_SOURCE
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
