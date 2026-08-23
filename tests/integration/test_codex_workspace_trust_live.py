"""Live failure-direction proof for the approved workspace-trust grant (#215).

Two runs of the *same* production argv, differing only by the grant, against
the installed Codex CLI in a real linked worktree with an isolated
``CODEX_HOME``:

======================  ====================================================
no approved grant       the trust dialog blocks; the TUI is never reached
approved-root grant     the dialog is absent; the TUI reaches its composer
======================  ====================================================

A third run carries a grant naming a *different* root and blocks again, which
is what makes the grant a repository-root decision rather than a blanket
"trusted" flag.

Two things this file is careful about, both learned the hard way:

* **The dialog is matched on whitespace-normalized output.** Codex's TUI paints
  each word at an absolute cursor position, so the spaces on screen are not in
  the raw stream; matching the sentence as written produces a false negative
  while the dialog is on screen (it did, during #204's measurement).
* **This is provider evidence for one Codex version.** Trust storage, the
  ``-c`` layer's participation in the trust gate, and the accepted spelling of
  a ``projects`` override are upstream behaviours that can change between
  releases — the accepted spelling already differed from the one #215
  specified. The version is asserted, so an upgrade fails this test loudly
  instead of silently invalidating what it claims to prove.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pexpect
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    # Spawns the real Codex CLI: the marker, not a filename list, is what keeps
    # it out of blocking candidate validation (#194).
    pytest.mark.live_agent,
    pytest.mark.live_codex,
    pytest.mark.xdist_group("codex"),
]

from issue_orchestrator.domain.workspace_trust import RepositoryTrustGrant
from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider
from issue_orchestrator.execution.agent_runner_providers.codex_trust import (
    CODEX_TRUST_MECHANISM,
    authorize_codex_workspace_trust,
    codex_trust_override_argv,
    resolve_codex_common_repository_root,
)

from tests.codex_home import CODEX_HOME_POLICY
from tests.workspace_trust import approval_for, approved_workspace

from .conftest import xdist_timeout

# The Codex release these findings were measured against. #204 recorded the
# trust behaviour for 0.147.0 and said explicitly that it must not be
# generalized; asserting the version keeps that promise mechanical.
MEASURED_CODEX_VERSION = "0.147.0"

# The dialog, as it survives a TUI that paints words at absolute positions.
TRUST_DIALOG = "doyoutrustthecontentsofthisdirectory?"
# The composer frame's header. Reaching it is what "ran unattended" means.
TUI_BANNER = "openaicodex(v"

_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")

# Long enough for a cold Codex start to paint either the dialog or the
# composer, short enough that a genuine park stays bounded. The dialog appears
# in well under a second; the TUI takes a few.
_OBSERVE_SECONDS = 14.0


def _normalize(raw: str) -> str:
    """Collapse the TUI's absolute-position painting into a searchable string."""
    text = _OSC.sub("", raw)
    text = _ANSI.sub("", text)
    return re.sub(r"\s+", "", text).casefold()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _codex_version() -> str:
    """The installed provider CLI's own version string."""
    result = subprocess.run(
        ["codex", "--version"],
        capture_output=True,
        text=True,
        timeout=xdist_timeout(30),
        check=True,
    )
    return (result.stdout or result.stderr).strip()


def _observe(argv: list[str], *, cwd: Path) -> str:
    """Run *argv* under a PTY for a bounded window and return what it painted."""
    timeout = xdist_timeout(_OBSERVE_SECONDS)
    child = pexpect.spawn(
        argv[0],
        argv[1:],
        cwd=str(cwd),
        env=dict(os.environ),
        encoding=None,
        dimensions=(40, 120),
        timeout=timeout,
    )
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                chunks.append(child.read_nonblocking(4096, timeout=1))
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
    finally:
        child.close(force=True)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _drop_grant(argv: list[str], override: str) -> list[str]:
    """The same launch with only the trust ``-c`` pair removed.

    Derived from the granted argv rather than written out, so the two runs
    cannot drift apart in any other flag — the grant is the only difference.
    """
    index = argv.index(override)
    assert argv[index - 1] == "-c"
    return argv[: index - 1] + argv[index + 1 :]


def _retarget_grant(argv: list[str], override: str, other_root: Path) -> list[str]:
    """The same launch, but trusting *other_root* instead of the real one."""
    elsewhere = codex_trust_override_argv(
        RepositoryTrustGrant(
            approved=approval_for(other_root),
            resolved_common_root=other_root.resolve(),
            mechanism=CODEX_TRUST_MECHANISM,
        )
    )
    index = argv.index(override)
    return argv[:index] + [elsewhere[1]] + argv[index + 1 :]


@pytest.fixture(autouse=True)
def require_codex_cli() -> None:
    """Fail loudly rather than skip: the proof is about the installed CLI."""
    if shutil.which("codex") is None:
        pytest.fail(
            "Codex CLI not found. This proof measures the installed provider; "
            "install Codex (brew install --cask codex) and re-run."
        )


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real repository plus a linked worktree, as the launcher creates them."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _git("init", "-b", "main", cwd=repository)
    _git("config", "user.email", "trust-proof@example.com", cwd=repository)
    _git("config", "user.name", "trust proof", cwd=repository)
    (repository / "README.md").write_text("trust proof\n", encoding="utf-8")
    _git("add", "-A", cwd=repository)
    _git("commit", "-m", "seed", cwd=repository)
    worktree = tmp_path / "wt-215"
    _git("worktree", "add", str(worktree), cwd=repository)
    return repository.resolve(), worktree.resolve()


class TestCodexWorkspaceTrustLive:
    """The grant is what decides whether an unattended launch begins."""

    def test_measured_against_the_recorded_codex_version(self) -> None:
        """An upgrade must invalidate this evidence loudly, not silently."""
        assert MEASURED_CODEX_VERSION in _codex_version(), (
            "Codex workspace-trust behaviour is measured evidence for "
            f"{MEASURED_CODEX_VERSION}; re-measure before trusting these "
            "assertions on another release"
        )

    def test_failure_direction_and_host_config_are_both_preserved(
        self,
        linked_worktree: tuple[Path, Path],
        isolated_codex_home: Path,
    ) -> None:
        """No grant parks; the approved grant runs; a wrong root parks again.

        Also proves the mechanism's central promise: per-launch materialization
        writes nothing. The operator's own ``config.toml`` is compared
        byte-for-byte across all three runs, and the isolated home — which
        starts with no ``config.toml`` at all — must not gain one either.
        """
        repository, worktree = linked_worktree
        assert resolve_codex_common_repository_root(worktree) == repository

        operator_config = CODEX_HOME_POLICY.operator_home / "config.toml"
        before = operator_config.read_bytes() if operator_config.is_file() else None

        granted = CodexProvider().build_command(
            prompt="reply with the single word ok",
            launch_workspace=approved_workspace(worktree, repository),
        )
        override = codex_trust_override_argv(
            authorize_codex_workspace_trust(
                approved_workspace(worktree, repository)
            )
        )[1]
        assert override in granted, "the launch argv must carry the grant"

        ungranted_output = _observe(_drop_grant(granted, override), cwd=worktree)
        granted_output = _observe(granted, cwd=worktree)
        wrong_root_output = _observe(
            _retarget_grant(granted, override, repository.parent),
            cwd=worktree,
        )

        assert TRUST_DIALOG in _normalize(ungranted_output), (
            "expected the workspace-trust dialog to block a launch with no "
            "approved grant"
        )
        assert TUI_BANNER not in _normalize(ungranted_output)

        assert TRUST_DIALOG not in _normalize(granted_output), (
            "the approved common-root grant did not suppress the trust dialog; "
            "re-measure the accepted -c spelling for this Codex release"
        )
        assert TUI_BANNER in _normalize(granted_output), (
            "the approved launch did not reach the Codex composer unattended"
        )

        assert TRUST_DIALOG in _normalize(wrong_root_output), (
            "a grant naming a different repository root must not trust this "
            "one — the grant is keyed to the root, not a blanket flag"
        )

        after = operator_config.read_bytes() if operator_config.is_file() else None
        assert after == before, (
            "per-launch materialization must not modify the operator's Codex "
            "config"
        )
        assert not (isolated_codex_home / "config.toml").exists(), (
            "per-launch materialization must not write a Codex config at all"
        )
