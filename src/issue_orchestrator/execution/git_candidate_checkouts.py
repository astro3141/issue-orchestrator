"""Git implementation of :class:`~..ports.candidate_checkout.CandidateCheckouts`.

Execution layer: it does what it is told. The policy question — *may* this
candidate be re-evaluated — belongs to
:mod:`~..control.publication_revalidation`, and nothing here consults it.

What this does own is the one thing the port promises and the caller cannot
check for itself: the checkout it hands back really sits at the commit that was
asked for. ``git worktree add --detach`` at a SHA is the mechanism; re-reading
HEAD afterwards is the proof. A checkout that disagrees is destroyed rather
than returned, because a caller that received it would gate the wrong artifact
and file the verdict under the right candidate's name.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.commit_sha import normalize_commit_sha
from ..ports.candidate_checkout import (
    CandidateCheckoutError,
    MaterializedCandidate,
)
from ..ports.command_runner import CommandRunner
from ..ports.git import Git, GitError

logger = logging.getLogger(__name__)

CHECKOUT_DIR_PREFIX = "revalidate-"
"""Names a disposable revalidation checkout apart from an issue's worktree."""


def build_candidate_checkouts(
    *, repo_root: Path, command_runner: CommandRunner
) -> "GitCandidateCheckouts":
    """The one way to assemble exact-commit checkouts for a repository.

    Owns where they land, because that is an execution concern: beside the
    primary checkout, under a directory named for it, so a revalidation can
    never be confused with — or collide with — an issue's own worktree, and so
    nothing it writes lands inside the repository being evaluated.
    """
    from ..adapters.git.git_cli import GitCLI

    return GitCandidateCheckouts(
        GitCLI(runner=command_runner),
        repo_root=repo_root,
        base_dir=repo_root.parent / f"{repo_root.name}-revalidations",
    )


class GitCandidateCheckouts:
    """Detached worktrees of exact commits, created under one owned base dir."""

    def __init__(self, git: Git, *, repo_root: Path, base_dir: Path) -> None:
        self._git = git
        self._repo_root = repo_root
        self._base_dir = base_dir

    def materialize(self, head_sha: str) -> MaterializedCandidate:
        commit = normalize_commit_sha(head_sha, field_name="head_sha")
        path = self._base_dir / f"{CHECKOUT_DIR_PREFIX}{commit[:12]}"
        if path.exists():
            # Never reused: a directory left behind by an interrupted run holds
            # an unknown tree, and adopting it would gate whatever is in it.
            raise CandidateCheckoutError(
                f"revalidation checkout path already exists: {path}"
            )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._git.worktree_add_detached(self._repo_root, path, commit)
        except GitError as exc:
            raise CandidateCheckoutError(
                f"cannot materialize candidate {commit[:12]}: {exc}"
            ) from exc
        return self._verified(path, commit)

    def release(self, candidate: MaterializedCandidate) -> None:
        self._remove(candidate.path)

    def _verified(self, path: Path, commit: str) -> MaterializedCandidate:
        try:
            actual = normalize_commit_sha(
                self._git.head_sha(path), field_name="materialized head_sha"
            )
        except (GitError, TypeError, ValueError) as exc:
            self._remove(path)
            raise CandidateCheckoutError(
                f"cannot read HEAD of revalidation checkout at {path}: {exc}"
            ) from exc
        if actual != commit:
            self._remove(path)
            raise CandidateCheckoutError(
                "revalidation checkout does not sit at the recorded candidate: "
                f"asked for {commit}, got {actual}"
            )
        logger.info(
            "[REVALIDATION] materialized candidate %s at %s", commit[:12], path
        )
        return MaterializedCandidate(path=path, head_sha=commit)

    def _remove(self, path: Path) -> None:
        """Dispose of a checkout, reporting rather than raising on failure.

        Removal is always the *second* thing happening: either a verification
        that already failed is being cleaned up after, or a run that is already
        over is being released. Letting git's failure out of here would replace
        the real reason with a cleanup error on the first path, and turn a
        finished revalidation into an exception on the second — the route
        promises an outcome, and only ``CandidateCheckoutError`` is shaped to
        become one. A checkout left behind is visible, named after its commit,
        and refused by :meth:`materialize` rather than silently adopted.
        """
        try:
            self._git.worktree_remove(self._repo_root, path)
        except GitError as exc:
            logger.warning(
                "[REVALIDATION] could not remove revalidation checkout at %s: %s",
                path,
                exc,
            )


__all__ = [
    "CHECKOUT_DIR_PREFIX",
    "GitCandidateCheckouts",
    "build_candidate_checkouts",
]
