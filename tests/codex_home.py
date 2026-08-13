"""Keep real Codex CLI runs out of the operator's Codex home.

Codex writes a rollout transcript under ``$CODEX_HOME/sessions`` for every
session it runs, and ``~/.codex`` is a *live* directory the operator's desktop
app owns.  A test that spawns the real CLI while ``CODEX_HOME`` still points
there accumulates transcripts in a personal directory forever.

Two mechanisms live here, and they are deliberately different in kind:

``codex_home_session``
    Session-scoped, autouse for every test the run collects.  Isolation is what
    happens by default, not what a test opts into.  Per-test ``usefixtures``
    is exactly what failed before: the fixture existed and simply was not
    applied to two of the six files that reach for the codex binary.

``codex_home_guard``
    Function-scoped, autouse.  Wraps ``subprocess.Popen`` and
    ``pexpect.spawn`` - the only two spawn primitives this repository uses -
    and, when the command being started is the codex binary, asserts on the
    environment that spawn would hand to it.  It checks the effective env, not
    whether a fixture was listed, so a newly added live test cannot leak by
    omission even if the session default is later broken.

Three boundaries are worth knowing before trusting the guard:

*It is registered by the repository-root ``conftest.py``.*  ``conftest.py`` is
directory-scoped and ``pyproject.toml`` declares two testpaths (``tests`` and
``packages/agent_runner/tests``), so registering under ``tests/`` would leave
the second root unguarded.  The root conftest is the one ancestor both share,
which is what makes "cannot leak by omission" true of every testpath rather
than of one tree;
``packages/agent_runner/tests/test_codex_home_guard_registration.py`` proves it
from inside the second root, since no test under ``tests/`` can.  The boundary
is the rootdir: point pytest *only* at ``packages/agent_runner`` and its own
``[tool.pytest.ini_options]`` wins the rootdir search, which puts this
registration above ``confcutdir`` and leaves that run unguarded.  The unit lane
names both roots in one invocation, so the repository stays the rootdir and the
registration holds.

*It wraps module attributes, not the OS.*  ``subprocess.run`` and asyncio's
subprocess transports go through ``subprocess.Popen`` as a module global, so
those are covered.  What is not: a direct
``os.posix_spawn``/``pty.fork``/``ptyprocess`` call, a
``from subprocess import Popen`` binding taken before the patch, and
``pexpect.run`` - which reaches ``spawn`` through its own
``from .pty_spawn import spawn`` rather than the ``pexpect`` package attribute
patched here.  None exist in this repository today; adding one means extending
this module.

*It is single-level.*  The guard sees the process pytest starts, not that
process's children.  A spawn whose own command carries no ``codex`` word and
which passes an explicit ``env=`` is not inspected, so a codex run launched
through an intermediary that owns its own environment - a tmux server is the
concrete shape - is out of reach.  Nothing on the canonical gate's path spawns
codex that way.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import inspect
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Any, Final

import pexpect
import pytest

__all__ = [
    "CODEX_HOME_ENV",
    "CODEX_HOME_POLICY",
    "CodexHomePolicy",
    "codex_home_guard",
    "codex_home_session",
    "isolated_codex_home",
    "provision_codex_home",
    "spawns_codex",
]

CODEX_HOME_ENV: Final[str] = "CODEX_HOME"


@dataclass(frozen=True, slots=True)
class CodexHomePolicy:
    """Owns the single rule: no spawned Codex may write into a protected home.

    ``operator_home`` is the Codex home the *operator* owns - resolved once,
    from the environment as it was before any fixture touched it, so the rule
    keeps its meaning after the session default has been installed.  It is also
    what an isolated home is seeded from.

    ``extra_protected_homes`` carries the homes that are off limits without
    being the seed source.  The account default ``~/.codex`` goes here when
    ``CODEX_HOME`` points somewhere else: exporting ``CODEX_HOME=~/.codex/ci``
    must not stop ``~/.codex`` itself from being protected.
    """

    operator_home: Path
    extra_protected_homes: tuple[Path, ...] = ()

    @classmethod
    def for_environment(cls, env: Mapping[str, str]) -> CodexHomePolicy:
        """Build the policy *env* implies, mirroring Codex's own resolution.

        ``$CODEX_HOME`` wins when set, exactly as ``resolve_codex_home`` does,
        and the account default is protected either way.
        """
        account_home = (Path.home() / ".codex").resolve()
        configured = env.get(CODEX_HOME_ENV)
        if not configured:
            return cls(account_home)
        operator_home = Path(configured).expanduser().resolve()
        if operator_home == account_home:
            return cls(operator_home)
        return cls(operator_home, (account_home,))

    @property
    def protected_homes(self) -> tuple[Path, ...]:
        """Every Codex home a spawned CLI must stay out of."""
        return (self.operator_home, *self.extra_protected_homes)

    def selected_home(self, env: Mapping[str, str]) -> Path | None:
        """Return the Codex home *env* selects, or ``None`` if it selects none.

        Mirrors ``codex_config.resolve_codex_home`` for an arbitrary mapping
        instead of the current process environment.
        """
        raw = env.get(CODEX_HOME_ENV)
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    def describe_leak(self, env: Mapping[str, str]) -> str | None:
        """Describe how *env* would leak Codex state, or ``None`` when safe."""
        home = self.selected_home(env)
        if home is None:
            homes = ", ".join(str(path) for path in self.protected_homes)
            return (
                f"{CODEX_HOME_ENV} is unset, so Codex would write its sessions "
                f"to the operator's home ({homes})"
            )
        for protected in self.protected_homes:
            if home == protected or protected in home.parents:
                return (
                    f"{CODEX_HOME_ENV}={home} resolves inside the operator's "
                    f"Codex home {protected}"
                )
        return None

    def enforce(self, env: Mapping[str, str], *, spawning: str) -> None:
        """Fail the test when *env* would send a real Codex run into the operator's home."""
        leak = self.describe_leak(env)
        if leak is None:
            return
        protected = ", ".join(str(path) for path in self.protected_homes)
        raise AssertionError(
            f"Codex home leak: {spawning} would run with a leaking environment "
            f"({leak}).\n"
            f"Every test that spawns the real Codex CLI must run against an "
            f"isolated {CODEX_HOME_ENV}. Every collected test gets one by "
            f"default from the autouse 'codex_home_session' fixture; a test "
            f"that needs its own pristine home requests 'isolated_codex_home'. "
            f"Do not point {CODEX_HOME_ENV} back at {protected}."
        )


# Resolved at import time - that is, while ``CODEX_HOME`` still holds whatever
# the operator (or CI) exported - so the guard keeps comparing against the real
# home rather than the session's throwaway one.
CODEX_HOME_POLICY: Final[CodexHomePolicy] = CodexHomePolicy.for_environment(os.environ)


def provision_codex_home(destination: Path, *, source: Path) -> Path:
    """Create an isolated Codex home at *destination* seeded from *source*.

    Only ``auth.json`` is copied: live tests must stay authenticated, but the
    operator's ``config.toml`` (models, MCP servers, sandbox settings) must not
    be able to change what a test proves.
    """
    destination.mkdir(parents=True, exist_ok=True)
    auth_file = source / "auth.json"
    if auth_file.is_file():
        shutil.copy2(auth_file, destination / "auth.json")
    return destination


# Shell metacharacters and separators that delimit a command word. Splitting on
# them lets a single expression cover both argv lists and shell strings such as
# ``cd "..." && export PATH=... && codex exec "..."``.
_COMMAND_WORD_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[\s;&|()<>=]+")


def _command_text(command: str | bytes | os.PathLike[str] | Sequence[Any]) -> str:
    """Flatten a spawn's command arguments into one searchable string."""
    if isinstance(command, (str, bytes, os.PathLike)):
        return os.fsdecode(command)
    return " ".join(_command_text(part) for part in command)


def spawns_codex(command: str | bytes | os.PathLike[str] | Sequence[Any]) -> bool:
    """Return whether *command* starts the codex binary.

    Handles argv lists, ``shell=True`` command strings, and ``/bin/bash -c``
    wrappers by asking whether any command word's basename is exactly
    ``codex``.  The check deliberately over-approximates: a false positive only
    triggers an isolation assertion that a healthy test already satisfies,
    while a false negative would let a real leak through.
    """
    return any(
        PurePosixPath(word.strip("'\"")).name == "codex"
        for word in _COMMAND_WORD_SEPARATORS.split(_command_text(command))
        if word
    )


def _guarded_spawn_factory(
    factory: Any,
    *,
    label: str,
    command_params: tuple[str, ...],
) -> Any:
    """Wrap *factory* so codex spawns assert on their effective environment.

    *command_params* names the parameters that carry the command to start -
    ``args`` for :class:`subprocess.Popen`, ``command`` plus ``args`` for
    :class:`pexpect.spawn`.  Both treat a missing or ``None`` ``env`` as
    "inherit this process's environment", which is what the guard then checks.
    """
    signature = inspect.signature(factory)

    class _Guarded(factory):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            bound = signature.bind_partial(*args, **kwargs)
            command = tuple(
                bound.arguments[name]
                for name in command_params
                if name in bound.arguments
            )
            if spawns_codex(command):
                env = bound.arguments.get("env")
                CODEX_HOME_POLICY.enforce(
                    os.environ if env is None else env,
                    spawning=f"{label}({_command_text(command)!r})",
                )
            super().__init__(*args, **kwargs)

    _Guarded.__name__ = factory.__name__
    _Guarded.__qualname__ = factory.__qualname__
    return _Guarded


# Built once, wrapping the real primitives, so the per-test fixture only has to
# install them.
_GUARDED_POPEN: Final[Any] = _guarded_spawn_factory(
    subprocess.Popen,
    label="subprocess.Popen",
    command_params=("args",),
)
_GUARDED_PEXPECT_SPAWN: Final[Any] = _guarded_spawn_factory(
    pexpect.spawn,
    label="pexpect.spawn",
    command_params=("command", "args"),
)


@pytest.fixture(scope="session", autouse=True)
def codex_home_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point ``CODEX_HOME`` at a throwaway home for the entire test session.

    Session-scoped rather than per-test because the redirect is what makes
    isolation the default for every test root; a per-test home is still
    available through :func:`isolated_codex_home` for tests that want one.
    """
    home = provision_codex_home(
        tmp_path_factory.mktemp("codex-home"),
        source=CODEX_HOME_POLICY.operator_home,
    )
    patcher = pytest.MonkeyPatch()
    patcher.setenv(CODEX_HOME_ENV, str(home))
    try:
        yield home
    finally:
        patcher.undo()


@pytest.fixture(autouse=True)
def codex_home_guard(
    codex_home_session: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail any test that would start real Codex against the operator's home."""
    monkeypatch.setattr(subprocess, "Popen", _GUARDED_POPEN)
    monkeypatch.setattr(pexpect, "spawn", _GUARDED_PEXPECT_SPAWN)


@pytest.fixture
def isolated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run live Codex tests without inheriting personal config or MCP servers."""
    source = CODEX_HOME_POLICY.selected_home(os.environ)
    isolated_home = provision_codex_home(
        tmp_path / "codex-home",
        source=source if source is not None else CODEX_HOME_POLICY.operator_home,
    )
    monkeypatch.setenv(CODEX_HOME_ENV, str(isolated_home))
    return isolated_home
