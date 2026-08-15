"""Owner for the runtime setup sequence applied to orchestrator worktrees.

``_worktree.py`` owns worktree *lifecycle* decisions — reuse this path, rebase
that branch, recreate when validation fails. It must not also own what "ready
for an agent session" means. This module does: it holds the setup steps, their
order, and the failure semantics of each, so a policy change lands in exactly
one place instead of drifting across the three lifecycle paths (fresh create,
reuse by branch, reuse by path) that all need identical setup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ._worktree_errors import WorktreeError
from ._worktree_hooks import install_hooks
from ._worktree_runtime import (
    _configure_no_verify_dry_run,
    _hide_runtime_artifacts_from_git_status,
    install_worktree_identity,
    install_claude_settings,
    sync_cli_tools,
    unhide_repo_owned_cli_tools,
)
from ._worktree_venv import ensure_worktree_owns_its_venv

logger = logging.getLogger(__name__)

__all__ = ["WorktreeRuntimeSetup", "WorktreeRuntimeState"]


@dataclass(frozen=True)
class WorktreeRuntimeState:
    """What runtime setup actually put in place for one worktree.

    Returned so callers can observe the outcome (and tests can assert on it)
    without re-deriving it from the filesystem or from the setup inputs. Every
    field is an observed result: a state object that echoed the request back
    would be a more convincing lie than no state object at all.

    Args:
        worktree_path: The worktree the setup was applied to.
        worktree_id: Identity persisted in the worktree, existing or new.
        hooks_installed: Whether the orchestrator pre-push guardrail is now
            installed. False only when hooks were not requested — a requested
            hook that did not install fails ``apply`` instead of being reported.
        no_verify_dry_run_allowed: State the ``--no-verify`` dry-run flag file
            was left in.
        synced_cli_tool_paths: Worktree-relative paths of the CLI tools copied
            in, in the form the git exclude entries use.
    """

    worktree_path: Path
    worktree_id: str
    hooks_installed: bool
    no_verify_dry_run_allowed: bool
    synced_cli_tool_paths: tuple[Path, ...]


@dataclass(frozen=True)
class WorktreeRuntimeSetup:
    """Composes the runtime setup a worktree needs before an agent runs in it.

    Built once per ``create_worktree`` call from the caller's hook and preflight
    options, then applied to whichever worktree the lifecycle settles on.

    Args:
        enforce_hooks: Whether guardrail git hooks are installed into the worktree.
        pre_push_hook: Custom pre-push hook to install instead of the bundled one.
        allow_no_verify_dry_run_preflight: Whether the worktree may use
            ``--no-verify`` for the reuse push preflight.
    """

    enforce_hooks: bool = True
    pre_push_hook: Path | None = None
    allow_no_verify_dry_run_preflight: bool = False

    def preflight_reuse(self, worktree_path: Path) -> None:
        """Clear a reused worktree for the destructive steps reuse is about to run.

        Reuse rebases, hard-resets and cleans before setup ever runs, so a
        worktree hiding repo-owned CLI source behind ``--skip-worktree`` loses
        that content before ``apply`` gets the chance to notice it — and loses
        it invisibly, because the bit is precisely what stopped git reporting
        the file. Preserving the work inside ``apply`` would then be preserving
        whatever the reset happened to leave.

        So the same question ``apply`` asks is asked here first, ahead of the
        reset: establish provenance, put the paths back under git's eye, and
        either repair what this orchestrator can prove it planted or stop the
        reuse path outright. It is idempotent with the ``sync_cli_tools`` step
        inside ``apply``: by the time that runs, nothing is hidden any more.

        Called once validation has accepted the worktree, which is what makes
        ``git`` answerable there at all — a path that is not a usable worktree
        is deleted by the lifecycle before this point, and has no index to hold
        a ``--skip-worktree`` bit in the first place.

        Raises:
            WorktreeError: If the worktree holds repo-owned CLI source that no
                orchestrator copy explains. The content is preserved and made
                visible to ``git status``; reuse stops rather than resetting
                over work only a human can classify.
        """
        try:
            unhide_repo_owned_cli_tools(worktree_path)
        except WorktreeError:
            raise
        except Exception as exc:
            raise WorktreeError(
                f"Worktree reuse preflight failed for {worktree_path}: {exc}"
            ) from exc

    def apply(self, worktree_path: Path) -> WorktreeRuntimeState:
        """Bring ``worktree_path`` to a runnable state for an agent session.

        Idempotent: a reused worktree runs the same sequence as a fresh one.

        ``WorktreeError`` is the whole failure surface of this owner. The steps
        it composes fail in their own vocabularies (``OSError`` from a write,
        ``RuntimeError`` from a corrupt hook chain); callers hold the owner, not
        the steps, so anything that escapes a step is translated here rather
        than leaking an adapter's internals through a public API.

        Raises:
            WorktreeError: If any step the session depends on cannot complete —
                hook install, Claude settings, the no-verify flag, removing a
                ``.venv`` this worktree cannot claim as its own, worktree
                identity, or artifact hiding. A half-set-up worktree is a worse
                outcome than a failed create the lifecycle can retry or
                recreate from.
        """
        try:
            return self._apply(worktree_path)
        except WorktreeError:
            raise
        except Exception as exc:
            raise WorktreeError(
                f"Worktree runtime setup failed for {worktree_path}: {exc}"
            ) from exc

    def _apply(self, worktree_path: Path) -> WorktreeRuntimeState:
        """Run the setup sequence.

        Artifact hiding runs last because it needs the CLI tool paths the sync
        step planted.
        """
        hooks_installed = False
        if self.enforce_hooks:
            hooks_installed = install_hooks(worktree_path, self.pre_push_hook)
            if not hooks_installed:
                # Enforced hooks are the guardrail that stops an agent pushing
                # past validation. Continuing here would hand back a worktree
                # that looks configured and silently is not.
                raise WorktreeError(
                    "Enforced guardrail hooks were requested but no "
                    f"orchestrator pre-push hook was installed in {worktree_path} "
                    f"(pre_push_hook={self.pre_push_hook or 'bundled'})"
                )
        install_claude_settings(worktree_path)
        _configure_no_verify_dry_run(
            worktree_path, self.allow_no_verify_dry_run_preflight
        )
        ensure_worktree_owns_its_venv(worktree_path)
        synced_cli_tool_paths = list(sync_cli_tools(worktree_path))
        worktree_id = install_worktree_identity(worktree_path)
        _hide_runtime_artifacts_from_git_status(worktree_path, synced_cli_tool_paths)

        logger.debug(
            "Worktree runtime setup applied: path=%s id=%s hooks=%s",
            worktree_path,
            worktree_id,
            hooks_installed,
        )
        return WorktreeRuntimeState(
            worktree_path=worktree_path,
            worktree_id=worktree_id,
            hooks_installed=hooks_installed,
            no_verify_dry_run_allowed=self.allow_no_verify_dry_run_preflight,
            synced_cli_tool_paths=tuple(synced_cli_tool_paths),
        )
