"""A shared uv cache must not let one checkout's environment be selected in another (#53).

uv answers "which environment does this project use?" from its interpreter
cache whenever the cached entry's timestamp still matches the canonical
executable, without querying the interpreter. A cache shared between checkouts
can therefore hand a worktree the *primary* checkout's environment, and the
sync that follows repoints that environment's editable install at the worktree.
That is the observed #53 failure.

These tests drive the real ``uv`` against a real primary/worktree pair and a
real cache. The polluted cache is synthesized the way the live one was found:
uv writes both entries itself, and only the *contents* of the worktree's entry
are replaced with the primary's record — so the key, its shard, and the
timestamp validity check are uv's own, not this test's guesses.

The failure direction is what is pinned: with no bound the selection lands on
the primary checkout, and the bound is what moves it back. Remove the
``UV_CACHE_DIR`` line from ``build_runtime_tool_env`` and
``test_polluted_cache_does_not_reach_the_primary_environment`` fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.control.isolation import (
    build_runtime_tool_env,
    build_runtime_tool_env_assignments,
    get_uv_cache_dir,
)

UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not installed")

# A project with no dependencies: the probe needs a resolvable project, not a
# resolution. Nothing is downloaded, so the tests neither need a network nor
# measure one.
PYPROJECT = """\
[project]
name = "fixtureproj"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""


def _uv(*args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    assert UV is not None
    return subprocess.run(
        [UV, *args], cwd=cwd, env=env, capture_output=True, text=True
    )


def _base_env(cache: Path) -> dict[str, str]:
    """The environment an unbounded orchestrated command would inherit."""
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(cache)
    return env


def _make_checkout(root: Path, name: str) -> Path:
    checkout = root / name
    (checkout / "src" / "fixtureproj").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(PYPROJECT)
    (checkout / "src" / "fixtureproj" / "__init__.py").write_text("")
    return checkout


def _selected_environment(cwd: Path, env: dict[str, str]) -> Path:
    """Return the environment uv selects for the project at ``cwd``.

    uv names a local environment relatively (``.venv``) and a foreign one
    absolutely, so the report is resolved against ``cwd`` before comparison —
    the distinction under test is which checkout it lands in, not how it was
    spelled.

    ``--dry-run`` is deliberate: the question is which environment uv
    *selects*, and asking it must not be able to damage the checkout that a
    regression would target.
    """
    result = _uv("sync", "--dry-run", cwd=cwd, env=env)
    output = result.stdout + result.stderr
    for line in output.splitlines():
        if "project environment at:" in line:
            reported = line.split("at:", 1)[1].strip()
            return (cwd / reported).resolve()
    raise AssertionError(f"uv reported no environment selection:\n{output}")


def _interpreter_entries(cache: Path) -> list[Path]:
    return sorted((cache / "interpreter-v4").rglob("*.msgpack"))


def _entry_naming(entries: list[Path], venv: Path) -> Path:
    """Return the cache entry whose recorded interpreter belongs to ``venv``."""
    needle = str(venv).encode()
    matches = [e for e in entries if needle in e.read_bytes()]
    assert len(matches) == 1, f"expected one entry naming {venv}, found {matches}"
    return matches[0]


def _venv_fingerprint(venv: Path) -> set[tuple[str, int]]:
    """Identity of what is installed in ``venv``, by name and mtime."""
    site = next(venv.glob("lib/python*/site-packages"), None)
    if site is None:
        return set()
    return {(p.name, p.stat().st_mtime_ns) for p in site.iterdir()}


@pytest.fixture
def checkouts(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A primary checkout and a worktree of the same project, each with a venv."""
    primary = _make_checkout(tmp_path, "primary")
    worktree = _make_checkout(tmp_path, "worktree")
    shared_cache = tmp_path / "shared-cache"
    for checkout in (primary, worktree):
        assert _uv(
            "venv", ".venv", cwd=checkout, env=_base_env(shared_cache)
        ).returncode == 0
    return primary, worktree, shared_cache


@pytest.fixture
def polluted_cache(checkouts: tuple[Path, Path, Path]) -> Path:
    """A shared cache whose worktree entry carries the primary's interpreter.

    Both entries are written by uv, so the key, the shard and the timestamp the
    validity check compares against are uv's own. Only the payload is moved.
    """
    primary, worktree, shared_cache = checkouts
    for checkout in (worktree, primary):
        _selected_environment(checkout, _base_env(shared_cache))

    entries = _interpreter_entries(shared_cache)
    worktree_entry = _entry_naming(entries, worktree / ".venv")
    primary_entry = _entry_naming(entries, primary / ".venv")
    worktree_entry.write_bytes(primary_entry.read_bytes())
    return shared_cache


def test_unbounded_run_selects_the_other_checkouts_environment(
    checkouts: tuple[Path, Path, Path], polluted_cache: Path
) -> None:
    """The failure this bound exists to prevent, reproduced on demand."""
    primary, worktree, _ = checkouts

    selected = _selected_environment(worktree, _base_env(polluted_cache))

    assert selected == (primary / ".venv").resolve(), (
        "expected the polluted cache to select the primary checkout's "
        f"environment, got {selected}"
    )


def test_polluted_cache_does_not_reach_the_primary_environment(
    checkouts: tuple[Path, Path, Path], polluted_cache: Path
) -> None:
    """The bound: an orchestrated run in the worktree stays in the worktree.

    This is the mutation-sensitive assertion. With the ``UV_CACHE_DIR`` line
    removed from ``build_runtime_tool_env`` the run inherits ``polluted_cache``
    from the base environment and selects the primary checkout, exactly as the
    unbounded test above shows.
    """
    primary, worktree, _ = checkouts
    before = _venv_fingerprint(primary / ".venv")

    env = build_runtime_tool_env(worktree, base_env=_base_env(polluted_cache))
    selected = _selected_environment(worktree, env)

    assert selected == (worktree / ".venv").resolve()
    assert _venv_fingerprint(primary / ".venv") == before


def test_the_bound_survives_a_real_sync(
    checkouts: tuple[Path, Path, Path], polluted_cache: Path
) -> None:
    """The primary environment is untouched by a bounded sync that actually runs.

    ``--dry-run`` proves the selection; this proves the write. A sync under the
    bound populates the worktree's own environment and leaves the primary's
    installed set and mtimes exactly as they were.
    """
    primary, worktree, _ = checkouts
    before = _venv_fingerprint(primary / ".venv")

    env = build_runtime_tool_env(worktree, base_env=_base_env(polluted_cache))
    assert _uv("sync", cwd=worktree, env=env).returncode == 0

    assert _venv_fingerprint(primary / ".venv") == before
    assert get_uv_cache_dir(worktree).exists(), (
        "the bounded run must populate the worktree's own cache"
    )


def test_a_clean_cache_selects_the_worktree_either_way(
    checkouts: tuple[Path, Path, Path]
) -> None:
    """Ordinary path: with nothing polluted, the bound changes no outcome."""
    _, worktree, shared_cache = checkouts

    unbounded = _selected_environment(worktree, _base_env(shared_cache))
    bounded = _selected_environment(
        worktree, build_runtime_tool_env(worktree, base_env=_base_env(shared_cache))
    )

    assert unbounded == (worktree / ".venv").resolve()
    assert bounded == (worktree / ".venv").resolve()


def test_the_bound_overrides_an_inherited_cache_setting(tmp_path: Path) -> None:
    """A caller-provided ``UV_CACHE_DIR`` must not win over the worktree's.

    The orchestrator launches commands from an environment it does not control,
    so inheriting a shared cache from it would reopen the same hazard.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    env = build_runtime_tool_env(
        worktree, base_env={"UV_CACHE_DIR": "/somewhere/else"}
    )

    assert env["UV_CACHE_DIR"] == str(get_uv_cache_dir(worktree))


def test_both_runtime_tool_invocation_shapes_carry_the_same_bound(
    tmp_path: Path,
) -> None:
    """The shell-assignment form must isolate what the env form isolates.

    Orchestrated commands reach the runtime tools through either shape. If only
    one carried the cache bound, the same command would be safe or unsafe
    depending on how it happened to be launched.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    env = build_runtime_tool_env(worktree, base_env={})
    assignments = build_runtime_tool_env_assignments(worktree)

    assert any(
        a.startswith("UV_CACHE_DIR=") and str(get_uv_cache_dir(worktree)) in a
        for a in assignments
    ), f"assignments do not bind UV_CACHE_DIR: {assignments}"
    assert env["UV_CACHE_DIR"] == str(get_uv_cache_dir(worktree))
