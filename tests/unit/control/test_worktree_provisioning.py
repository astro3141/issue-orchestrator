"""The worktree provisioning owner (#48, #54).

A worktree that reaches validation without the repository's runtime
prerequisites produces a verdict about the environment while the record says
the verdict is about the candidate commit. These tests pin the owner that
makes a worktree runnable: what it runs, where it runs it, the two ways it
refuses — a failing setup command, and provisioning that changed the candidate
— and how many times it will keep asking before that becomes a human's problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.control.actions import AddCommentAction, AddLabelAction
from issue_orchestrator.control.isolation import get_worktree_venv_bin
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NeedsHumanCause
from issue_orchestrator.control.worktree_provisioning import (
    PROVISIONING_ATTEMPT_LIMIT,
    PROVISIONING_ESCALATION_CONTEXT,
    ProvisioningAttempt,
    ProvisioningAttemptLedger,
    ProvisioningAttemptsExhausted,
    WorktreeProvisioner,
    WorktreeProvisioningError,
    build_provisioning_escalation,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.command_runner import CommandResult

ISSUE = 54

#: How many launches the bound tests drive. Deliberately far past any plausible
#: ceiling: the assertions are about what STOPPED happening across them, so the
#: number only has to be big enough that an unbounded loop is unmistakable.
TICKS = 25


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


class AlwaysFailingCommandRunner(RecordingCommandRunner):
    """A setup command that never succeeds — the persistent environmental fault.

    ``RecordingCommandRunner`` falls back to success once its queued results run
    out, which is exactly the wrong shape for a test whose subject is a loop
    that never ends.
    """

    def __init__(self, *, stderr: str = "boom", returncode: int = 1) -> None:
        super().__init__()
        self._stderr = stderr
        self._returncode = returncode

    def run(self, command, **kwargs: Any) -> CommandResult:
        super().run(command, **kwargs)
        return CommandResult(
            returncode=self._returncode,
            stdout="",
            stderr=self._stderr,
            timed_out=False,
        )


def _failed_provision(
    provisioner: WorktreeProvisioner,
    worktree: Path,
    *,
    issue_number: int = ISSUE,
) -> WorktreeProvisioningError:
    """One launch that is expected to fail; returns the failure it produced."""
    with pytest.raises(WorktreeProvisioningError) as excinfo:
        provisioner.provision(worktree, issue_number=issue_number)
    return excinfo.value


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


class RecordingEscalation:
    """Stands in for the launcher's needs-human escalation (#54).

    ``committed`` scripts whether the durable half of the escalation landed, so
    a test can drive the retry-until-it-commits path; ``cleared`` is how a test
    plays a human removing the label the escalation applied.
    """

    def __init__(self, *, committed: bool = True) -> None:
        self.escalations: list[ProvisioningAttempt] = []
        self.committed = committed
        self.cleared = False

    def escalate(self, attempt: ProvisioningAttempt) -> bool:
        self.escalations.append(attempt)
        return self.committed

    def still_escalated(self, issue_number: int) -> bool:
        if self.cleared:
            return False
        return any(a.issue_number == issue_number for a in self.escalations)


class EscalationRefused(Exception):
    """What the applier re-raises past this owner rather than reporting.

    ``ActionApplier.apply`` swallows most faults into a failed ``ActionResult``
    but deliberately re-raises ``ReconciliationRequired`` and ``ClaimLostError``,
    and nothing between it and the provisioner catches them. A distinct type here
    rather than one of those: the provisioner must not care WHICH exception left
    the escalation unfinished, only that one did.
    """


class RaisingEscalation(RecordingEscalation):
    """An escalation whose first attempts leave by raising, not by returning."""

    def __init__(self, *, raise_times: int = 1) -> None:
        super().__init__()
        self.raise_times = raise_times
        #: Every call, including the ones that raised. ``escalations`` records
        #: only the ones that got as far as reporting an outcome.
        self.attempted: list[ProvisioningAttempt] = []

    def escalate(self, attempt: ProvisioningAttempt) -> bool:
        self.attempted.append(attempt)
        if len(self.attempted) <= self.raise_times:
            raise EscalationRefused("the claim was lost before the label write")
        return super().escalate(attempt)


class ReentrantEscalation(RecordingEscalation):
    """A second launch for the same issue, arriving mid-escalation.

    Escalating is where the first launch spends real time (a label write and a
    comment write against GitHub), so it is the window a concurrently launching
    session actually lands in. Re-entering ``provision`` from inside
    ``escalate`` reproduces that interleaving deterministically — no threads, no
    timing — because it puts the second launch at exactly the point the first
    one has decided to escalate and has not finished doing so.
    """

    def __init__(self, *, provision: Any) -> None:
        super().__init__()
        self._provision = provision
        self._reentered = False
        self.reentrant_failure: BaseException | None = None

    def escalate(self, attempt: ProvisioningAttempt) -> bool:
        if not self._reentered:
            self._reentered = True
            try:
                self._provision()
            except WorktreeProvisioningError as exc:
                self.reentrant_failure = exc
        return super().escalate(attempt)


def _provisioner(
    *,
    commands: list[str],
    runner: RecordingCommandRunner | None = None,
    working_copy: StubWorkingCopy | None = None,
    escalation: RecordingEscalation | None = None,
    limit: int = PROVISIONING_ATTEMPT_LIMIT,
) -> tuple[WorktreeProvisioner, RecordingCommandRunner, StubWorkingCopy]:
    config = Config()
    config.setup_worktree = commands
    runner = runner or RecordingCommandRunner()
    working_copy = working_copy or StubWorkingCopy()
    provisioner = WorktreeProvisioner(
        config=config,
        command_runner=runner,
        working_copy=working_copy,
        escalation=escalation or RecordingEscalation(),
        ledger=ProvisioningAttemptLedger(limit=limit),
    )
    return provisioner, runner, working_copy


class TestProvisioningCommands:
    """What the owner runs, and where."""

    def test_runs_every_configured_command_in_the_worktree(self, tmp_path):
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup", "npm ci"]
        )

        provisioner.provision(tmp_path, issue_number=ISSUE)

        assert [call["command"] for call in runner.calls] == [
            "make worktree-setup",
            "npm ci",
        ]
        assert all(call["cwd"] == tmp_path for call in runner.calls)
        assert all(call["shell"] is True for call in runner.calls)

    def test_commands_resolve_tools_from_the_worktree_venv(self, tmp_path):
        provisioner, runner, _ = _provisioner(commands=["make worktree-setup"])

        provisioner.provision(tmp_path, issue_number=ISSUE)

        env = runner.calls[0]["env"]
        assert env is not None
        assert env["PATH"].startswith(str(get_worktree_venv_bin(tmp_path)))

    def test_no_configured_commands_touches_nothing(self, tmp_path):
        provisioner, runner, working_copy = _provisioner(commands=[])

        provisioner.provision(tmp_path, issue_number=ISSUE)

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
            escalation=RecordingEscalation(),
        )

        config.setup_worktree = ["make worktree-setup"]
        provisioner.provision(tmp_path, issue_number=ISSUE)

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
            provisioner.provision(tmp_path, issue_number=ISSUE)

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
            provisioner.provision(tmp_path, issue_number=ISSUE)

        assert "no stderr captured" in str(excinfo.value)

    def test_timeout_surfaces_as_a_timeout(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [CommandResult(returncode=137, stdout="", stderr="killed", timed_out=True)]
            ),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path, issue_number=ISSUE)

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
            provisioner.provision(tmp_path, issue_number=ISSUE)

        assert "moved HEAD" in str(excinfo.value)
        assert "sha-before" in str(excinfo.value)
        assert "sha-after" in str(excinfo.value)

    def test_dirtying_a_clean_worktree_is_refused(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(dirty=[False, True]),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(tmp_path, issue_number=ISSUE)

        assert "uncommitted changes" in str(excinfo.value)

    def test_an_already_dirty_worktree_is_not_blamed_on_provisioning(self, tmp_path):
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(dirty=[True, True]),
        )

        provisioner.provision(tmp_path, issue_number=ISSUE)

        assert len(runner.calls) == 1

    def test_ignored_build_output_keeps_the_worktree_clean(self, tmp_path):
        """The real prerequisites (`.venv`, `node_modules`) are gitignored."""
        provisioner, _, working_copy = _provisioner(
            commands=["make worktree-setup"],
            working_copy=StubWorkingCopy(dirty=[False, False]),
        )

        provisioner.provision(tmp_path, issue_number=ISSUE)

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
            provisioner.provision(tmp_path, issue_number=ISSUE)

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
            provisioner.provision(tmp_path, issue_number=ISSUE)

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
            provisioner.provision(tmp_path, issue_number=ISSUE)

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
            escalation=RecordingEscalation(),
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        provisioner.provision(worktree, issue_number=ISSUE)

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
            escalation=RecordingEscalation(),
        )

        with pytest.raises(WorktreeProvisioningError) as excinfo:
            provisioner.provision(worktree, issue_number=ISSUE)

        assert "outside the worktree" in str(excinfo.value)
        assert runner.calls == []


class TestProvisioningFailureIsBounded:
    """A failure that never stops failing must stop being retried (#54).

    These drive real repeated launches rather than asserting on a counter: the
    defect was a LOOP, so the proof has to be that the loop ends. Every test
    here runs ``TICKS`` launches — far more than any plausible bound — and
    measures what actually happened across them. Remove the bound and the recipe
    runs ``TICKS`` times, no escalation is raised, and every one of them fails.
    """

    def test_a_permanently_failing_recipe_stops_being_retried_and_escalates(
        self, tmp_path
    ):
        escalation = RecordingEscalation()
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(
                stderr="npm ERR! getaddrinfo ENOTFOUND registry.npmjs.org"
            ),
            escalation=escalation,
        )

        failures = [_failed_provision(provisioner, tmp_path) for _ in range(TICKS)]

        # The recipe ran exactly as many times as the budget allows, not once
        # per tick: this is the `npm ci` the loop was paying for every tick.
        assert len(runner.calls) == PROVISIONING_ATTEMPT_LIMIT
        # ...and a human was told, exactly once.
        assert len(escalation.escalations) == 1
        assert escalation.escalations[0].issue_number == ISSUE
        assert escalation.escalations[0].attempts == PROVISIONING_ATTEMPT_LIMIT
        assert escalation.escalations[0].limit == PROVISIONING_ATTEMPT_LIMIT
        assert "ENOTFOUND" in escalation.escalations[0].error
        # Every launch from the one that spent the budget onward names itself as
        # terminal rather than as another retryable setup failure.
        spent_at = PROVISIONING_ATTEMPT_LIMIT - 1
        assert not any(
            isinstance(f, ProvisioningAttemptsExhausted) for f in failures[:spent_at]
        )
        assert all(
            isinstance(f, ProvisioningAttemptsExhausted) for f in failures[spent_at:]
        )

    def test_the_terminal_failure_names_the_count_and_the_last_error(self, tmp_path):
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(stderr="lockfile is corrupt"),
        )

        failures = [_failed_provision(provisioner, tmp_path) for _ in range(TICKS)]

        message = str(failures[-1])
        assert "not being retried" in message
        assert str(PROVISIONING_ATTEMPT_LIMIT) in message
        assert "lockfile is corrupt" in message

    def test_a_transient_failure_recovers_without_a_human(self, tmp_path):
        """The half a "provisioning failures are human-fixable" rule gets wrong."""
        escalation = RecordingEscalation()
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [
                    CommandResult(
                        returncode=1, stdout="", stderr="registry timeout", timed_out=False
                    ),
                    CommandResult(
                        returncode=1, stdout="", stderr="registry timeout", timed_out=False
                    ),
                ]
            ),
            escalation=escalation,
        )

        _failed_provision(provisioner, tmp_path)
        _failed_provision(provisioner, tmp_path)
        provisioner.provision(tmp_path, issue_number=ISSUE)

        assert escalation.escalations == []
        assert len(runner.calls) == 3

    def test_a_success_restores_the_whole_budget(self, tmp_path):
        """Consecutive, not cumulative: two blips a week apart cost nothing."""
        escalation = RecordingEscalation()
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=RecordingCommandRunner(
                [
                    CommandResult(
                        returncode=1, stdout="", stderr="blip", timed_out=False
                    ),
                    CommandResult(returncode=0, stdout="", stderr="", timed_out=False),
                    CommandResult(
                        returncode=1, stdout="", stderr="blip", timed_out=False
                    ),
                ]
            ),
            escalation=escalation,
        )

        _failed_provision(provisioner, tmp_path)
        provisioner.provision(tmp_path, issue_number=ISSUE)
        failure = _failed_provision(provisioner, tmp_path)

        assert not isinstance(failure, ProvisioningAttemptsExhausted)
        assert escalation.escalations == []

    def test_ordinary_successful_provisioning_escalates_nothing(self, tmp_path):
        escalation = RecordingEscalation()
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup", "npm ci"], escalation=escalation
        )

        for _ in range(TICKS):
            provisioner.provision(tmp_path, issue_number=ISSUE)

        assert escalation.escalations == []
        assert len(runner.calls) == 2 * TICKS

    def test_the_budget_is_per_issue(self, tmp_path):
        """One broken worktree must not spend another issue's attempts."""
        escalation = RecordingEscalation()
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(stderr="boom"),
            escalation=escalation,
        )

        for _ in range(TICKS):
            _failed_provision(provisioner, tmp_path)
        other = _failed_provision(provisioner, tmp_path, issue_number=ISSUE + 1)

        assert not isinstance(other, ProvisioningAttemptsExhausted)
        assert [e.issue_number for e in escalation.escalations] == [ISSUE]

    def test_an_escalation_that_did_not_commit_is_retried(self, tmp_path):
        """A label write that failed must not leave the issue silently refused."""
        escalation = RecordingEscalation(committed=False)
        provisioner, _, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(stderr="boom"),
            escalation=escalation,
        )

        for _ in range(TICKS):
            _failed_provision(provisioner, tmp_path)

        # Retried on every launch from the one that spent the budget onward,
        # rather than announced once into a failed write and never spoken of again.
        assert len(escalation.escalations) == TICKS - (PROVISIONING_ATTEMPT_LIMIT - 1)

    def test_an_escalation_that_raised_is_retried_by_the_next_launch(self, tmp_path):
        """Leaving by exception is not an escalation, so the right to raise it returns.

        The applier re-raises two of its refusals past this owner rather than
        reporting them, and the launch paths that hold a claim while provisioning
        are exactly the ones a long-failing recipe keeps busy — so this arrives
        precisely where the bound matters. Holding the announcement claim on the
        way out would leave the issue with no label, no report, and no later
        launch permitted to raise one: this bound's own symptom, minus the
        ``npm ci``.
        """
        escalation = RaisingEscalation(raise_times=1)
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(stderr="boom"),
            escalation=escalation,
        )

        for _ in range(PROVISIONING_ATTEMPT_LIMIT - 1):
            _failed_provision(provisioner, tmp_path)
        with pytest.raises(EscalationRefused):
            provisioner.provision(tmp_path, issue_number=ISSUE)
        for _ in range(TICKS):
            _failed_provision(provisioner, tmp_path)

        # Raised again by the next launch, and that one landed — once.
        assert len(escalation.attempted) == 2
        assert len(escalation.escalations) == 1
        # And the refusal did not buy the recipe another run either.
        assert len(runner.calls) == PROVISIONING_ATTEMPT_LIMIT

    def test_a_human_clearing_the_escalation_restores_the_budget(self, tmp_path):
        """The durable escalation, not this process's counter, ends the refusal."""
        escalation = RecordingEscalation()
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(stderr="boom"),
            escalation=escalation,
        )
        for _ in range(TICKS):
            _failed_provision(provisioner, tmp_path)
        assert len(runner.calls) == PROVISIONING_ATTEMPT_LIMIT

        escalation.cleared = True
        _failed_provision(provisioner, tmp_path)

        # The next launch after the clear runs the recipe again: the human is
        # saying they fixed the environment, and the only way to find out is to
        # try it.
        assert len(runner.calls) == PROVISIONING_ATTEMPT_LIMIT + 1

    def test_a_recipe_the_worktree_could_supply_is_bounded_too(self, tmp_path):
        """An unpinned recipe is as persistent as a missing toolchain."""
        config = Config()
        config.setup_worktree = ["make worktree-setup"]
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        config.config_path = worktree / ".issue-orchestrator" / "config.yaml"
        escalation = RecordingEscalation()
        provisioner = WorktreeProvisioner(
            config=config,
            command_runner=RecordingCommandRunner(),
            working_copy=StubWorkingCopy(),
            escalation=escalation,
        )

        failures = [_failed_provision(provisioner, worktree) for _ in range(TICKS)]

        assert isinstance(failures[-1], ProvisioningAttemptsExhausted)
        assert len(escalation.escalations) == 1

    def test_a_second_launch_arriving_mid_escalation_escalates_nothing(self, tmp_path):
        """Claiming the escalation and checking for one are ONE step.

        The ledger is keyed per issue because a coding launch and a review
        launch for one issue share an environment and an escalation, and it
        takes a lock because those launches really do run at once. Checking
        "has this been announced?" before the escalation and recording it
        afterwards leaves both of them reading "no" and both posting the
        operator the same comment.
        """
        holder: list[WorktreeProvisioner] = []
        escalation = ReentrantEscalation(
            provision=lambda: holder[0].provision(tmp_path, issue_number=ISSUE)
        )
        provisioner, runner, _ = _provisioner(
            commands=["make worktree-setup"],
            runner=AlwaysFailingCommandRunner(stderr="boom"),
            escalation=escalation,
        )
        holder.append(provisioner)

        for _ in range(TICKS):
            _failed_provision(provisioner, tmp_path)

        assert len(escalation.escalations) == 1
        # The launch that arrived mid-escalation was refused as terminal, and
        # refused BEFORE the recipe: it must not buy the loop another `npm ci`.
        assert isinstance(escalation.reentrant_failure, ProvisioningAttemptsExhausted)
        assert len(runner.calls) == PROVISIONING_ATTEMPT_LIMIT

    def test_a_ledger_bound_below_one_is_refused(self):
        with pytest.raises(ValueError):
            ProvisioningAttemptLedger(limit=0)


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event: Any) -> None:
        self.events.append((event.event_type, event.data))


class RecordingApplier:
    """The applier seam, with each half of the escalation scriptable.

    ``comment_committed`` is separate from ``committed`` because the two halves
    are not equally load-bearing: the label is what stops the loop, the comment
    only explains it, and a comment write that fails on an issue that IS
    correctly labelled must not undo the escalation.
    """

    def __init__(self, *, committed: bool = True, comment_committed: bool = True) -> None:
        self.applied: list[tuple[list[Any], str]] = []
        self._committed = committed
        self._comment_committed = comment_committed

    def __call__(self, actions: list[Any], *, context: str) -> bool:
        self.applied.append((actions, context))
        if any(isinstance(action, AddCommentAction) for action in actions):
            return self._comment_committed
        return self._committed

    def _of_type(self, action_type: type) -> list[Any]:
        return [
            action
            for actions, _context in self.applied
            for action in actions
            if isinstance(action, action_type)
        ]

    @property
    def label_actions(self) -> list[Any]:
        return self._of_type(AddLabelAction)

    @property
    def comment_actions(self) -> list[Any]:
        return self._of_type(AddCommentAction)


def _escalation(
    *,
    applier: RecordingApplier,
    events: RecordingEventSink,
    labels: list[str] | None = None,
):
    return build_provisioning_escalation(
        apply_actions=applier,
        label_manager=LabelManager(Config()),
        events=events,
        read_labels=lambda _number: list(labels or []),
    )


class TestProvisioningEscalationSurface:
    """What an exhausted budget actually does to the issue (#54)."""

    ATTEMPT = ProvisioningAttempt(
        issue_number=ISSUE,
        attempts=3,
        limit=3,
        error="setup command failed: npm ci (exit_code=1): ENOTFOUND",
    )

    def test_it_applies_the_shared_needs_human_block_with_its_cause(self):
        applier = RecordingApplier()
        events = RecordingEventSink()

        assert _escalation(applier=applier, events=events).escalate(self.ATTEMPT)

        actions, context = applier.applied[0]
        assert context == PROVISIONING_ESCALATION_CONTEXT
        # The durable half goes first and ALONE, so what it committed is what
        # decides whether this escalation landed.
        assert len(actions) == 1
        label_action = actions[0]
        assert label_action.issue_number == ISSUE
        assert label_action.label == LabelManager(Config()).needs_human
        assert label_action.needs_human_cause is NeedsHumanCause.SESSION_LIFECYCLE

    def test_the_operator_comment_names_the_count_and_the_last_failure(self):
        applier = RecordingApplier()
        events = RecordingEventSink()

        _escalation(applier=applier, events=events).escalate(self.ATTEMPT)

        comment = applier.comment_actions[0].comment
        assert "no longer being retried" in comment
        assert "ENOTFOUND" in comment
        assert "setup_worktree" in comment
        assert "remove the label" in comment

    def test_a_comment_that_did_not_post_still_leaves_the_issue_escalated(self):
        """The block holds; re-running the whole escalation would double-post it."""
        applier = RecordingApplier(comment_committed=False)
        events = RecordingEventSink()

        committed = _escalation(applier=applier, events=events).escalate(self.ATTEMPT)

        assert committed is True
        assert [name for name, _data in events.events] == [
            EventName.ISSUE_NEEDS_HUMAN.value
        ]

    def test_the_event_carries_why_the_orchestrator_stopped(self):
        applier = RecordingApplier()
        events = RecordingEventSink()

        _escalation(applier=applier, events=events).escalate(self.ATTEMPT)

        name, data = events.events[0]
        assert name == EventName.ISSUE_NEEDS_HUMAN.value
        assert data["issue_number"] == ISSUE
        assert data["reason"] == "provisioning_attempts_exhausted"
        assert data["attempts"] == 3
        assert data["limit"] == 3

    def test_a_label_write_that_did_not_commit_publishes_nothing(self):
        """A warning whose durable half never landed vanishes on restart."""
        applier = RecordingApplier(committed=False)
        events = RecordingEventSink()

        committed = _escalation(applier=applier, events=events).escalate(self.ATTEMPT)

        assert committed is False
        assert events.events == []
        # And nothing explained a block that is not there.
        assert applier.comment_actions == []

    def test_the_escalation_is_in_force_while_the_label_is_on_the_issue(self):
        needs_human = LabelManager(Config()).needs_human
        escalation = _escalation(
            applier=RecordingApplier(),
            events=RecordingEventSink(),
            labels=[needs_human, "agent:backend"],
        )

        assert escalation.still_escalated(ISSUE) is True

    def test_clearing_the_label_ends_the_escalation(self):
        escalation = _escalation(
            applier=RecordingApplier(),
            events=RecordingEventSink(),
            labels=["agent:backend"],
        )

        assert escalation.still_escalated(ISSUE) is False

    def test_labels_that_cannot_be_read_keep_the_escalation(self):
        """Fails closed: one refused launch beats returning to the loop."""

        def unreadable(_number: int) -> list[str]:
            raise RuntimeError("GitHub is unreachable")

        escalation = build_provisioning_escalation(
            apply_actions=RecordingApplier(),
            label_manager=LabelManager(Config()),
            events=RecordingEventSink(),
            read_labels=unreadable,
        )

        assert escalation.still_escalated(ISSUE) is True
