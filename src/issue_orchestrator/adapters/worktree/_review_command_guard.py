"""Installs the review-exchange worktree's technical refusal of gate commands.

The reviewer worktree is deliberately created without the repository's runtime
prerequisites (``execution/reviewer_worktree``,
``docs/architecture/validation.md``). Telling the reviewer not to run gates
there is an explanation, not an enforcement: ``docs/architecture/hooks.md``
rules that prompts are suggestions and hooks are the barrier. This module
installs the barrier.

**A guard is a provider's mechanism, so the installer is keyed on the provider
that will actually sit in the worktree.** A reviewer running under Codex never
reads ``.claude/settings.local.json``, and a Claude reviewer never loads
``.codex/rules``; writing the wrong one anyway would produce a worktree that is
*apparently* guarded and actually unguarded, which is worse than being told
there is no guard. So each guardable provider has a **registration** here, and
:data:`GUARDABLE_PROVIDERS` is derived from that table rather than declared
beside it — naming a provider that has no registration is not something this
module can express, which is the point (#396). A provider with no registration
gets no file at all and an outcome whose ``guarded`` is ``False``.

**Claude Code — a pinned ``PreToolUse`` hook.** Two properties make it
trustworthy:

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

**Codex — a worktree-local exec policy, verified before it is claimed.** Codex
resolves a linked worktree as its own project root (its ``.git`` is a file), so
``<reviewer worktree>/.codex/rules/`` is a policy surface only this reviewer
reads, loaded under the existing #215 trust grant, which names the *common*
repository root — the product checkout, already approved. No new trust root, no
``~/.codex`` mutation, and no provisioning of the reviewer worktree. The
mechanism, its composition with the shipped ``orchestrator.rules`` safety
policy, and its verification through ``codex execpolicy check`` belong to
:mod:`._codex_gate_policy`, which the planning guard (#289) proved and uses
too; what this module owns is the reviewer's refusal prose and the samples a
reviewer must keep. Establishment that cannot be verified raises
:class:`WorktreeError`, and the reviewer worktree is rolled back rather than
handed over as guarded.

The third thing this module owes its caller is an honest answer:
:class:`ReviewCommandGuardOutcome` reports what was actually established —
``guarded`` is a fact the caller must handle, not a value it can assume, and
``probes`` carries the classifications a mechanism that can be measured gave
before the guard was called established.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.artifact_contracts import AgentProvider
from ...infra.hooks.review_command_guard import (
    GUARD_MODULE,
    REFUSAL_REASON,
    orchestrator_source_root,
)
from ...ports.command_guard import GuardProbe
from ..hooks.codex_execpolicy import CodexCliExecPolicy, ExecPolicyChecker
from ._codex_gate_policy import (
    CODEX_SAFETY_RULES,
    CodexGatePolicy,
    CodexGatePolicyError,
)
from ._worktree_errors import WorktreeError
from ._worktree_runtime import _write_worktree_exclude_entries

logger = logging.getLogger(__name__)

__all__ = [
    "GUARDABLE_PROVIDERS",
    "REVIEW_COMMAND_GUARD_PATHS",
    "REVIEW_COMMAND_GUARD_SETTINGS",
    "REVIEW_GUARD_ALLOWED_SAMPLES",
    "REVIEW_GUARD_REFUSED_SAMPLES",
    "REVIEW_GUARD_RULES",
    "ReviewCommandGuardOutcome",
    "install_review_command_guard",
    "render_review_rules",
    "review_command_guard_command",
]

#: Worktree-relative settings file the Claude Code guard is registered in.
REVIEW_COMMAND_GUARD_SETTINGS = Path(".claude") / "settings.local.json"

#: Worktree-relative policy file the Codex registration owns outright.
REVIEW_GUARD_RULES = Path(".codex") / "rules" / "review-gate.rules"

#: Every worktree-relative path a reviewer guard registration can plant.
#:
#: The reviewer worktree's owner has to lift these before ``git worktree
#: remove`` (untracked files block it), and asking this module which paths it
#: writes is how that list cannot fall behind a new registration.
REVIEW_COMMAND_GUARD_PATHS: tuple[Path, ...] = (
    REVIEW_COMMAND_GUARD_SETTINGS,
    REVIEW_GUARD_RULES,
    CODEX_SAFETY_RULES,
)

#: Gate commands whose refusal is verified before a measurable guard is
#: reported as established. Pinned rather than sampled from the vocabulary they
#: check: a probe set derived from the same table would agree with itself even
#: after the table stopped meaning anything (#396 F2).
REVIEW_GUARD_REFUSED_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("make", "validate-pr-raw"),
    ("pytest", "-q", "tests/unit"),
    ("python", "-m", "pytest"),
)

#: What a reviewer must keep. Verified in the same pass, so a policy that
#: refuses by refusing everything fails to establish: the reviewer reads the
#: candidate's code, and a guard that turned reviewing into a no-tools role
#: would be a worse outcome than the gate it prevents.
#:
#: The last entry is *this* principal's way out. The only caller of
#: ``create_reviewer_worktree`` is the review exchange
#: (``execution/persistent_review_exchange_runner``), where a verdict is
#: recorded with ``exchange-respond`` and ``reviewer-done`` is forbidden
#: outright (``resources/review_exchange_reviewer.md``). A refusal here is the
#: one that would deadlock the round — the reviewer could not answer, the turn
#: mailbox would never receive, and the exchange would time out — so it is the
#: command that has to be put to the checker. Pinning the standalone lane's
#: exit instead would leave that failure unmeasured.
REVIEW_GUARD_ALLOWED_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("git", "log", "--oneline", "-20"),
    ("rg", "-n", "install_review_command_guard", "src"),
    ("cat", "AGENTS.md"),
    ("exchange-respond", "ok", "--getting-closer", "--text", "reads clean"),
)

_REVIEW_RULES_HEADER = """\
# Gate-command refusal for one issue-orchestrator review-exchange reviewer.
#
# Generated by adapters/worktree/_review_command_guard.py from the single
# gate-command vocabulary in infra/hooks/gate_commands.py -- the same list the
# Claude reviewer's PreToolUse hook renders as command regexes. This file lives
# in the reviewer worktree only: the product checkout's .codex/rules and the
# operator's ~/.codex are untouched, and the file goes away with the exchange.
# The one thing that outlives it is the repository's shared .git/info/exclude,
# which gains a line naming this path so the policy stays out of git status; it
# is written once and not removed. Editing this file by hand has no effect on
# the next exchange.
"""

#: The reviewer's half of the shared Codex policy mechanism. The refusal prose
#: is the reviewer guard's own :data:`REFUSAL_REASON` — the same explanation the
#: Claude hook prints — so a reviewer meets one reason for one rule whichever
#: provider it is running under.
REVIEW_GATE_POLICY = CodexGatePolicy(
    name="reviewer command guard",
    rules_path=REVIEW_GUARD_RULES,
    header=_REVIEW_RULES_HEADER,
    justification=REFUSAL_REASON,
    refused_samples=REVIEW_GUARD_REFUSED_SAMPLES,
    allowed_samples=REVIEW_GUARD_ALLOWED_SAMPLES,
)


@dataclass(frozen=True)
class ReviewCommandGuardOutcome:
    """What the installer did for one reviewer worktree.

    ``guarded`` is the fact callers have to branch on. It exists as a returned
    value rather than an assumed post-condition because the alternative — a
    ``Path`` for every provider — is what let a Claude-shaped settings file
    stand in for enforcement on providers that never read it.

    ``probes`` is empty for a mechanism whose enforcement is not established by
    classifying samples (the Claude hook runs the orchestrator's own pinned
    policy module), and carries the measured classifications for one that is
    (the Codex exec policy, whose file is data a checker must be asked about).
    """

    provider: AgentProvider
    policy_file: Path | None
    probes: tuple[GuardProbe, ...] = ()

    @property
    def guarded(self) -> bool:
        return self.policy_file is not None

    def refusals(self) -> tuple[str, ...]:
        return tuple(probe.label for probe in self.probes if probe.refused)

    def allowances(self) -> tuple[str, ...]:
        return tuple(probe.label for probe in self.probes if not probe.refused)


#: What one provider's registration returns: the file it wrote, and whatever
#: the enforcing mechanism answered while it was being verified.
_Registration = tuple[Path, tuple[GuardProbe, ...]]

_BASH_MATCHER = "Bash"


def render_review_rules() -> str:
    """Render the reviewer's Codex policy file, deterministically."""
    return REVIEW_GATE_POLICY.render()


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


def _register_claude_pre_tool_use_hook(
    worktree_path: Path, execpolicy: ExecPolicyChecker
) -> _Registration:
    """Register the guard in Claude Code's never-tracked local settings layer.

    ``execpolicy`` is unused: what enforces here is the orchestrator's own
    policy module, named by a pinned command, not a data file some checker has
    to be asked about.
    """
    del execpolicy
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
    return settings_file, ()


def _register_codex_exec_policy(
    worktree_path: Path, execpolicy: ExecPolicyChecker
) -> _Registration:
    """Establish, and verify, the reviewer's worktree-local Codex exec policy.

    Nothing here is reported until the enforcing mechanism has classified the
    pinned samples: an unverifiable policy is not a barrier, and a reviewer
    worktree that cannot get one must not be handed over as guarded.
    """
    try:
        established = REVIEW_GATE_POLICY.establish(
            worktree_path, execpolicy=execpolicy
        )
    except CodexGatePolicyError as exc:
        raise WorktreeError(str(exc)) from exc
    return established.policy_file, established.probes


#: The registrations this installer can actually make, by provider.
#:
#: The *policy* was already provider-ready — ``review_command_guard.main``
#: speaks ``--mode {claude,cursor,gemini,copilot}`` and
#: ``gate_commands.codex_argv_patterns`` renders the argv dialect. What is
#: provider-specific is the *registration*: where the policy is written and
#: what proves the provider loads it. Widening this table means writing that
#: provider's registration and measuring the provider against it, which is
#: exactly the work adding a name to a set would skip.
_REGISTRATIONS: Mapping[str, Callable[[Path, ExecPolicyChecker], _Registration]] = {
    "claude-code": _register_claude_pre_tool_use_hook,
    "codex": _register_codex_exec_policy,
}

#: Configured providers this installer can actually register the guard with.
#:
#: Derived from :data:`_REGISTRATIONS` rather than written out, so the set
#: cannot claim a provider that has no registration behind it.
GUARDABLE_PROVIDERS: frozenset[str] = frozenset(_REGISTRATIONS)


def install_review_command_guard(
    worktree_path: Path,
    *,
    provider: AgentProvider,
    execpolicy: ExecPolicyChecker | None = None,
) -> ReviewCommandGuardOutcome:
    """Register the gate-command refusal in ``worktree_path`` if it can be.

    ``provider`` is the provider that will actually run in this worktree, as
    the exchange resolves it for launch. It is required — not defaulted —
    because a default would silently re-open exactly the hole it closes.

    ``execpolicy`` answers the exec-policy questions the Codex registration
    asks. It defaults to the installed Codex CLI, the only authority on its own
    rules; it is injectable so verification can be measured at the port
    boundary the hook gate already uses.

    A provider outside :data:`GUARDABLE_PROVIDERS` gets **no file written** and
    an outcome whose ``guarded`` is ``False``: this installer will not leave a
    guard-shaped file in a worktree whose agent cannot read it, and it will not
    let the caller mistake that file for a barrier.

    Raises:
        WorktreeError: a guard this installer *can* register could not be
            written, or could not be verified as refusing. A guardable provider
            left unguarded is a worktree the caller must not proceed with.
    """
    register = _REGISTRATIONS.get(provider.value)
    if register is None:
        logger.warning(
            "Reviewer worktree is UNGUARDED: no gate-command guard mechanism is "
            "implemented for provider %s (guardable: %s). Nothing but the "
            "reviewer prompt's note stops a build/test/validation command in "
            "%s — see docs/architecture/validation.md.",
            provider.value,
            ", ".join(sorted(GUARDABLE_PROVIDERS)),
            worktree_path,
        )
        return ReviewCommandGuardOutcome(provider=provider, policy_file=None)
    policy_file, probes = register(
        Path(worktree_path),
        CodexCliExecPolicy() if execpolicy is None else execpolicy,
    )
    logger.debug(
        "Installed reviewer command guard at %s for provider %s",
        policy_file,
        provider.value,
    )
    return ReviewCommandGuardOutcome(
        provider=provider, policy_file=policy_file, probes=probes
    )
