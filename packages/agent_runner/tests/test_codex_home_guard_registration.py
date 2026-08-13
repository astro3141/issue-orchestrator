"""The Codex-home guard has to be live in *this* test root.

``pyproject.toml`` declares two testpaths - ``tests`` and this one - and
``conftest.py`` is directory-scoped, so fixtures registered under ``tests/``
never reach here.  ``tests/codex_home.py`` claims a newly added live test
cannot leak by omission; for this root that claim rests entirely on the
registration living in the repository-root ``conftest.py``, above both.

Nothing under ``packages/agent_runner`` spawns Codex today.  But
``agent_runner.runner`` spawns processes, so a live test added beside it is the
plausible next step, and it would leak silently - the failure this guard exists
to close.  Only a test living in this root can show the guard is active here;
inspecting the other root cannot.

These tests assert that wiring, so they need the repository-root ``conftest.py``
to be loaded - which means a run rooted at the repository, the way the unit lane
invokes it (``pytest tests/unit packages/agent_runner/tests``).  Point pytest at
this subtree alone and ``packages/agent_runner/pyproject.toml`` wins the rootdir
search with its own ``[tool.pytest.ini_options]``, putting the repository-root
``conftest.py`` above ``confcutdir``; these tests then fail rather than pretend.

They are safe by construction either way: each leaking spawn passes an explicit
``env`` with no ``PATH``, so an unguarded run resolves ``codex`` against the
default path and finds nothing.  The guard is what should stop them; the
operating system is the backstop that keeps a broken guard from reaching the
real CLI.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import subprocess

import pytest

from tests.codex_home import CODEX_HOME_ENV, CODEX_HOME_POLICY


def _assert_guard_refuses(command: Sequence[str], env: Mapping[str, str]) -> None:
    """Require the guard - not the operating system - to stop a leaking spawn.

    Without the guard installed the spawn reaches the OS, and the resulting
    ``FileNotFoundError`` says nothing about why. Translate it into the failure
    this test is actually about.
    """
    try:
        with pytest.raises(AssertionError, match="Codex home leak"):
            subprocess.Popen(command, env=dict(env))
    except OSError:
        pytest.fail(
            "the Codex-home guard is not installed in this test root: the spawn "
            "reached the operating system instead of being refused. The fixtures "
            "register in the repository-root conftest.py, which a run rooted at "
            "packages/agent_runner does not load."
        )


class TestCodexHomeGuardRegistration:
    """Both halves of the isolation - the redirect and the guard - reach here."""

    def test_session_redirect_applies_to_this_root(self) -> None:
        """``codex_home_session`` moved ``CODEX_HOME`` off the operator's home."""
        assert CODEX_HOME_POLICY.describe_leak(os.environ) is None

    def test_guard_refuses_a_codex_spawn_with_no_codex_home(self) -> None:
        """A spawn built with an explicit env that dropped ``CODEX_HOME``."""
        _assert_guard_refuses(["codex", "exec", "hello"], {})

    def test_guard_refuses_a_codex_spawn_pointed_at_the_operator_home(self) -> None:
        """The other leak shape: a home that resolves inside a protected one."""
        _assert_guard_refuses(
            ["codex", "exec", "hello"],
            {CODEX_HOME_ENV: str(CODEX_HOME_POLICY.operator_home)},
        )
