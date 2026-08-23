"""Fixtures for the approved repository-root workspace trust (#215).

Every interactive Codex launch must prove it runs inside the repository root a
human approved, so tests that build one need two cheap things: a directory that
*looks* like the git layout the trust resolver reads, and an approval naming its
root. Both are built from plain files — the resolver reads ``.git`` / the
``gitdir:`` pointer / ``commondir`` directly, so no ``git`` subprocess is
involved and the helpers stay usable in the unit suite.

Paths are resolved (``Path.resolve``) because the resolver canonicalizes the
way Codex does, and pytest's ``tmp_path`` lives under a symlinked ``/tmp`` on
macOS; an unresolved expectation would compare two spellings of one directory.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.domain.workspace_trust import (
    ApprovedRepositoryTrust,
    LaunchWorkspace,
    TrustAuthoritySource,
)

__all__ = [
    "APPROVAL_FINGERPRINT",
    "approval_for",
    "approved_workspace",
    "make_linked_worktree",
    "make_repository",
]

# Stand-in for the sha256 of a real authority document. Tests that care about
# the fingerprint's provenance build it from the config loader instead.
APPROVAL_FINGERPRINT = "0" * 64


def make_repository(root: Path) -> Path:
    """Create a main checkout at *root* and return its resolved path."""
    (root / ".git").mkdir(parents=True)
    return root.resolve()


def make_linked_worktree(repository_root: Path, worktree: Path) -> Path:
    """Create a linked worktree of *repository_root* and return its path.

    Reproduces the shape a managed worktree actually has: ``.git`` is a *file*
    pointing at ``<root>/.git/worktrees/<name>``, and that private directory
    carries the ``commondir`` pointer back to the repository's common Git
    directory. A root walk that tests ``(candidate / '.git').exists()`` stops
    here; the trust resolver must not.
    """
    private_dir = repository_root / ".git" / "worktrees" / worktree.name
    private_dir.mkdir(parents=True)
    (private_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (private_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {private_dir}\n", encoding="utf-8")
    return worktree.resolve()


def approval_for(
    repository_root: Path,
    *,
    authority_path: Path | None = None,
    fingerprint: str = APPROVAL_FINGERPRINT,
) -> ApprovedRepositoryTrust:
    """Approve *repository_root*, as an operator's config document would."""
    return ApprovedRepositoryTrust(
        repository_root=repository_root.resolve(),
        source=TrustAuthoritySource(
            path=(authority_path or Path("/approvals/selfhost.yaml")).resolve(),
            fingerprint=fingerprint,
        ),
    )


def approved_workspace(
    working_directory: Path,
    repository_root: Path | None = None,
) -> LaunchWorkspace:
    """A launch workspace whose approval covers *working_directory*.

    ``repository_root`` defaults to the working directory itself (the main
    checkout case); pass it explicitly for a linked worktree, whose approval is
    keyed to the repository root rather than to the worktree.
    """
    return LaunchWorkspace(
        working_directory=working_directory,
        approved_trust=approval_for(repository_root or working_directory),
    )
