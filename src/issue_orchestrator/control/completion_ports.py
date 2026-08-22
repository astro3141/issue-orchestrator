"""Adapter protocols used by completion processing, and their refusals.

The null objects at the end are the defaults for the two ports a completion
processor may be constructed without. They live beside the protocols they
stand in for, and they refuse rather than no-op: a deployment that forgot to
wire one should say so at the first call, not quietly process a completion
whose evidence nothing read.
"""

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..ports.pull_request_tracker import PRInfo
from ..ports.review_artifact_reader import ReviewArtifactReadCommand
from ..ports.working_copy import (
    BranchPathsResult,
    BranchTextFilesResult,
    DiffResult,
    PushResult,
    RebaseResult,
)


@runtime_checkable
class LabelAdapter(Protocol):
    """Protocol for label operations."""

    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...


@runtime_checkable
class PRAdapter(Protocol):
    """Protocol for PR operations."""

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None
    ) -> PRInfo: ...
    def add_comment(self, issue_or_pr_number: int, body: str) -> str: ...
    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]: ...
    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]: ...
    def set_pr_base(self, pr_number: int, base: str) -> None: ...


@runtime_checkable
class GitAdapter(Protocol):
    """Protocol for git operations."""

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult: ...

    def rebase_on_branch(self, worktree: Path, target: str = "origin/main") -> RebaseResult: ...
    def create_branch_from_current(self, worktree: Path, branch: str) -> None: ...
    def list_branch_names(self, worktree: Path) -> list[str]: ...
    def get_current_branch(self, worktree: Path) -> str | None: ...
    def get_head_sha(self, worktree: Path) -> str | None: ...
    def has_uncommitted_changes(self, worktree: Path) -> bool: ...
    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool: ...
    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None: ...
    def diff_against_base(self, worktree: Path, base_ref: str) -> DiffResult: ...
    def read_branch_text_files(
        self, worktree: Path, paths: tuple[str, ...]
    ) -> BranchTextFilesResult: ...
    def branch_post_image_paths_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchPathsResult: ...
    def default_branch(self, repo_root: Path, remote: str = "origin") -> str: ...


class MissingReviewArtifactReader:
    """Fail-fast default for an unwired review-artifact reader."""

    def read_review_artifact(self, command: ReviewArtifactReadCommand) -> Any:
        raise RuntimeError(
            "CompletionProcessor requires review_artifact_reader to read "
            f"review artifacts for issue #{command.issue_number}"
        )


class MissingTechLeadAuthorityStore:
    """Fail-fast default: tech_lead completions require the wired port.

    Production always injects the SQLite-backed store from bootstrap; a test
    that exercises a tech_lead completion without wiring the port surfaces the
    misconfiguration immediately instead of silently fail-safing.
    """

    def _fail(self) -> Any:
        raise RuntimeError(
            "CompletionProcessor requires tech_lead_authority to process a "
            "tech_lead session completion (wired in entrypoints/bootstrap.py)"
        )

    def record(self, *, run_id: str, session_name: str, authority: Any) -> None:
        self._fail()

    def load(self, *, run_id: str, session_name: str) -> Any:
        self._fail()

    def discard(self, *, run_id: str, session_name: str) -> None:
        self._fail()
