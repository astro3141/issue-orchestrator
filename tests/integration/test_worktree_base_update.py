"""The base-update invariant, on the path that used to skip it (#79).

A worktree that is absent but whose issue branch is still present locally is
the ordinary state after cleanup: the checkout goes, the branch stays. The next
launch for that issue takes the *creation* path, and creation used to attach
that branch exactly as it was found — no fetch, no ancestry check, no rebase —
while reuse rebased the branch it found in place. A branch last left on a base
36 commits old therefore became the base a candidate was built on, and the
launch reported the worktree ready without logging a rebase, reset or
fast-forward, because none had happened.

The invariant these tests hold is one sentence, and it belongs to both paths:

    when ``create`` returns, the worktree's branch has the current base branch
    in its ancestry, and still carries its own commits.

Everything here is real Git against a real remote — a bare repository on disk,
which is what keeps the whole file offline and deterministic. Nothing pushes:
the origin's refs are captured before each exercise and asserted unchanged
after, so a fix that "updated" a branch by publishing it would fail rather than
pass quietly.

The `preserve_branch` exemption is proven too. A tech_lead investigation reads
its subject's branch as evidence, so it must survive creation untouched exactly
as it survives reuse — the update is skipped, not merely reordered.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import WorktreeError
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
from issue_orchestrator.ports.worktree_manager import WorktreeInfo, WorktreeReuseOptions

#: The base branch every test names explicitly. Auto-detection would make the
#: exercise depend on the runner's environment, and the invariant is about
#: *which* base, so the base is stated rather than discovered.
BASE = "main"

#: The issue these worktrees are created for. One number throughout: the
#: worktree directory basename is derived from it, so reusing it is what makes
#: the reuse test find the worktree the create test would have made.
ISSUE = 79


@dataclass(frozen=True)
class Repository:
    """A bare remote plus the primary checkout the orchestrator works from."""

    origin: Path
    root: Path
    worktrees: Path
    scratch: Path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def _commit(checkout: Path, filename: str, content: str) -> str:
    (checkout / filename).write_text(content, encoding="utf-8")
    _git(checkout, "add", filename)
    _git(checkout, "commit", "-m", f"add {filename}")
    return _git(checkout, "rev-parse", "HEAD")


def _is_ancestor(checkout: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _refs(bare_repo: Path) -> str:
    """Every ref the remote holds — the thing no test here may change."""
    return _git(bare_repo, "show-ref")


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    """A checkout with one commit pushed to its own bare origin."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", BASE, str(origin))

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", BASE)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "remote", "add", "origin", str(origin))
    _commit(root, "base.txt", "base\n")

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo = Repository(origin=origin, root=root, worktrees=worktrees, scratch=scratch)
    _publish_base(repo)
    return repo


def _publish_base(repo: Repository) -> str:
    """Move the remote's base to the checkout's — without a push.

    The remote fetches from the checkout rather than the checkout pushing to
    it. The refs end up identical, and the exercise keeps the property the
    issue asks these tests to prove: nothing here pushes, so the count of
    pushes a passing run performs is zero rather than "zero to GitHub".
    """
    _git(repo.origin, "fetch", str(repo.root), f"{BASE}:{BASE}")
    _git(repo.root, "fetch", "origin", BASE)
    return _git(repo.root, "rev-parse", f"origin/{BASE}")


def _stranded_branch(repo: Repository, branch: str) -> str:
    """Leave ``branch`` behind at its own commit, with no worktree on it.

    Built the way the real one is: a worktree does the work, and cleanup takes
    the checkout away and leaves the branch. Returns the tip it is stranded at.
    """
    checkout = repo.scratch / branch
    _git(repo.root, "worktree", "add", "-b", branch, str(checkout), f"origin/{BASE}")
    head = _commit(checkout, "agent-work.txt", "work only this branch has\n")
    _git(repo.root, "worktree", "remove", "--force", str(checkout))
    return head


def _advance_base(repo: Repository, commits: int = 3) -> str:
    """Move the base on the remote, the way everyone else's merges do."""
    for index in range(commits):
        _commit(repo.root, f"base-{index}.txt", f"base commit {index}\n")
    return _publish_base(repo)


def _create(repo: Repository, *, branch: str | None, **options: bool) -> WorktreeInfo:
    return GitWorktreeManager().create(
        repo_root=repo.root,
        issue_number=ISSUE,
        issue_title="Worktree base update",
        worktree_base=repo.worktrees,
        enforce_hooks=False,
        branch_name=branch,
        base_branch=BASE,
        reuse_options=WorktreeReuseOptions(reuse_push_preflight=False, **options),
    )


def test_a_stranded_branch_is_updated_onto_the_current_base_before_it_is_handed_over(
    repo,
):
    """The incident itself: no worktree, a local branch, a base that moved on.

    This is the failure-direction proof. Creation attaches the existing branch —
    it is the only plan that adopts a tip rather than building one — and before
    the fix it attached it untouched, so the assertion below found the base
    absent from the branch's history. Revert the update and this test fails on
    exactly that.
    """
    branch = f"{ISSUE}-stranded"
    stranded_head = _stranded_branch(repo, branch)
    base_head = _advance_base(repo)
    assert not _is_ancestor(repo.root, base_head, stranded_head)
    refs_before = _refs(repo.origin)

    info = _create(repo, branch=branch)

    assert info.reuse_status == "created"
    assert _is_ancestor(info.path, f"origin/{BASE}", "HEAD")
    assert _is_ancestor(info.path, base_head, "HEAD")
    assert _refs(repo.origin) == refs_before


def test_the_stranded_branchs_own_commits_survive_the_update(repo):
    """Updating onto the base may not cost the branch the work that was on it.

    A hard reset to the base would satisfy the ancestry assertion above and
    silently throw the session's evidence away, so the surviving commit is
    asserted by content, not by count.
    """
    branch = f"{ISSUE}-stranded-work"
    _stranded_branch(repo, branch)
    _advance_base(repo)

    info = _create(repo, branch=branch)

    assert (info.path / "agent-work.txt").read_text() == "work only this branch has\n"
    assert info.commits_discarded == 0
    assert _is_ancestor(info.path, f"origin/{BASE}", "HEAD")


def test_ordinary_fresh_branch_creation_still_starts_from_the_base(repo):
    """No local branch, no remote branch — the untouched majority path.

    A branch built from ``origin/<base>`` is already on the current base, and
    the update must not change where it starts or what it is called.
    """
    _advance_base(repo)
    base_head = _git(repo.root, "rev-parse", f"origin/{BASE}")
    refs_before = _refs(repo.origin)

    info = _create(repo, branch=None)

    assert info.reuse_status == "created"
    assert info.branch_name == f"{ISSUE}-worktree-base-update"
    assert _git(info.path, "rev-parse", "HEAD") == base_head
    assert _git(info.path, "rev-parse", "--abbrev-ref", "HEAD") == info.branch_name
    assert info.uncommitted_discarded == 0
    assert info.commits_discarded == 0
    assert _refs(repo.origin) == refs_before


def test_reusing_an_existing_worktree_still_updates_it_onto_the_base(repo):
    """The path that already held the invariant, proven rather than assumed.

    The worktree is present this time, so creation never runs. Reuse rebases
    what it finds, and it must keep doing exactly that.
    """
    branch = f"{ISSUE}-reused"
    checkout = repo.worktrees / f"repo-{ISSUE}"
    _git(repo.root, "worktree", "add", "-b", branch, str(checkout), f"origin/{BASE}")
    _commit(checkout, "agent-work.txt", "work only this branch has\n")
    base_head = _advance_base(repo)
    refs_before = _refs(repo.origin)

    info = _create(repo, branch=branch)

    assert info.reuse_status == "reused"
    assert info.path == checkout
    assert _is_ancestor(info.path, base_head, "HEAD")
    assert (info.path / "agent-work.txt").read_text() == "work only this branch has\n"
    assert _refs(repo.origin) == refs_before


def test_preserve_branch_leaves_an_attached_branch_exactly_where_it_was(repo):
    """The tech_lead exemption, on the creation path (#6823).

    An investigation reads its subject's branch as evidence. Rebasing it would
    rewrite the very commits being read, and hard-resetting it would destroy
    unpushed work no PR holds — so when the subject's worktree is gone and the
    branch is attached fresh, the branch must arrive unmoved.
    """
    branch = f"{ISSUE}-investigated"
    stranded_head = _stranded_branch(repo, branch)
    _advance_base(repo)
    refs_before = _refs(repo.origin)

    info = _create(repo, branch=branch, preserve_branch=True)

    assert _git(info.path, "rev-parse", "HEAD") == stranded_head
    assert not _is_ancestor(info.path, f"origin/{BASE}", "HEAD")
    assert _refs(repo.origin) == refs_before


def test_a_branch_that_cannot_reach_the_base_fails_the_create_loudly(repo):
    """No base, no session: the branch's age stays unknown, so nothing starts.

    Silently attaching an unverifiable branch is the defect this issue is
    about; degrading to it when the remote is unreachable would reintroduce it
    for exactly the case where the branch is most likely to be stale.
    """
    branch = f"{ISSUE}-unreachable"
    _stranded_branch(repo, branch)
    _advance_base(repo)
    repo.origin.rename(repo.origin.parent / "origin-gone.git")

    with pytest.raises(WorktreeError, match="update attached branch onto current base"):
        _create(repo, branch=branch)
