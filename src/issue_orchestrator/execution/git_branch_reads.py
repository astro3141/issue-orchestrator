"""Every read of what one branch contributes over a base ref.

Four questions, all asked of the same range and all answered by Git: how many
commits the branch adds, what its patch says, what its tip holds at a given
path, and which files it leaves behind. They lived as four methods on
:class:`~.git_working_copy.GitWorkingCopy` and are gathered here because they
share two things nothing else in that class shares, and both are easy to get
subtly wrong one method at a time:

* **byte-exact transport.** Git delimits patch records and blob lines with LF
  alone and path lists with NUL, so a bare ``\\r`` anywhere in that output is
  content — inside a source line, or inside a filename, which POSIX permits.
  The default universal-newline transport rewrites it, which splits one patch
  record in two (detaching added source from the ``+`` that marks it) and
  silently mutates a path. Every function here takes an :class:`ExactGitRead`
  and none of them runs Git any other way, so no two of these reads can drift
  into different transport semantics.
* **failure is a result, never an empty answer.** These reads feed guards that
  must fail closed. A command that failed returns ``success=False`` with the
  operator-facing output, so no caller can mistake an unreadable branch for a
  branch with nothing in it.

Execution-only: nothing here interprets what it read. Whether an empty diff, a
zero commit count or a forbidden path *means* anything is the calling control
policy's question.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from ..ports.git import GitError, GitResult
from ..ports.working_copy import (
    BranchCommitsResult,
    BranchPathsResult,
    BranchTextFile,
    BranchTextFilesResult,
    DiffResult,
)

logger = logging.getLogger(__name__)


class ExactGitRead(Protocol):
    """One working copy's byte-exact git read, already bound to that copy.

    The supplied callable MUST leave Git's own delimiters untouched — see the
    module docstring for what a translating transport costs. The working copy
    owns how a git command is run, including that guarantee; this module owns
    which commands are run and what a failure means.
    """

    def __call__(self, worktree: Path, args: list[str]) -> GitResult: ...


def git_error_output(error: GitError) -> str:
    """Return the full user-facing output from a failed git command."""
    parts: list[str] = []
    stdout = (error.result.stdout or "").strip()
    stderr = (error.result.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr and stderr != stdout:
        parts.append(stderr)
    if parts:
        return "\n".join(parts)
    return str(error)


def read_nul_paths(read: ExactGitRead, worktree: Path, args: list[str]) -> list[str]:
    """Run a NUL-delimited git path query and return its paths.

    Reading and parsing are one step on purpose: NUL-delimited output is only
    safe to split when the transport left it byte-exact, so there is no
    parse-only entry point a translated read could reach.
    """
    result = read(worktree, args)
    return [path for path in result.stdout.split("\0") if path]


def commits_against_base(
    read: ExactGitRead, worktree: Path, base_ref: str
) -> BranchCommitsResult:
    """Count the commits this branch adds over ``base_ref``.

    ``rev-list --count base_ref..HEAD`` — the RANGE form, not the symmetric
    difference the diff reads use, so commits the base gained after this branch
    was cut are never attributed to the branch.
    """
    try:
        result = read(worktree, ["rev-list", "--count", f"{base_ref}..HEAD"])
        return BranchCommitsResult(success=True, count=int(result.stdout.strip()))
    except GitError as exc:
        error = git_error_output(exc)
        logger.warning(
            "Failed to count commits against %s in %s: %s", base_ref, worktree, error
        )
        return BranchCommitsResult(success=False, error=error)
    except ValueError as exc:
        # Git answered, but not with a number. Unreadable is not empty.
        logger.warning(
            "Unparseable commit count against %s in %s: %s", base_ref, worktree, exc
        )
        return BranchCommitsResult(success=False, error=str(exc))


def diff_against_base(
    read: ExactGitRead, worktree: Path, base_ref: str
) -> DiffResult:
    """Return the branch's unified diff using merge-base semantics."""
    try:
        result = read(
            worktree,
            [
                "diff",
                "--unified=0",
                "--no-ext-diff",
                "--no-color",
                f"{base_ref}...HEAD",
            ],
        )
        return DiffResult(success=True, diff_text=result.stdout)
    except GitError as exc:
        error = git_error_output(exc)
        logger.warning(
            "Failed to read diff against %s in %s: %s", base_ref, worktree, error
        )
        return DiffResult(success=False, error=error)


def branch_text_files(
    read: ExactGitRead, worktree: Path, paths: tuple[str, ...]
) -> BranchTextFilesResult:
    """Return exact tracked ``HEAD`` content for the requested paths.

    Any missing or undecodable path fails the complete request rather than
    returning a partial result, so blob content and the patch text it is
    matched against always agree on where every line ends.
    """
    files: list[BranchTextFile] = []
    try:
        for path in paths:
            result = read(worktree, ["show", f"HEAD:{path}"])
            files.append(BranchTextFile(path=path, content=result.stdout))
        return BranchTextFilesResult(success=True, files=tuple(files))
    except GitError as exc:
        error = git_error_output(exc)
        logger.warning("Failed to read branch-tip text files in %s: %s", worktree, error)
        return BranchTextFilesResult(success=False, error=error)


def post_image_paths_against_base(
    read: ExactGitRead, worktree: Path, base_ref: str
) -> BranchPathsResult:
    """Return branch-tip post-image paths via a path-oriented diff.

    ``--name-only --diff-filter=ACMRT -z`` lists branch-tip files (post-image
    name for renames/copies) excluding deletions, intact through spaces. Unlike
    a unified-diff parser it sees no-hunk and binary changes, so committed
    runtime artifacts cannot slip past path-based guards.
    """
    try:
        paths = read_nul_paths(
            read,
            worktree,
            [
                "diff",
                "--name-only",
                "-z",
                "--no-ext-diff",
                "--diff-filter=ACMRT",
                f"{base_ref}...HEAD",
            ],
        )
        return BranchPathsResult(success=True, paths=tuple(paths))
    except GitError as exc:
        error = git_error_output(exc)
        logger.warning(
            "Failed to read branch paths against %s in %s: %s",
            base_ref,
            worktree,
            error,
        )
        return BranchPathsResult(success=False, error=error)


__all__ = [
    "ExactGitRead",
    "branch_text_files",
    "commits_against_base",
    "diff_against_base",
    "git_error_output",
    "post_image_paths_against_base",
    "read_nul_paths",
]
