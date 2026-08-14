"""The single owner of worktree runtime provisioning for session launches.

Every session — coding, rework, review — runs validation *inside* its worktree,
so a worktree that lacks the repository's runtime prerequisites (its virtualenv,
its node modules, its browser binaries) cannot produce a meaningful verdict. The
gate still runs, still fails, and the failure is attributed to the candidate
commit: an environment gap is recorded as if the change failed validation (#48).

Provisioning is therefore a property of *the worktree*, not of one launch path.
This module owns it once; launch paths ask for a provisioned worktree and get
either that or a loud failure. Before this owner existed the setup commands were
invoked from two of the five launch paths, so whether a worktree was runnable
depended on which path happened to create it — a rework or review worktree
reached the publish gate unprovisioned.

Three invariants ride along with running the commands:

* **Fail closed, at the point of failure.** A provisioning failure aborts the
  launch where provisioning happened, rather than letting the session start and
  surface hours later as an unrelated gate target dying.
* **Provisioning must not alter the candidate.** Setup commands install tooling;
  they must not move ``HEAD`` or leave the candidate's tracked content modified.
  A checkpoint taken before the commands is re-read afterwards, and it is read
  whether or not the commands succeeded: a failing command and an altered
  candidate are two separate facts, and the first must not suppress the second.
* **The recipe is pinned to the operator's configuration.** Which commands run
  is read from ``Config.setup_worktree``, whose source is the configuration file
  the orchestrator was started with. That file must live outside the worktree
  being provisioned, so the worktree under test never supplies the list of
  commands run on it.

Authority
---------

Provisioning runs the configured commands at orchestrator host authority in the
worktree, and those commands resolve to the repository's own build files. That
is the same authority, in the same worktree, under which the configured
validation gate already runs the repository's build and test code
(``docs/architecture/validation.md``), and it holds under ADR-0034's
trusted-repository contract: the operator selects and onboards the repository
the orchestrator works on. Extending provisioning to the rework and review
launch paths therefore adds no class of code and no authority that the gate in
those same worktrees did not already carry — it makes the gate's verdict mean
what the record says it means. Anything stronger (a bounded execution
substrate) is the separate untrusted-repository track, not this owner.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..events import EventName
from ..infra.config import Config
from ..infra.logging_config import issue_log
from ..ports import EventSink
from ..ports.command_runner import CommandRunner
from ..ports.event_sink import make_trace_event
from ..ports.working_copy import WorkingCopy
from .isolation import build_runtime_tool_env
from .session_launch_types import LaunchResult
from .transition_log import log_transition

logger = logging.getLogger(__name__)


class WorktreeProvisioningError(RuntimeError):
    """A worktree could not be provisioned for a session launch.

    Subclasses :class:`RuntimeError` because the launch paths that already
    treated a setup failure as a runtime error keep catching it unchanged.
    """


@dataclass(frozen=True)
class _CandidateCheckpoint:
    """What the candidate looked like immediately before provisioning."""

    head_sha: str | None
    dirty: bool


class WorktreeProvisioner:
    """Makes a worktree runnable, or explains why it is not.

    Holds the ``Config`` rather than a snapshot of its commands so a runtime
    configuration change is picked up by the next launch, exactly as reading
    ``config.setup_worktree`` at each call site used to.
    """

    def __init__(
        self,
        *,
        config: Config,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
    ) -> None:
        self._config = config
        self._command_runner = command_runner
        self._working_copy = working_copy

    @property
    def has_commands(self) -> bool:
        """Whether this repository configures any provisioning commands."""
        return bool(self._config.setup_worktree)

    def provision(self, worktree_path: Path) -> None:
        """Run the configured setup commands in ``worktree_path``.

        The candidate check runs on both outcomes. A setup command that alters
        the candidate and *then* fails would otherwise abort the launch with the
        alteration left in the worktree and never reported, so the two facts are
        gathered separately and both are named in the failure.

        Raises:
            WorktreeProvisioningError: the recipe is not pinned outside the
                worktree, a command failed or timed out, or provisioning
                changed the candidate's committed state.
        """
        commands = list(self._config.setup_worktree)
        if not commands:
            return
        self._require_pinned_recipe(worktree_path)
        checkpoint = self._checkpoint(worktree_path)
        step_start = time.time()
        setup_failure: WorktreeProvisioningError | None = None
        try:
            for cmd in commands:
                self._run_command(cmd, worktree_path)
        except WorktreeProvisioningError as exc:
            setup_failure = exc
        else:
            logger.info("[launch] Setup completed in %.1fs", time.time() - step_start)
        candidate_change = self._describe_candidate_change(worktree_path, checkpoint)
        if candidate_change is not None:
            logger.error("Provisioning altered the candidate: %s", candidate_change)
        if setup_failure is not None and candidate_change is not None:
            raise WorktreeProvisioningError(
                f"{setup_failure}; the candidate was also altered: {candidate_change}"
            ) from setup_failure
        if setup_failure is not None:
            raise setup_failure
        if candidate_change is not None:
            raise WorktreeProvisioningError(candidate_change)

    def _require_pinned_recipe(self, worktree_path: Path) -> None:
        """Refuse a recipe the provisioned worktree could itself supply.

        ``Config.setup_worktree`` is only as trustworthy as the file it was read
        from. A configuration file resolved *inside* the worktree being
        provisioned would let the worktree under test choose what runs on it, so
        that arrangement is refused rather than executed. A ``Config`` built
        in-process carries no file and is trivially not worktree-sourced.
        """
        config_path = self._config.config_path
        if config_path is None:
            return
        resolved = Path(config_path).resolve()
        worktree = Path(worktree_path).resolve()
        if resolved.is_relative_to(worktree):
            raise WorktreeProvisioningError(
                "provisioning commands must come from configuration outside the "
                f"worktree they provision: {resolved} is inside {worktree}"
            )

    def _run_command(self, cmd: str, worktree_path: Path) -> None:
        logger.debug("Running setup command: %s", cmd)
        logger.info("[launch] Running setup: %s", cmd)
        result = self._command_runner.run(
            cmd,
            shell=True,
            cwd=worktree_path,
            env=build_runtime_tool_env(worktree_path),
        )
        if result.timed_out:
            logger.error("[launch] Setup command timed out: %s", cmd)
            raise WorktreeProvisioningError(f"setup command timed out: {cmd}")
        if result.returncode != 0:
            stderr = result.stderr.strip() or "no stderr captured"
            logger.error("Setup command failed: %s\n%s", cmd, stderr)
            raise WorktreeProvisioningError(
                f"setup command failed: {cmd} (exit_code={result.returncode}): {stderr}"
            )

    def _checkpoint(self, worktree_path: Path) -> _CandidateCheckpoint:
        return _CandidateCheckpoint(
            head_sha=self._working_copy.get_head_sha(worktree_path),
            dirty=self._working_copy.has_uncommitted_changes(worktree_path),
        )

    def _describe_candidate_change(
        self, worktree_path: Path, before: _CandidateCheckpoint
    ) -> str | None:
        """Name what provisioning changed about the candidate, or ``None``.

        Returns rather than raises so the caller can report it alongside a setup
        command that failed after making the change.

        A worktree that was already dirty stays a question this check cannot
        answer, so only a clean-to-dirty transition is treated as provisioning's
        doing. Moving ``HEAD`` is always provisioning's doing.
        """
        after = self._checkpoint(worktree_path)
        if after.head_sha != before.head_sha:
            return (
                f"provisioning moved HEAD in {worktree_path}: "
                f"{before.head_sha} -> {after.head_sha}"
            )
        if after.dirty and not before.dirty:
            return f"provisioning left uncommitted changes in {worktree_path}"
        return None


def provision_launch_worktree(
    provisioner: WorktreeProvisioner,
    worktree_path: Path,
    *,
    events: EventSink,
    kind: str,
    number: int,
    session_name: str,
) -> LaunchResult | None:
    """Provision a launch's worktree, or return that launch's failure.

    One reporting shape for every launch path, so the rule cannot be enforced
    one way for a coder and another way for a reviewer.
    """
    try:
        provisioner.provision(worktree_path)
    except Exception as e:
        log_transition(kind, number, "LAUNCHING", "FAILED", "setup commands failed")
        logger.error(issue_log(number, "FAILED: setup commands failed: %s"), e)
        events.publish(make_trace_event(
            EventName.SESSION_START_FAILED,
            {
                "issue_number": number,
                "session_name": session_name,
                "reason": "setup_commands_failed",
                "error": str(e),
            },
        ))
        return LaunchResult(None, False, f"Setup commands failed: {e}")
    return None
