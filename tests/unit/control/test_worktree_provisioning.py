"""The worktree provisioning owner (#48).

A worktree that reaches validation without the repository's runtime
prerequisites produces a verdict about the environment while the record says
the verdict is about the candidate commit. These tests pin the owner that
makes a worktree runnable: what it runs, where it runs it, and the two ways it
refuses — a failing setup command, and provisioning that changed the candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.control.isolation import get_worktree_venv_bin
from issue_orchestrator.control.worktree_provisioning import (
    WorktreeProvisioner,
    WorktreeProvisioningError,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.command_runner import CommandResult


class RecordingCommandRunner:
    """Records setup command invocations and replays queued results."""

    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._results = list(results or [])

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        **_kwargs: Any,
    ) -> CommandResult:
        self.calls.append({"command": command, "cwd": cwd, "env": env, "shell": shell})
        if self._results:
            return self._results.pop(0)
        return CommandResult(returncode=0, stdout="", stderr="", timed_out=False)


class StubWorkingCopy:
    """Reports a scripted HEAD/dirty history for the checkpoint comparison."""

    def __init__(
        self,
        *,
        head_shas: list[str | None] | None = None,
        dirty: list[bool] | None = None,
    ) -> None:
        self._head_shas = list(head_shas or [])
        self._dirty = list(dirty or [])
        self.head_sha_reads = 0
        self.dirty_reads = 0

    def get_head_sha(self, worktree: Path) -> str | None:
        self.head_sha_reads += 1
        if self._head_shas:
            return self._head_shas.pop(0)
        return "sha-unchanged"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        self.dirty_reads += 1
        if self._dirty:
            return self._dirty.pop(0)
        return False


def _provisioner(
    *,
    commands: list[str],
    runner: RecordingCommandRunner | None = None,
    working_copy: StubWorkingCopy | None = None,
) -> tuple[WorktreeProvisioner, RecordingCommandRunner, StubWorkingCopy]:
    config = Config()
    config.setup_worktree = commands
    runner = runner or RecordingCommandRunner()
    working_copy = working_copy or StubWorkingCopy()
    provisioner = WorktreeProvisioner(
        config=config,
        command_runner=runner,
        working_copy=working_copy,
    )
    return provisioner, runner, working_copy


class TestProvisioningCommands:
    """What the owner runs, and where."""

    def test_runs_every_configured_command_in_the_worktree(self, tmp_path):
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup", "npm ci"]
        )

        provisioner.provision(tmp_path)

        assert [call["command"] for call in runner.calls] == [
            "make worktree-setup",
            "npm ci",
        ]
        assert all(call["cwd"] == tmp_path for call in runner.calls)
        assert all(call["shell"] is True for call in runner.calls)

    def test_commands_resolve_tools_from_the_worktree_venv(self, tmp_path):
        provisioner, runner, _ = _provisioner(commands=["make worktree-setup"])

        provisioner.provision(tmp_path)

        env = runner.calls[0]["env"]
        assert env is not None
        assert env["PATH"].startswith(str(get_worktree_venv_bin(tmp_path)))

    def test_no_configured_commands_touches_nothing(self, tmp_path):
        provisioner, runner, working_copy = _provisioner(commands=[])

        provisioner.provision(tmp_path)

        assert runner.calls == []
        assert working_copy.head_sha_reads == 0
        assert working_copy.dirty_reads == 0
        assert provisioner.has_commands is False

    def test_configured_commands_are_read_per_call(self, tmp_path):
        """A configuration change reaches the next launch, not the next process."""
        config = Config()
        config.setup_worktree = []
        runner = RecordingCommandRunner()
        provisioner = WorktreeProvisioner(
            config=config,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        )

        config.setup_worktree = ["make worktree-setup"]
        provisioner.provision(tmp_path)

        assert [call["command"] for call in runner.calls] == ["make worktree-setup"]


class TestProvisioningFailsClosed:
    """Provisioning refuses loudly instead of leaving a half-ready worktree."""

    def test_failing_command_names_command_exit_code_and_stderr(self, tmp_path):
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup", "npm ci"],
            runner=RecordingCommandRunner(
                [
                    CommandResult(
                        returncode=2,
                        stdout="",
                        stderr="Missing packages/vscode/node_modules",
                        timed_out=False,
                    )
                ]
            ),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        message = str(excinfo.value)
        assert "make worktree-setup" in message
        assert "exit_code=2" in message
        assert "Missing packages/vscode/node_modules" in message
        # The remaining commands never ran: provisioning stops at the failure.
        assert len(runner.calls) == 1

    def test_failing_command_without_stderr_still_explains_itself(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [CommandResult(returncode=127, stdout="", stderr="   ", timed_out=False)]
            ),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        assert "no stderr captured" in str(excinfo.value)

    def test_timeout_surfaces_as_a_timeout(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [CommandResult(returncode=137, stdout="", stderr="killed", timed_out=True)]
            ),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        assert "timed out" in str(excinfo.value)
        assert "exit_code=137" not in str(excinfo.value)

    def test_provisioning_error_is_a_runtime_error(self):
        assert issubclass(WorktreeProvisioningError, RuntimeError)


class TestProvisioningLeavesTheCandidateAlone:
    """Installing tooling must not become editing the change under test."""

    def test_moving_head_is_refused(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(head_shas=["sha-before", "sha-after"]),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        assert "moved HEAD" in str(excinfo.value)
        assert "sha-before" in str(excinfo.value)
        assert "sha-after" in str(excinfo.value)

    def test_dirtying_a_clean_worktree_is_refused(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(dirty=[False, True]),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        assert "uncommitted changes" in str(excinfo.value)

    def test_an_already_dirty_worktree_is_not_blamed_on_provisioning(self, tmp_path):
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(dirty=[True, True]),
        )

        provisioner.provision(tmp_path)

        assert len(runner.calls) == 1

    def test_ignored_build_output_keeps_the_worktree_clean(self, tmp_path):
        """The real prerequisites (`.venv`, `node_modules`) are gitignored."""
        provisioner, _, working_copy = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(dirty=[False, False]),
        )

        provisioner.provision(tmp_path)

        assert working_copy.dirty_reads == 2

    def test_a_command_that_alters_the_candidate_and_then_fails_reports_both(
        self, tmp_path
    ):
        """The failure of one fact must not suppress the report of the other.

        A setup command that edits the candidate and exits nonzero aborts the
        launch either way — but without the post-check on the failure path the
        alteration is left in the worktree and never named.
        """
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [
                    CommandResult(
                        returncode=1,
                        stdout="",
                        stderr="patched then died",
                        timed_out=False,
                    )
                ]
            ),
            working_copy=StubWorkingCopy(dirty=[False, True]),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        message = str(excinfo.value)
        assert "exit_code=1" in message
        assert "patched then died" in message
        assert "uncommitted changes" in message

    def test_the_candidate_is_re_read_even_when_a_command_fails(self, tmp_path):
        provisioner, _, working_copy = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [CommandResult(returncode=1, stdout="", stderr="boom", timed_out=False)]
            ),
        )

        with pytest.raises(WorktreeProvisioningError):
            provisioner.provision(tmp_path)

        assert working_copy.head_sha_reads == 2
        assert working_copy.dirty_reads == 2

    def test_a_command_that_fails_without_altering_the_candidate_reports_only_that(
        self, tmp_path
    ):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [CommandResult(returncode=1, stdout="", stderr="boom", timed_out=False)]
            ),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path)

        assert "uncommitted changes" not in str(excinfo.value)
        assert "moved HEAD" not in str(excinfo.value)


class TestProvisioningRecipeIsPinned:
    """What runs is the operator's configuration, not the worktree's own."""

    def test_configuration_outside_the_worktree_provisions_normally(self, tmp_path):
        config = Config()
        config.setup_worktree = ["make worktree-setup"]
        config.config_path = tmp_path / "config" / "selfhost.yaml"
        runner = RecordingCommandRunner()
        provisioner = WorktreeProvisioner(
            config=config,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        provisioner.provision(worktree)

        assert len(runner.calls) == 1

    def test_configuration_inside_the_provisioned_worktree_is_refused(self, tmp_path):
        config = Config()
        config.setup_worktree = ["make worktree-setup"]
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        config.config_path = worktree / ".issue-orchestrator" / "config.yaml"
        runner = RecordingCommandRunner()
        provisioner = WorktreeProvisioner(
            config=config,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(worktree)

        assert "outside the worktree" in str(excinfo.value)
        assert runner.calls == []
