"""Lightweight probes for live agent CLI availability in tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import cache


LIVE_PROVIDER_PROBES = ("is_claude_authenticated", "is_codex_authenticated")
"""Helpers here that make a **real** provider round trip when called.

Blocking validation deselects `live_agent` and `live_codex` after collection,
so it imports every module under ``tests/integration`` regardless. Calling one
of these while a module is being imported therefore puts a live provider call —
in every xdist worker — inside the publish gate.
``tests/unit/test_makefile_validation_phases.py`` states that as a guardrail,
and reads this tuple rather than hard-coding names, so a new probe helper is
covered by adding it here.

The registry is per **provider**, not per lane (#227). Both markers name lanes
with the same invariant, so a probe written as a private module function in the
test that needs it is structurally invisible to the guardrail — which is how a
module-scope ``codex login status`` sat inside the publish gate while the rule
forbidding exactly that read green. A new provider gets a probe here, not a
local helper.

``is_claude_available`` and ``is_codex_available`` are deliberately absent: they
are ``shutil.which`` lookups and contact nothing.
"""


def is_claude_available() -> bool:
    """Return whether the Claude CLI is available in PATH."""
    return shutil.which("claude") is not None


@cache
def is_claude_authenticated() -> bool:
    """Return whether the Claude CLI can run a minimal prompt.

    This is a live provider probe, so call it from live-agent test bodies or
    live-agent lanes, not broad e2e collection. Scrub ``CLAUDECODE`` so the
    probe works when tests run inside a Claude Code session.
    """
    if not is_claude_available():
        return False
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "Reply with OK"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def is_codex_available() -> bool:
    """Return whether the Codex CLI is available in PATH."""
    return shutil.which("codex") is not None


@cache
def is_codex_authenticated() -> bool:
    """Return whether the Codex CLI is installed and logged in.

    ``codex login status`` exits 0 when logged in; a missing binary, a logged
    out CLI, or a network-down auth check all answer ``False``.

    This spawns the real Codex CLI, so it is a live provider probe: call it
    from a `live_codex` test's fixture, never at module scope. At module scope
    it runs during collection — before ``tests/codex_home.py``'s autouse
    isolation fixtures, which are session/function scoped and therefore only
    reach test setup — so the spawn would hit the operator's live ``~/.codex``,
    which ``tests/CLAUDE.md`` forbids outright.
    """
    if not is_codex_available():
        return False
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False
