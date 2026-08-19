"""The runnability core's own contract, independent of who is asking (#153).

Both callers exercise this core through their own policy — a session launch
turns a refusal into a spent attempt and eventually a human's problem, a
same-SHA revalidation turns it into no verdict at all — so the two properties
that belong to the core rather than to either caller are only ever implied by
those tests:

* every refusal is **returned**, not raised. The launch side observes a raise
  because :class:`WorktreeProvisioner` raises what this returns; the
  revalidation side observes a return. Neither can show that the core itself
  never raises, which is what lets the second caller exist at all;
* a command that failed **and** altered the candidate reports both in one
  message. The consumers assert on outcomes and attempt counts, not on the
  message, so the composition is untested from either side.

Everything else about the recipe — where it runs, what environment it runs
with, the attempt ledger — is pinned by the consumers and is not repeated here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from issue_orchestrator.control.worktree_runnability import (
    WorktreeProvisioningError,
    WorktreeRunnability,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.command_runner import CommandResult

SETUP = "make worktree-setup"


class StubCommandRunner:
    """Replays one scripted result for every setup command."""

    def __init__(self, *, result: CommandResult | None = None) -> None:
        self.commands: list[str | list[str]] = []
        self._result = result or CommandResult(
            returncode=0, stdout="", stderr="", timed_out=False
        )

    def run(self, command: str | list[str], **_kwargs: Any) -> CommandResult:
        self.commands.append(command)
        return self._result


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

    def get_head_sha(self, worktree: Path) -> str | None:
        if self._head_shas:
            return self._head_shas.pop(0)
        return "sha-unchanged"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        if self._dirty:
            return self._dirty.pop(0)
        return False


def _runnability(
    *,
    commands: list[str],
    runner: StubCommandRunner | None = None,
    working_copy: StubWorkingCopy | None = None,
    config_path: Path | None = None,
) -> tuple[WorktreeRunnability, StubCommandRunner]:
    config = Config()
    config.setup_worktree = commands
    if config_path is not None:
        config.config_path = config_path
    runner = runner or StubCommandRunner()
    core = WorktreeRunnability(
        config=config,
        command_runner=runner,
        working_copy=working_copy or StubWorkingCopy(),
    )
    return core, runner


def _failing_runner(*, stderr: str = "pyright: not found") -> StubCommandRunner:
    return StubCommandRunner(
        result=CommandResult(returncode=1, stdout="", stderr=stderr, timed_out=False)
    )


class TestRefusalsAreReturned:
    """No refusal leaves this owner by raising, whatever the reason.

    The caller decides what a failure costs, and it can only do that if every
    reason arrives through the same channel.
    """

    def test_a_failing_setup_command_is_returned(self, tmp_path):
        core, runner = _runnability(commands=[SETUP], runner=_failing_runner())

        failure = core.make_runnable(tmp_path)

        assert isinstance(failure, WorktreeProvisioningError)
        assert SETUP in str(failure)
        assert runner.commands == [SETUP]

    def test_a_timed_out_setup_command_is_returned(self, tmp_path):
        core, _ = _runnability(
            commands=[SETUP],
            runner=StubCommandRunner(
                result=CommandResult(
                    returncode=137, stdout="", stderr="killed", timed_out=True
                )
            ),
        )

        failure = core.make_runnable(tmp_path)

        assert isinstance(failure, WorktreeProvisioningError)
        assert "timed out" in str(failure)

    def test_an_altered_candidate_is_returned(self, tmp_path):
        core, _ = _runnability(
            commands=[SETUP],
            working_copy=StubWorkingCopy(head_shas=["sha-a", "sha-b"]),
        )

        failure = core.make_runnable(tmp_path)

        assert isinstance(failure, WorktreeProvisioningError)
        assert "moved HEAD" in str(failure)

    def test_an_unpinned_recipe_is_returned_before_anything_runs(self, tmp_path):
        """The one refusal raised internally, so the one most able to escape."""
        core, runner = _runnability(
            commands=[SETUP], config_path=tmp_path / "config.yaml"
        )

        failure = core.make_runnable(tmp_path)

        assert isinstance(failure, WorktreeProvisioningError)
        assert "outside the worktree" in str(failure)
        assert runner.commands == []

    def test_a_runnable_worktree_returns_none(self, tmp_path):
        core, runner = _runnability(commands=[SETUP])

        assert core.make_runnable(tmp_path) is None
        assert runner.commands == [SETUP]


class TestFailureAndAlterationCompose:
    """A failing command must not suppress the candidate it damaged on the way.

    These are two separate facts about one run: the environment is not ready,
    *and* the candidate under test is no longer the one that was checked out.
    Reporting only the first would leave an altered candidate to be discovered
    by whatever ran next.
    """

    def test_one_message_names_both_the_failure_and_the_alteration(self, tmp_path):
        core, _ = _runnability(
            commands=[SETUP],
            runner=_failing_runner(stderr="lockfile is unreadable"),
            working_copy=StubWorkingCopy(head_shas=["sha-a", "sha-b"]),
        )

        failure = core.make_runnable(tmp_path)

        assert isinstance(failure, WorktreeProvisioningError)
        message = str(failure)
        assert "lockfile is unreadable" in message
        assert "moved HEAD" in message
        assert "sha-a -> sha-b" in message

    def test_the_candidate_is_read_after_a_failing_command(self, tmp_path):
        """A dirty candidate is found even though the command already failed."""
        core, _ = _runnability(
            commands=[SETUP],
            runner=_failing_runner(),
            working_copy=StubWorkingCopy(dirty=[False, True]),
        )

        failure = core.make_runnable(tmp_path)

        assert isinstance(failure, WorktreeProvisioningError)
        assert "uncommitted changes" in str(failure)
