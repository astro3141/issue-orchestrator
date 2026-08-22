"""Lightweight probes for live agent CLI availability in tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import cache


LIVE_PROVIDER_PROBES = ("is_claude_authenticated",)
"""Helpers here that make a **real** provider round trip when called.

Blocking validation deselects `live_agent` after collection, so it imports
every module under ``tests/integration`` regardless. Calling one of these while
a module is being imported therefore puts a live provider call — in every xdist
worker — inside the publish gate.
``tests/unit/test_makefile_validation_phases.py`` states that as a guardrail,
and reads this tuple rather than hard-coding names, so a new probe helper is
covered by adding it here.

``is_claude_available`` is deliberately absent: it is a ``shutil.which`` lookup
and contacts nothing.
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
