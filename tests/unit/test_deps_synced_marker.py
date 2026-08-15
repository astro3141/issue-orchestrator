"""What `.venv/.deps-synced` is allowed to claim (#60).

The marker used to be written with an unconditional `touch` inside a
`;`-separated recipe block, so a `uv sync` that failed — or one that succeeded
having installed nothing — still produced the marker and still let `make` exit
0. These tests drive the real recipe with a fake `uv` and assert the failure
direction: no marker, non-zero recipe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import venv
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
MARKER_NAME = ".deps-synced"


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


def _run_venv_fast(checkout: FakeCheckout) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("MAKEFLAGS", None)
    # The probe must read what the environment resolves, not what the caller's
    # shell happens to export; agent sessions always carry a PYTHONPATH.
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [
            _gnu_make(),
            "venv-fast",
            f"UV={checkout.uv_path}",
            f"SETUP_LOG={checkout.root / 'setup.log'}",
        ],
        cwd=checkout.root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_venv_fast_records_marker_when_sync_populates_environment(
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, _install_project(checkout))

    result = _run_venv_fast(checkout)

    assert result.returncode == 0, result.stderr
    assert checkout.marker.exists()


def test_venv_fast_fails_without_marker_when_sync_fails(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, '  echo "uv sync exploded" >&2\n  exit 1')
    # A marker left by an earlier, healthy run: it claims the environment is
    # usable, and must not survive a sync that failed.
    checkout.marker.touch()

    result = _run_venv_fast(checkout)

    assert result.returncode != 0
    assert not checkout.marker.exists()


def test_venv_fast_fails_without_marker_when_sync_installs_nothing(
    tmp_path: Path,
) -> None:
    # The measured shape of the #53 failure: `uv sync` exits 0, site-packages
    # stays empty, and nothing in the recipe noticed.
    checkout = _make_checkout(tmp_path)
    _write_fake_uv(checkout, "  :")
    checkout.marker.touch()

    result = _run_venv_fast(checkout)

    assert result.returncode != 0
    assert not checkout.marker.exists()
    assert "did not install issue_orchestrator" in result.stderr


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


def test_no_recipe_writes_the_marker_without_the_rule() -> None:
    # Every writer goes through scripts/deps_marker.sh, which is where the
    # claim is checked. A bare `touch` anywhere here reintroduces the defect on
    # one path while the others stay honest.
    makefile = MAKEFILE.read_text(encoding="utf-8")

    offenders = [
        line.strip()
        for line in makefile.splitlines()
        if "touch" in line and f".venv/{MARKER_NAME}" in line
    ]

    assert offenders == [], offenders
