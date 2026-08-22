"""Repository identity, resolved with real git status.

:mod:`...infra.repo_identity` reads git metadata files directly and never
shells out, so it can name the commit but cannot tell whether the tree matches
it. Dirtiness only appears when a caller supplies a status resolver, and that
resolver is an execution-layer concern. This module is the single place that
supplies it: two callers wiring their own would be two answers to "is this
checkout clean", which is not a question a codebase should have two answers to.

Two accessors, because two callers need opposite failure directions and the
difference is a decision, not an accident:

* :func:`build_repo_identity` is the Control Center's. A status read that
  fails degrades to "clean" (:func:`...infra.repo_identity.
  build_repo_identity_with_status` swallows resolver errors by design) because
  the consequence is a diagnostic panel that shows one field less.
* :func:`working_tree_is_dirty` is for callers where the answer gates
  something. It fails **closed**: a status read that errors reads as dirty,
  because "we could not tell" must never be spent as "it was clean".
"""

from __future__ import annotations

from pathlib import Path

from ..infra.repo_identity import RepoIdentity, build_repo_identity_with_status
from .git_working_copy import GitWorkingCopy


def resolve_repo_status(root: Path) -> tuple[str | None, list[str]]:
    """Branch and ``git status --porcelain`` lines for the checkout at ``root``."""
    git = GitWorkingCopy()
    branch: str | None
    try:
        branch = git.get_current_branch(root)
    except Exception:
        branch = None
    return branch, git.get_status_porcelain_lines(root)


def build_repo_identity(repo_root: Path) -> RepoIdentity:
    """Repository identity including working-tree state, for display and handshake."""
    return build_repo_identity_with_status(
        repo_root, status_resolver=resolve_repo_status
    )


def working_tree_is_dirty(root: Path) -> bool:
    """Whether ``root`` holds anything uncommitted, failing closed on error."""
    return GitWorkingCopy().has_uncommitted_changes(root)


__all__ = [
    "build_repo_identity",
    "resolve_repo_status",
    "working_tree_is_dirty",
]
