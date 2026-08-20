"""Making one worktree runnable, and proving it is still the same candidate.

This is the whole of what "provisioned" means, and nothing about who asked.
Two callers need it and they are not the same kind of caller:

* a **session launch** provisions the worktree an agent is about to work in,
  and a failure there is one launch of many, retryable, and bounded by an
  attempt ledger that escalates to a human once the retrying stops being worth
  anything (:mod:`.worktree_provisioning`);
* a **same-SHA revalidation** provisions a detached checkout of one exact
  recorded commit so the unchanged publication gate can run against it, and its
  bound is #139's single start allowance, already spent before the checkout
  existed (:mod:`.publication_revalidation`).

Bundling the recipe with the launch ledger would hand the second caller a
second retry predicate over the same run — an issue-keyed consecutive-failure
count, a ``needs-human`` escalation, a refusal that fires *before* the recipe —
none of which #139 admits and all of which would quietly become a way to start
a revalidation twice. So what both share lives here and is only two facts:

* **The recipe is the operator's.** Which commands run comes from
  ``Config.setup_worktree``, whose source file must resolve outside the worktree
  being provisioned. A candidate that could supply the list of commands run on
  it would be choosing what executes at orchestrator host authority, so that
  arrangement is refused rather than executed.
* **Provisioning must not alter the candidate.** Setup installs tooling; it may
  write untracked runtime state such as ``.venv`` into the worktree, and it may
  not move ``HEAD`` or leave the candidate's tracked content modified. A
  checkpoint taken before the commands is re-read afterwards, and it is read
  whether or not the commands succeeded — a failing command and an altered
  candidate are two separate facts, and the first must not suppress the second.
  The two reads and the comparison belong to
  :class:`~.candidate_integrity.CandidateIntegrity`, so an operation that is
  not provisioning can be held to the same rule without a second copy of it.

Failure is RETURNED rather than raised, because the two callers turn it into
different things: a launch failure that spends an attempt, and a revalidation
that files no verdict at all. Returning keeps this owner from deciding which.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..infra.config import Config
from ..ports.command_runner import CommandRunner
from ..ports.working_copy import WorkingCopy
from .candidate_integrity import CandidateIntegrity
from .isolation import build_runtime_tool_env

logger = logging.getLogger(__name__)

PROVISIONING_OPERATION = "provisioning"
"""How an altered candidate is attributed when the recipe is what ran."""


class WorktreeProvisioningError(RuntimeError):
    """A worktree could not be made runnable.

    Subclasses :class:`RuntimeError` because the launch paths that already
    treated a setup failure as a runtime error keep catching it unchanged.
    """


class WorktreeRunnability:
    """Runs the operator-pinned recipe in a worktree, leaving the candidate alone.

    Holds the ``Config`` rather than a snapshot of its commands so a runtime
    configuration change is picked up by the next call, exactly as reading
    ``config.setup_worktree`` at each call site used to.

    Carries no attempt count, no escalation and no notion of an issue: this
    answers "is this worktree runnable now, and did making it so change the
    candidate", which is the same question whoever is asking.
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
        # The shared owner of "did this operation alter the candidate", bound
        # to the name provisioning's changes are attributed under. The
        # continuation's quick-validation preparation binds the same owner to
        # its own name (#173), so the two cannot drift about the rule.
        self._integrity = CandidateIntegrity(
            working_copy, operation=PROVISIONING_OPERATION
        )

    @property
    def has_commands(self) -> bool:
        """Whether this repository configures any provisioning commands."""
        return bool(self._config.setup_worktree)

    def make_runnable(
        self, worktree_path: Path
    ) -> WorktreeProvisioningError | None:
        """Run the pinned recipe; return why the worktree is not runnable, or ``None``.

        Refusals are RETURNED rather than raised so every reason a worktree is
        not runnable — including an unpinned recipe, which is as persistent as
        a missing toolchain — reaches the caller through one channel, and the
        caller alone decides what a failure costs.

        A repository that configures no commands is already as runnable as it
        is going to get: nothing runs, and nothing is read about the candidate.
        """
        commands = list(self._config.setup_worktree)
        if not commands:
            return None
        try:
            self._require_pinned_recipe(worktree_path)
        except WorktreeProvisioningError as unpinned:
            return unpinned
        checkpoint = self._integrity.checkpoint(worktree_path)
        step_start = time.time()
        setup_failure: WorktreeProvisioningError | None = None
        try:
            for cmd in commands:
                self._run_command(cmd, worktree_path)
        except WorktreeProvisioningError as exc:
            setup_failure = exc
        else:
            logger.info(
                "[provisioning] Setup completed in %.1fs", time.time() - step_start
            )
        candidate_change = self._integrity.describe_change(
            worktree_path, checkpoint
        )
        if candidate_change is not None:
            logger.error("Provisioning altered the candidate: %s", candidate_change)
        if setup_failure is not None and candidate_change is not None:
            return WorktreeProvisioningError(
                f"{setup_failure}; the candidate was also altered: {candidate_change}"
            )
        if setup_failure is not None:
            return setup_failure
        if candidate_change is not None:
            return WorktreeProvisioningError(candidate_change)
        return None

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
        logger.info("[provisioning] Running setup: %s", cmd)
        result = self._command_runner.run(
            cmd,
            shell=True,
            cwd=worktree_path,
            env=build_runtime_tool_env(worktree_path),
        )
        if result.timed_out:
            logger.error("[provisioning] Setup command timed out: %s", cmd)
            raise WorktreeProvisioningError(f"setup command timed out: {cmd}")
        if result.returncode != 0:
            stderr = result.stderr.strip() or "no stderr captured"
            logger.error("Setup command failed: %s\n%s", cmd, stderr)
            raise WorktreeProvisioningError(
                f"setup command failed: {cmd} (exit_code={result.returncode}): {stderr}"
            )


__all__ = [
    "PROVISIONING_OPERATION",
    "WorktreeProvisioningError",
    "WorktreeRunnability",
]
