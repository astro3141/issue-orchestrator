"""What `.venv/.deps-synced` is allowed to claim (#60).

The marker used to be written with an unconditional `touch` inside a
`;`-separated recipe block, so a `uv sync` that failed — or one that succeeded
having installed nothing — still produced the marker and still let `make` exit
0. These tests drive the real recipe with a fake `uv` and assert the failure
direction: no marker, non-zero recipe.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import venv
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
LAUNCHER = REPO_ROOT / "scripts" / "start_control_center.sh"
MARKER_NAME = ".deps-synced"
MARKER_TOOL = "$(DEPS_MARKER_TOOL)"

# Recipes whose whole job is one sync, so the harness can drive them without
# arranging staleness first. `sync-deps` syncs conditionally and is covered
# separately.
ALWAYS_SYNCING_RECIPES = ("venv-fast", "install")


def _gnu_make() -> str:
    make_bin = shutil.which("gmake") or shutil.which("make")
    if make_bin is None:
        pytest.fail("GNU make is required to validate Makefile targets")
    result = subprocess.run(
        [make_bin, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or "GNU Make" not in result.stdout:
        pytest.fail("GNU make is required to validate Makefile targets")
    return make_bin


def _site_packages(venv_path: Path) -> Path:
    result = subprocess.run(
        [
            str(venv_path / "bin" / "python"),
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


@dataclass(frozen=True)
class FakeCheckout:
    """A checkout carrying the real recipe, a real `.venv`, and a fake `uv`."""

    root: Path
    venv_path: Path
    uv_path: Path

    @property
    def marker(self) -> Path:
        return self.venv_path / MARKER_NAME


def _make_checkout(tmp_path: Path) -> FakeCheckout:
    root = tmp_path / "repo"
    (root / "src" / "issue_orchestrator").mkdir(parents=True)
    (root / "src" / "issue_orchestrator" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='issue-orchestrator'\nversion='0.0.0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("# lock\n", encoding="utf-8")

    # The recipe under test, and the marker rule it delegates to, verbatim.
    shutil.copy2(MAKEFILE, root / "Makefile")
    scripts_dir = root / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "deps_marker.sh", scripts_dir)

    # An existing, healthy environment: `venv-fast` reuses it and syncs into it,
    # which is the path the defect was measured on.
    venv_path = root / ".venv"
    venv.EnvBuilder(with_pip=False).create(venv_path)

    # A Semgrep environment that already satisfies its own reuse test, so
    # `venv-fast`'s trailing `semgrep-venv` call is a no-op here.
    semgrep_venv = root / ".venv-semgrep"
    (semgrep_venv / "bin").mkdir(parents=True)
    semgrep_bin = semgrep_venv / "bin" / "semgrep"
    semgrep_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    semgrep_bin.chmod(0o755)
    (semgrep_venv / MARKER_NAME).touch()

    return FakeCheckout(root=root, venv_path=venv_path, uv_path=root / "fake-uv")


def _write_fake_uv(checkout: FakeCheckout, sync_body: str) -> None:
    checkout.uv_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "${1:-}" == "sync" ]]; then\n'
        f"{sync_body}\n"
        "fi\n",
        encoding="utf-8",
    )
    checkout.uv_path.chmod(0o755)


def _install_project(checkout: FakeCheckout) -> str:
    """A `uv sync` that populates the environment, as a healthy one does."""
    editable_pth = _site_packages(checkout.venv_path) / "issue_orchestrator.pth"
    return f'  printf "%s\\n" "{checkout.root / "src"}" > "{editable_pth}"'


def _run_recipe(
    checkout: FakeCheckout, recipe: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("MAKEFLAGS", None)
    # The probe must read what the environment resolves, not what the caller's
    # shell happens to export; agent sessions always carry a PYTHONPATH.
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [
            _gnu_make(),
            recipe,
            f"UV={checkout.uv_path}",
            f"SETUP_LOG={checkout.root / 'setup.log'}",
        ],
        cwd=checkout.root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _run_venv_fast(checkout: FakeCheckout) -> subprocess.CompletedProcess[str]:
    return _run_recipe(checkout, "venv-fast")


def _age_marker_behind_manifests(checkout: FakeCheckout) -> None:
    """Make `sync-deps` decide a sync is due, the way a manifest edit does.

    The marker is aged rather than the manifests touched: `[ -nt ]` compares at
    whole-second resolution in the recipe's shell, so same-second writes read as
    equally old.
    """
    checkout.marker.touch()
    aged = checkout.root.joinpath("pyproject.toml").stat().st_mtime - 60
    os.utime(checkout.marker, (aged, aged))


@pytest.mark.parametrize("recipe", ALWAYS_SYNCING_RECIPES)
def test_recipe_records_marker_when_sync_populates_environment(
    recipe: str,
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, _install_project(checkout))

    result = _run_recipe(checkout, recipe)

    assert result.returncode == 0, result.stderr
    assert checkout.marker.exists()


@pytest.mark.parametrize("recipe", ALWAYS_SYNCING_RECIPES)
def test_recipe_fails_without_marker_when_sync_fails(
    recipe: str,
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, '  echo "uv sync exploded" >&2\n  exit 1')
    # A marker left by an earlier, healthy run: it claims the environment is
    # usable, and must not survive a sync that failed.
    checkout.marker.touch()

    result = _run_recipe(checkout, recipe)

    assert result.returncode != 0
    assert not checkout.marker.exists()


@pytest.mark.parametrize("recipe", ALWAYS_SYNCING_RECIPES)
def test_recipe_fails_without_marker_when_sync_installs_nothing(
    recipe: str,
    tmp_path: Path,
) -> None:
    # The measured shape of the #53 failure: `uv sync` exits 0, site-packages
    # stays empty, and nothing in the recipe noticed.
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, "  :")
    checkout.marker.touch()

    result = _run_recipe(checkout, recipe)

    assert result.returncode != 0
    assert not checkout.marker.exists()
    assert "did not install issue_orchestrator" in result.stderr


def test_sync_deps_records_marker_when_sync_populates_environment(
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, _install_project(checkout))
    _age_marker_behind_manifests(checkout)

    result = _run_recipe(checkout, "sync-deps")

    assert result.returncode == 0, result.stderr
    assert "Auto-syncing dependencies" in result.stdout
    assert checkout.marker.exists()


def test_sync_deps_fails_without_marker_when_sync_fails(tmp_path: Path) -> None:
    # `sync-deps` is the target that *reads* the marker to decide whether the
    # environment is current, so a failed sync here must both fail the make run
    # and leave the stale claim withdrawn — otherwise the next run reads a fresh
    # marker and skips the sync it still needs.
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, '  echo "uv sync exploded" >&2\n  exit 1')
    _age_marker_behind_manifests(checkout)

    result = _run_recipe(checkout, "sync-deps")

    assert result.returncode != 0
    assert not checkout.marker.exists()


def test_venv_fast_fails_without_marker_when_sync_installs_another_checkout(
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    sibling = tmp_path / "sibling"
    (sibling / "src" / "issue_orchestrator").mkdir(parents=True)
    (sibling / "src" / "issue_orchestrator" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    editable_pth = _site_packages(checkout.venv_path) / "issue_orchestrator.pth"
    _write_fake_uv(
        checkout,
        f'  printf "%s\\n" "{sibling / "src"}" > "{editable_pth}"',
    )

    result = _run_venv_fast(checkout)

    assert result.returncode != 0
    assert not checkout.marker.exists()
    assert "did not install issue_orchestrator" in result.stderr


def _unowned_marker_writes(text: str) -> list[str]:
    """Lines that write the marker themselves instead of asking the owner to.

    The marker is named several ways across the writers — the literal path, the
    `DEPS_MARKER` variable, the launcher's `deps_marker_path` — and any of them
    with a `touch` in front is the defect coming back. `.venv-semgrep`'s marker
    of the same name is deliberately outside the rule (it is synced with
    `--no-install-project`, so it has no project install to probe); see
    docs/architecture/validation.md.
    """
    marker_spellings = (MARKER_NAME, "DEPS_MARKER", "deps_marker_path")
    offenders = []
    for line in text.splitlines():
        stripped = line.strip()
        if "touch" not in stripped or "SEMGREP_DEPS_MARKER" in stripped:
            continue
        if any(spelling in stripped for spelling in marker_spellings):
            offenders.append(stripped)
    return offenders


def _recipe_lines_by_target(makefile: str) -> dict[str, list[str]]:
    """Recipe lines, per target, so ordering within one target can be checked."""
    targets: dict[str, list[str]] = {}
    current: str | None = None
    for line in makefile.splitlines():
        if line.startswith("\t"):
            if current is not None:
                targets[current].append(line.strip())
            continue
        if not line.strip() or line.lstrip().startswith(
            ("#", "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif")
        ):
            # Comments, blanks and make conditionals interleave with recipe
            # lines (`upgrade-deps` brackets its lock step in an `ifdef`) and do
            # not end the target.
            continue
        target_match = re.match(r"^([A-Za-z0-9_./%-]+)\s*:(?!=)", line)
        if target_match:
            current = target_match.group(1)
            targets.setdefault(current, [])
        else:
            current = None
    return targets


@pytest.mark.parametrize(
    "historical_write",
    [
        # What `sync-deps` contained until this rule existed.
        "\t\t$(UV) sync --frozen --all-extras && touch $(DEPS_MARKER) && \\",
        # What `venv-fast` contained: the write that could not fail.
        "\ttouch .venv/.deps-synced; \\",
        # What the Control Centre launcher contained.
        '  touch "$(deps_marker_path)"',
        # And any respelling of the path.
        "\ttouch $(VENV_DIR)/.deps-synced",
    ],
)
def test_marker_write_guard_catches_the_writes_it_outlaws(
    historical_write: str,
) -> None:
    # A guard that would not have caught the code this rule replaced is not a
    # guard, so it is asked to catch each of those forms directly.
    assert _unowned_marker_writes(historical_write) == [historical_write.strip()]


def test_marker_write_guard_permits_the_semgrep_tool_environment() -> None:
    semgrep_write = "\t\ttouch $(SEMGREP_DEPS_MARKER); \\"

    assert _unowned_marker_writes(semgrep_write) == []


@pytest.mark.parametrize("writer", [MAKEFILE, LAUNCHER], ids=lambda path: path.name)
def test_no_writer_records_the_marker_without_the_rule(writer: Path) -> None:
    # Every writer goes through scripts/deps_marker.sh, which is where the
    # claim is checked. A bare `touch` in either file reintroduces the defect on
    # one path while the others stay honest.
    offenders = _unowned_marker_writes(writer.read_text(encoding="utf-8"))

    assert offenders == [], offenders


def test_every_makefile_writer_withdraws_the_claim_before_syncing() -> None:
    # The rule is the bracket, not just the write: `record` after a sync that
    # was never preceded by `clear` leaves an earlier run's marker standing when
    # the sync fails. `guard` does both halves itself; a hand-bracketed recipe
    # has to be read in order.
    offenders = []
    for target, recipe in _recipe_lines_by_target(
        MAKEFILE.read_text(encoding="utf-8")
    ).items():
        withdrawn = False
        for line in recipe:
            if f"{MARKER_TOOL} clear" in line:
                withdrawn = True
            elif f"{MARKER_TOOL} record" in line and not withdrawn:
                offenders.append(f"{target}: {line}")

    assert offenders == [], offenders
