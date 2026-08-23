"""Materialize the approved Codex repository-root trust for one launch (#215).

Codex decides workspace trust *before* the config layers that
``--ask-for-approval`` / ``--sandbox`` live in are assembled, so no approval or
sandbox flag suppresses its trust dialog. An unattended launch in a linked
managed worktree parks on it forever (#204).

This module answers that with a per-launch grant and nothing wider:

1. **Resolve** the Codex *common repository root* for the launch directory.
   Codex keys the grant to the owner of the git **common** directory, not to
   the worktree it runs in — a linked worktree's ``.git`` is a *file*, so a
   "walk up until ``(candidate / '.git').exists()``" root walk (the one
   ``codex_config.py`` uses for project config discovery) stops at the
   worktree and never reaches the trust key. The two are different questions
   and are deliberately answered by different code.
2. **Verify** the resolved root against the operator-approved absolute root.
   Absent, malformed, unreadable, non-git, or mismatched → fail closed before
   spawn, as :class:`WorkspaceTrustError`.
3. **Materialize** it as one root-command ``-c`` override, scoped to the
   launch:

   .. code-block:: text

      -c projects={ "<approved-root>" = { trust_level = "trusted" } }

   **The spelling is load-bearing and was measured, not assumed.** #215
   specified the dotted form ``-c projects."<root>".trust_level="trusted"``.
   Against installed Codex 0.147.0 that form does **not** suppress the dialog:
   the CLI's ``-c key=value`` parser splits the key on ``.`` without honouring
   the quoted segment, so a path — which is full of ``.`` and ``/`` — never
   lands on ``projects.<root>.trust_level``. Assigning the ``projects`` table
   itself does land, and is the form emitted here. Every property #215 chose
   the mechanism for is preserved, and one is tightened: because the override
   replaces the ``projects`` table for this launch, the process trusts the
   approved root and *nothing else*, whatever the user layer says.

   Measured on installed Codex 0.147.0 against an isolated ``CODEX_HOME`` and
   a real linked worktree — no grant: dialog blocks, TUI never reached; this
   grant: dialog absent, TUI reached (also under ``--strict-config``, so the
   key is recognised rather than silently dropped); a grant naming a
   *different* root: dialog blocks again. That is provider evidence for
   0.147.0 and is not generalized to other Codex versions —
   ``tests/integration/test_codex_workspace_trust_live.py`` re-measures it
   rather than trusting this comment.

Why per-launch materialization, and not a persistent bootstrap: it writes
nothing to the operator's ``~/.codex/config.toml`` (a host-global store shared
with the desktop app), the grant's lifetime is the launch, it is visible in the
recorded argv so it stays auditable, and it is scoped exactly to the approved
repository root and cannot be broader. A ``-p`` profile-file bootstrap and an
edit to the user config were both rejected for the opposite properties.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

from issue_orchestrator.domain.sandbox_scope import SandboxUnsupportedError
from issue_orchestrator.domain.workspace_trust import (
    LaunchWorkspace,
    RepositoryTrustGrant,
    WorkspaceTrustError,
)

from .git_worktree_access import resolve_git_common_dir

__all__ = [
    "CODEX_TRUST_MECHANISM",
    "authorize_codex_workspace_trust",
    "codex_trust_override_argv",
    "resolve_codex_common_repository_root",
]

logger = logging.getLogger(__name__)

CODEX_TRUST_MECHANISM: Final[str] = (
    'per-launch root-command -c projects={ "<root>" = { trust_level = "trusted" } }'
)

_GIT_DIR_NAME: Final[str] = ".git"


def resolve_codex_common_repository_root(working_directory: Path) -> Path:
    """Return the repository root Codex keys workspace trust to.

    That is the owner of the git *common* directory: the main checkout, for
    every linked worktree of it. Resolution reuses the one linked-worktree
    pointer reader this repository has (``.git`` file → ``gitdir:`` →
    ``commondir``), so "which git metadata does this worktree belong to" has a
    single implementation, and canonicalizes the result the way Codex does.

    Fails closed — an unreadable, malformed, or non-git working directory, and
    any common directory whose layout does not name a working-tree root, raises
    :class:`WorkspaceTrustError` rather than guessing a root.
    """
    try:
        common_dir = resolve_git_common_dir(working_directory)
    except SandboxUnsupportedError as exc:
        raise WorkspaceTrustError(
            "Cannot resolve the Codex trust root for "
            f"{working_directory}: {exc}"
        ) from exc

    if common_dir.name != _GIT_DIR_NAME:
        raise WorkspaceTrustError(
            "Cannot resolve the Codex trust root for "
            f"{working_directory}: its common Git directory {common_dir} is "
            f"not a working tree's {_GIT_DIR_NAME} directory"
        )
    repository_root = common_dir.parent
    if not repository_root.is_dir():
        raise WorkspaceTrustError(
            "Cannot resolve the Codex trust root for "
            f"{working_directory}: {repository_root} is not a directory"
        )
    return repository_root


def authorize_codex_workspace_trust(
    workspace: LaunchWorkspace | None,
) -> RepositoryTrustGrant:
    """Authorize one Codex launch, or fail closed.

    Raises :class:`WorkspaceTrustError` when the launch declares no workspace
    at all, when it carries no approval, when the working directory's common
    repository root cannot be resolved, and when that root is not the approved
    one. The returned grant exists only for a verified match, and its evidence
    is logged at the moment of the decision so an operator can reconstruct why
    the repository was trusted.

    ``workspace is None`` and ``approved_trust is None`` are the same denial —
    "I could not tell" must never be recorded as "trusted" — so both live here
    rather than being restated by each caller.
    """
    if workspace is None:
        raise WorkspaceTrustError(
            "Refusing to build an interactive Codex launch that declares no "
            "launch workspace: workspace trust cannot be verified, and an "
            "unverified launch parks on Codex's trust dialog"
        )
    approved = workspace.approved_trust
    if approved is None:
        raise WorkspaceTrustError(
            "Refusing to launch Codex interactively in "
            f"{workspace.working_directory}: no approved repository-root trust "
            "is recorded for this launch (set "
            "security.workspace_trust.approved_repository_root to the absolute "
            "repository root a human approved)"
        )
    grant = RepositoryTrustGrant(
        approved=approved,
        resolved_common_root=resolve_codex_common_repository_root(
            workspace.working_directory
        ),
        mechanism=CODEX_TRUST_MECHANISM,
    )
    logger.info(
        "[codex-trust] granted: working_directory=%s %s",
        workspace.working_directory,
        " ".join(f"{key}={value}" for key, value in grant.evidence().items()),
    )
    return grant


def codex_trust_override_argv(grant: RepositoryTrustGrant) -> list[str]:
    """Render *grant* as Codex's root-command config override.

    One ``-c`` pair assigning the whole ``projects`` table, holding exactly one
    entry: the approved root, trusted. See the module docstring for why the
    dotted-key spelling does not work on 0.147.0.

    Emitted exactly once, before any subcommand: #205 established that a
    subcommand-level ``-c`` makes Codex discard the root-level ``-c`` list
    wholesale (which silently dropped the sandbox permission profile), so
    every override this adapter emits shares one position and one owner.
    """
    root = json.dumps(str(grant.repository_root), ensure_ascii=False)
    trusted = json.dumps("trusted", ensure_ascii=False)
    return ["-c", f"projects={{ {root} = {{ trust_level = {trusted} }} }}"]
