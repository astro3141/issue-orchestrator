"""Sibling reviewer worktree management for persistent-session review exchange.

Each persistent-session review exchange uses a separate reviewer worktree
in detached-HEAD on the coder's branch tip. This sidesteps Claude Code's
project-level lock (which prevents two Claude sessions in the same project
root) and is provider-agnostic — it works for any coder/reviewer pair.

Lifecycle:
- ``create_reviewer_worktree`` at exchange start (detached HEAD on coder tip).
- ``fast_forward_reviewer_worktree`` before each reviewer round so the
  reviewer always sees the latest committed state of the coder's branch.
- ``remove_reviewer_worktree`` at exchange end.

``ReviewerCandidatePresentation`` composes the per-round half of that
lifecycle and reports which commit it put in front of the reviewer.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..adapters.worktree.api import (
    REVIEW_COMMAND_GUARD_SETTINGS,
    WorktreeError,
    install_review_command_guard,
    install_worktree_identity,
)
from ..domain.artifact_contracts import AgentProvider
from ..domain.review_exchange import REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER
from ..infra.repo_identity import get_repo_head_sha
from ..ports.worktree_manager import REVIEWER_OWNED_HEAD_MARKER, WORKTREE_ID_MARKER

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewerWorktree:
    """A short-lived reviewer worktree for one review-exchange run."""

    path: Path
    coder_branch: str


@dataclass(frozen=True)
class GitCommandFailure:
    """Captured context from a failed git invocation.

    Carries everything an operator needs to tell apart the failure modes that
    look identical in a bare ``CalledProcessError`` message: dirty runtime
    files, a missing commit, a missing worktree, lock contention, etc. (#6659).
    """

    args: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, object]:
        return {
            "args": list(self.args),
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    def summary(self) -> str:
        cmd = " ".join(self.args)
        stderr = self.stderr.strip() or "<empty>"
        stdout = self.stdout.strip() or "<empty>"
        return (
            f"git command failed (exit {self.returncode}): {cmd} "
            f"[cwd={self.cwd}] stderr={stderr!r} stdout={stdout!r}"
        )


class ReviewerWorktreeError(RuntimeError):
    """Raised when reviewer-worktree management fails.

    When the underlying cause is a failed git command, ``git_failure`` carries
    the full command/cwd/returncode/stdout/stderr context and ``context`` holds
    review-exchange specifics (reviewer worktree path, coder branch, target SHA)
    so the surfaced diagnostic can pinpoint the cause precisely.
    """

    def __init__(
        self,
        message: str,
        *,
        git_failure: GitCommandFailure | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.git_failure = git_failure
        self.context: dict[str, object] = context or {}

    def diagnostic(self) -> dict[str, object]:
        """Structured diagnostic payload for logs and failure records."""
        payload: dict[str, object] = {"message": str(self), **self.context}
        if self.git_failure is not None:
            payload["git"] = self.git_failure.as_dict()
        return payload


def create_reviewer_worktree(
    *,
    coder_worktree: Path,
    coder_branch: str,
    timestamp: str,
    reviewer_provider: AgentProvider,
) -> ReviewerWorktree:
    """Create a sibling reviewer worktree in detached HEAD on the coder's branch tip.

    The sibling lives at ``<coder_worktree>-review-<timestamp>``. Detached
    HEAD is required because the coder's branch is already checked out in
    the coder worktree; git refuses to check out the same branch twice.

    **This worktree is deliberately unprovisioned.** It is created here with a
    raw ``git worktree add`` rather than through ``WorktreeManager``, so nothing
    ``worktrees.setup`` installs reaches it and it has no runtime environment at
    all — it is the one agent worktree ``WorktreeProvisioner`` does not
    own (#48, ``docs/architecture/validation.md``). The reviewer reads code; it
    does not run gates, and paying ``worktrees.setup`` (an ``npm ci`` and a
    browser install for this repository) per exchange to support a command that
    cannot run here is not a trade worth making.

    What keeps the exemption safe is a barrier, not an instruction:
    :func:`install_review_command_guard` registers a ``PreToolUse`` policy in
    this worktree that *refuses* build, test and validation commands before they
    execute, pinned to the orchestrator's own copy of that policy
    (``docs/architecture/hooks.md`` — prompts are suggestions, hooks are
    enforcement). ``REVIEWER_WORKTREE_IS_UNPROVISIONED_NOTE`` stays in every
    reviewer prompt so a refusal is expected rather than surprising, but the
    invariant no longer rests on the reviewer reading it. Installing the guard
    is part of taking ownership of the worktree: if it cannot be installed, the
    worktree is rolled back and creation fails.

    ``reviewer_provider`` is what stops that from being a claim rather than a
    fact. The guard is registered through one provider's hook mechanism, so a
    reviewer launched on a provider that mechanism does not reach would get a
    worktree that *looks* guarded and is not. Passing the provider the exchange
    actually launches lets the installer write nothing in that case and say so
    (``ReviewCommandGuardOutcome.guarded``), which is logged here at WARNING.

    **The gap that leaves.** ``claude-code`` is the only guardable provider
    today, and this repository's default mode configures a Codex reviewer, so
    for that configuration the note in the reviewer's prompt is still the only
    thing between the reviewer and a gate command
    (``docs/architecture/validation.md`` — "the one worktree that is exempt").
    Closing it needs either a Codex-loadable guard (its project-local exec
    policies are disabled until the project is trusted, and this worktree is
    brand new) or provisioning the worktree instead of exempting it. Both are
    larger than a guard installer, so neither is decided here; what *is*
    decided here is that no configuration gets a decorative one.
    """
    sibling = coder_worktree.parent / f"{coder_worktree.name}-review-{timestamp}"
    if sibling.exists():
        raise ReviewerWorktreeError(
            f"Reviewer worktree path already exists: {sibling}"
        )

    repo_root = _resolve_repo_root(coder_worktree)
    tip_sha = _resolve_branch_tip(repo_root, coder_branch)
    try:
        _git(repo_root, ["worktree", "add", "--detach", str(sibling), tip_sha])
    except ReviewerWorktreeError as exc:
        # Checking out the coder branch tip into the reviewer worktree is the
        # operation that committed runtime artifacts break (#6659). Mark it so
        # completion failure-reporting can attach runtime-artifact recovery
        # guidance to exactly this class.
        raise ReviewerWorktreeError(
            f"Failed to create reviewer worktree {sibling} at "
            f"{coder_branch}@{tip_sha}: {exc} "
            f"{REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER}",
            git_failure=exc.git_failure,
            context={
                "reviewer_worktree": str(sibling),
                "coder_branch": coder_branch,
                "target_sha": tip_sha,
            },
        ) from exc
    try:
        install_worktree_identity(sibling)
        guard = install_review_command_guard(sibling, provider=reviewer_provider)
        _persist_owned_head(sibling, tip_sha)
    except (WorktreeError, ReviewerWorktreeError) as exc:
        try:
            _git(repo_root, ["worktree", "remove", str(sibling), "--force"])
        except ReviewerWorktreeError:
            logger.exception("Failed to roll back unowned reviewer worktree %s", sibling)
        raise ReviewerWorktreeError(
            f"Failed to install reviewer ownership and command guard in "
            f"worktree {sibling}: {exc}",
            context={
                "reviewer_worktree": str(sibling),
                "coder_branch": coder_branch,
                "target_sha": tip_sha,
            },
        ) from exc
    logger.info(
        "Created reviewer worktree path=%s coder_branch=%s tip=%s provider=%s "
        "guarded=%s",
        sibling,
        coder_branch,
        tip_sha,
        reviewer_provider.value,
        guard.guarded,
    )
    return ReviewerWorktree(path=sibling, coder_branch=coder_branch)


def fast_forward_reviewer_worktree(reviewer: ReviewerWorktree) -> str:
    """Fast-forward the reviewer worktree to the current tip of the coder's branch.

    Returns the SHA the worktree now points at. Always uses detached HEAD so
    we never conflict with the coder's branch checkout.
    """
    repo_root = _resolve_repo_root(reviewer.path)
    tip_sha = _resolve_branch_tip(repo_root, reviewer.coder_branch)
    try:
        _git(reviewer.path, ["checkout", "--detach", tip_sha])
    except ReviewerWorktreeError as exc:
        context: dict[str, object] = {
            "reviewer_worktree": str(reviewer.path),
            "coder_branch": reviewer.coder_branch,
            "target_sha": tip_sha,
        }
        enriched = ReviewerWorktreeError(
            "Failed to fast-forward reviewer worktree "
            f"{reviewer.path} to {reviewer.coder_branch}@{tip_sha}: "
            f"{exc} {REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER}",
            git_failure=exc.git_failure,
            context=context,
        )
        logger.error(
            "Reviewer worktree fast-forward failed: %s",
            enriched.diagnostic(),
        )
        raise enriched from exc
    _persist_owned_head(reviewer.path, tip_sha)
    logger.debug(
        "Fast-forwarded reviewer worktree path=%s tip=%s",
        reviewer.path,
        tip_sha,
    )
    return tip_sha


@dataclass(frozen=True)
class ReviewerCandidatePresentation:
    """Puts the candidate commit in front of the reviewer, and says which one.

    One owner answers "what is the reviewer looking at this round", because the
    answer is half of an authority record: the exact-SHA verdict binding pairs
    the orchestrator's verdict with the commit the reviewer actually read
    (``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4).

    The reported SHA therefore comes from the checkout that *put* the commit
    there, never from a later independent observation. The coder's branch can
    advance at any moment, so a read taken after the checkout can name a commit
    the reviewer's filesystem never held — an approval for work no reviewer saw,
    which is precisely what the binding exists to prevent.

    ``before_round`` is the caller's own per-round hook. It runs after the
    checkout, exactly as the exchange loop has always ordered it, and cannot
    change what this reports.
    """

    reviewer_worktree_path: Path
    coder_branch: str | None
    before_round: Callable[[int], None] | None = None

    def present(self, round_index: int) -> str | None:
        """Prepare the reviewer's round; return the commit it will read.

        Returns None only when the presented commit cannot be established at
        all. Callers must treat that as "unknown", never as "current HEAD".
        """
        presented = self._checkout_candidate()
        if self.before_round is not None:
            self.before_round(round_index)
        return presented

    def _checkout_candidate(self) -> str | None:
        if self.coder_branch is None:
            # No branch to track, so nothing re-points the worktree: what the
            # reviewer holds is whatever it was created at. Still read from the
            # reviewer's own worktree — the coder's is a different filesystem
            # and answers a different question.
            return get_repo_head_sha(self.reviewer_worktree_path)
        return fast_forward_reviewer_worktree(
            ReviewerWorktree(
                path=self.reviewer_worktree_path,
                coder_branch=self.coder_branch,
            ),
        )


def remove_reviewer_worktree(
    reviewer: ReviewerWorktree, *, force: bool = False,
) -> None:
    """Remove the reviewer worktree at exchange end.

    With ``force=True`` we tolerate failure (use it when the orchestrator is
    cleaning up after a crash); without it we raise so the caller can surface
    a real problem.
    """
    if not reviewer.path.exists():
        return
    repo_root = _resolve_repo_root(reviewer.path)
    # Everything the reviewer lifecycle planted here is untracked, and
    # ``git worktree remove`` refuses a worktree that still holds untracked
    # files. Each is lifted (and restored below if the removal then fails).
    marker_contents: dict[Path, str] = {}
    for relative_marker in (
        WORKTREE_ID_MARKER,
        REVIEWER_OWNED_HEAD_MARKER,
        REVIEW_COMMAND_GUARD_SETTINGS,
    ):
        marker = reviewer.path / relative_marker
        try:
            marker_contents[marker] = marker.read_text(encoding="utf-8")
            marker.unlink()
        except OSError:
            continue
    args = ["worktree", "remove", str(reviewer.path)]
    if force:
        args.append("--force")
    try:
        _git(repo_root, args)
    except ReviewerWorktreeError as exc:
        if reviewer.path.exists():
            for marker, marker_content in marker_contents.items():
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(marker_content, encoding="utf-8")
                except OSError:
                    logger.exception(
                        "Failed to restore reviewer ownership marker after removal failure: %s",
                        marker,
                    )
        if force:
            logger.warning(
                "git worktree remove --force failed for %s: %s",
                reviewer.path,
                exc.diagnostic(),
            )
            return
        raise ReviewerWorktreeError(
            f"Failed to remove reviewer worktree {reviewer.path}: {exc}",
            git_failure=exc.git_failure,
            context={"reviewer_worktree": str(reviewer.path)},
        ) from exc


def resolve_current_branch(worktree_path: Path) -> str:
    """Resolve the named branch checked out in ``worktree_path``.

    Used by the persistent-session exchange dispatch to know what branch
    the reviewer worktree should track. Raises if the worktree is on
    detached HEAD or has no resolvable branch — the reviewer worktree
    needs a real branch tip to fast-forward to between rounds.

    Lives in this execution module so the control layer can compose it
    without importing ``subprocess`` directly (architectural lint
    forbids ``control.* -> subprocess``).
    """
    result = _git(worktree_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        raise ReviewerWorktreeError(
            f"Worktree {worktree_path} is detached or has no resolvable branch; "
            "review-exchange requires a named branch to point the reviewer at."
        )
    return branch


def _resolve_repo_root(worktree_path: Path) -> Path:
    result = _git(worktree_path, ["rev-parse", "--git-common-dir"])
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (worktree_path / common_dir).resolve()
    return common_dir.parent


def _resolve_branch_tip(repo_root: Path, branch: str) -> str:
    result = _git(repo_root, ["rev-parse", branch])
    return result.stdout.strip()


def _persist_owned_head(worktree_path: Path, head: str) -> None:
    """Record the exact detached tip installed by the reviewer lifecycle.

    Startup recovery compares this value with the registered detached HEAD.
    A failed or interrupted write therefore fails closed: recovery retains the
    checkout rather than guessing whether a reviewer committed local work.
    """
    marker = worktree_path / REVIEWER_OWNED_HEAD_MARKER
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(marker, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as owned_head_file:
            owned_head_file.write(f"{head}\n")
    except OSError as exc:
        raise ReviewerWorktreeError(
            f"Failed to persist reviewer owned HEAD at {marker}: {exc}",
            context={"reviewer_worktree": str(worktree_path), "target_sha": head},
        ) from exc


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command, raising a richly-contextualized error on failure.

    Unlike ``check=True`` (which surfaces only the command and exit code),
    failures here carry cwd, return code, and captured stdout/stderr so the
    caller's diagnostic can name the precise Git state problem (#6659).
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        failure = GitCommandFailure(
            args=("git", *args),
            cwd=str(cwd),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        raise ReviewerWorktreeError(failure.summary(), git_failure=failure)
    return proc
