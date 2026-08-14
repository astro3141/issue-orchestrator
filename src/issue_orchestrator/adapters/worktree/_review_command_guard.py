"""Installs the review-exchange worktree's technical refusal of gate commands.

The reviewer worktree is deliberately created without the repository's runtime
prerequisites (``execution/reviewer_worktree``,
``docs/architecture/validation.md``). Telling the reviewer not to run gates
there is an explanation, not an enforcement: ``docs/architecture/hooks.md``
rules that prompts are suggestions and hooks are the barrier. This module
installs the barrier.

Two properties make the installed hook trustworthy:

* **Pinned.** The registered command names the orchestrator's own interpreter
  and its own ``src`` root, so the policy that decides is
  ``infra/hooks/review_command_guard.py`` as installed on this machine —
  never a copy the guarded worktree happens to contain.
* **Outside the candidate.** It is written to ``.claude/settings.local.json``,
  Claude Code's local settings layer, which repositories are told to leave
  untracked. Nothing the candidate commit tracks is modified, so the guard
  cannot alter what the reviewer is reading, and it does not collide with the
  checkout the reviewer worktree does between rounds. A repository that
  *tracked* that path would make the per-round checkout fail loudly rather than
  silently drop the guard, which is the right direction for this failure.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

from ...infra.hooks.review_command_guard import GUARD_MODULE, orchestrator_source_root
from ._worktree_errors import WorktreeError
from ._worktree_runtime import _write_worktree_exclude_entries

logger = logging.getLogger(__name__)

__all__ = [
    "REVIEW_COMMAND_GUARD_SETTINGS",
    "install_review_command_guard",
    "review_command_guard_command",
]

#: Worktree-relative settings file the guard is registered in.
REVIEW_COMMAND_GUARD_SETTINGS = Path(".claude") / "settings.local.json"

_BASH_MATCHER = "Bash"


def review_command_guard_command() -> str:
    """The shell command the ``PreToolUse`` hook runs, fully pinned."""
    return (
        f"PYTHONPATH={shlex.quote(str(orchestrator_source_root()))} "
        f"{shlex.quote(sys.executable)} -m {GUARD_MODULE} --mode claude"
    )


def _merge_guard_hook(existing: dict[str, Any] | None) -> dict[str, Any]:
    """Return settings registering the guard exactly once."""
    entry = {"type": "command", "command": review_command_guard_command()}
    settings: dict[str, Any] = dict(existing or {})
    hooks = dict(settings.get("hooks") or {})
    pre_tool_use = list(hooks.get("PreToolUse") or [])

    for index, matcher in enumerate(pre_tool_use):
        if not isinstance(matcher, dict) or matcher.get("matcher") != _BASH_MATCHER:
            continue
        matcher_hooks = [
            hook
            for hook in list(matcher.get("hooks") or [])
            if not (isinstance(hook, dict) and GUARD_MODULE in str(hook.get("command")))
        ]
        matcher_hooks.append(entry)
        pre_tool_use[index] = {**matcher, "hooks": matcher_hooks}
        break
    else:
        pre_tool_use.append({"matcher": _BASH_MATCHER, "hooks": [entry]})

    hooks["PreToolUse"] = pre_tool_use
    settings["hooks"] = hooks
    return settings


def _readable_settings(settings_file: Path) -> dict[str, Any] | None:
    """Return settings safe to merge into, or ``None`` to write a fresh file.

    Content that cannot carry the guard — unreadable bytes, non-JSON text, a
    non-object document — is replaced. This layer is orchestrator-owned and
    never tracked, so there is no operator content to preserve, and a broken
    file must not leave the reviewer worktree without its barrier.
    """
    if not settings_file.exists():
        return None
    try:
        existing = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "Replacing unusable reviewer guard settings: path=%s error=%s",
            settings_file,
            exc,
        )
        return None
    return existing if isinstance(existing, dict) else None


def install_review_command_guard(worktree_path: Path) -> Path:
    """Register the gate-command refusal in ``worktree_path``.

    Returns the settings file written.

    Raises:
        WorktreeError: the guard could not be written. A reviewer worktree
            without it is an unprovisioned worktree with nothing stopping a
            gate command, so the caller must not proceed with it.
    """
    settings_file = Path(worktree_path) / REVIEW_COMMAND_GUARD_SETTINGS
    settings = _merge_guard_hook(_readable_settings(settings_file))
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as exc:
        raise WorktreeError(
            f"Failed to install reviewer command guard at {settings_file}: {exc}"
        ) from exc
    _write_worktree_exclude_entries(worktree_path, [REVIEW_COMMAND_GUARD_SETTINGS])
    logger.debug("Installed reviewer command guard at %s", settings_file)
    return settings_file
