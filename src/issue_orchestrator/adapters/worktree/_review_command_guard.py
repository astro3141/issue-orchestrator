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

Both properties are properties of *Claude Code's* hook mechanism, so the third
thing this module owes its caller is an honest answer about the provider that
will actually sit in the worktree. A reviewer running under Codex or Cursor
never reads ``.claude/settings.local.json``; writing it anyway would produce a
worktree that is *apparently* guarded and actually unguarded, which is worse
than being told there is no guard. So the installation is keyed on the
provider (:data:`GUARDABLE_PROVIDERS`), it writes nothing for a provider it
cannot register with, and it reports what it did in
:class:`ReviewCommandGuardOutcome` — ``guarded`` is a fact the caller must
handle, not a value it can assume.

**Known gap.** Codex is not guardable today, and this repository's default mode
configures a Codex reviewer (``.issue-orchestrator/config/modes/default/
main.yaml``). Codex does have a project-local exec-policy mechanism
(``.codex``, ``prefix_rule``/``execpolicy``, see ``adapters/hooks/codex.py``),
but the CLI disables project-local config, hooks and exec policies "until the
project is trusted" — and a reviewer worktree is a brand-new directory nothing
has trusted, so a rules file planted there would be exactly the decorative
guard this module refuses to write. Closing that needs the trust step (or
provisioning the worktree instead of exempting it); until then a Codex reviewer
is protected by ``REVIEWER_WORKTREE_IS_UNPROVISIONED_NOTE`` alone and this
module says so out loud instead of pretending otherwise.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.artifact_contracts import AgentProvider
from ...infra.hooks.review_command_guard import GUARD_MODULE, orchestrator_source_root
from ._worktree_errors import WorktreeError
from ._worktree_runtime import _write_worktree_exclude_entries

logger = logging.getLogger(__name__)

__all__ = [
    "GUARDABLE_PROVIDERS",
    "REVIEW_COMMAND_GUARD_SETTINGS",
    "ReviewCommandGuardOutcome",
    "install_review_command_guard",
    "review_command_guard_command",
]

#: Worktree-relative settings file the guard is registered in.
REVIEW_COMMAND_GUARD_SETTINGS = Path(".claude") / "settings.local.json"

#: Configured providers this installer can actually register the guard with.
#:
#: The *policy* is already provider-ready — ``review_command_guard.main``
#: speaks ``--mode {claude,cursor,gemini,copilot}``. What is Claude-specific
#: is the *registration*: a ``PreToolUse`` entry in
#: ``.claude/settings.local.json``. A reviewer that reads a different file
#: (Codex's ``.codex``, Cursor's ``hooks.json``) would never load this hook, so
#: this set — not the policy's modes — is the honest answer to "can this
#: worktree be guarded". Widening it means writing that provider's
#: registration and proving the provider loads it, not editing this line.
GUARDABLE_PROVIDERS: frozenset[str] = frozenset({"claude-code"})


@dataclass(frozen=True)
class ReviewCommandGuardOutcome:
    """What the installer did for one reviewer worktree.

    ``guarded`` is the fact callers have to branch on. It exists as a returned
    value rather than an assumed post-condition because the alternative — a
    ``Path`` for every provider — is what let a Claude-shaped settings file
    stand in for enforcement on providers that never read it.
    """

    provider: AgentProvider
    settings_file: Path | None

    @property
    def guarded(self) -> bool:
        return self.settings_file is not None

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

    Deliberately *not* the same choice as ``_worktree_runtime``'s sibling
    reader for ``.claude/settings.json``, which fails worktree setup on an
    unreadable file. That file is the repository's own tracked settings, where
    replacing operator content would be destructive; this one is a local layer
    the orchestrator writes and owns outright. The rule is "who owns the file",
    not "how we read settings files here".
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


def install_review_command_guard(
    worktree_path: Path, *, provider: AgentProvider
) -> ReviewCommandGuardOutcome:
    """Register the gate-command refusal in ``worktree_path`` if it can be.

    ``provider`` is the provider that will actually run in this worktree, as
    the exchange resolves it for launch. It is required — not defaulted —
    because a default would silently re-open exactly the hole it closes.

    A provider outside :data:`GUARDABLE_PROVIDERS` gets **no file written** and
    an outcome whose ``guarded`` is ``False``: this installer will not leave a
    Claude-shaped settings file in a worktree whose agent cannot read it, and
    it will not let the caller mistake that file for a barrier.

    Raises:
        WorktreeError: a guard this installer *can* write could not be
            written. A guardable provider left unguarded by an I/O failure is
            a worktree the caller must not proceed with.
    """
    if provider.value not in GUARDABLE_PROVIDERS:
        logger.warning(
            "Reviewer worktree is UNGUARDED: no gate-command guard mechanism is "
            "implemented for provider %s (guardable: %s). Nothing but the "
            "reviewer prompt's note stops a build/test/validation command in "
            "%s — see docs/architecture/validation.md.",
            provider.value,
            ", ".join(sorted(GUARDABLE_PROVIDERS)),
            worktree_path,
        )
        return ReviewCommandGuardOutcome(provider=provider, settings_file=None)
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
    return ReviewCommandGuardOutcome(provider=provider, settings_file=settings_file)
