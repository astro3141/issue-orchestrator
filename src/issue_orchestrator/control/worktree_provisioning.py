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
* **What it builds belongs to the worktree alone.** The recipe runs with the
  worktree as its working directory, so what it writes must stay there. It did
  not, in two ways. Worktree creation used to plant a ``.venv`` symlink to the
  repository's, and every run of a recipe that populates ``.venv`` wrote through
  it into the environment every other checkout used (#53); and a ``.venv`` that
  was a real directory was trusted for being a directory, so a worktree carrying
  a stale environment handed the recipe an install record naming another
  checkout, which the installer then "reconciled" by rewriting that checkout's
  environment (#61). Either way the shared environment ended up repointed at the
  worktree being provisioned, and broken when that worktree was removed. Worktree
  setup now hands over a ``.venv`` that is this worktree's own healthy
  environment or none at all
  (``adapters/worktree/_worktree_venv.ensure_worktree_owns_its_venv``), which is
  also why concurrent provisioning needs no lock here: there is no shared
  environment left to race over.

A provisioning failure is bounded
--------------------------------

Failing the launch closed is not the same as being finished with it. A failure
here is usually ENVIRONMENTAL and persistent — a missing toolchain, a broken
lockfile, an unreachable package registry — and retrying it does not help, so
an unbounded retry re-ran the recipe (for this repository an ``npm ci`` and a
browser install) every tick, spent a session slot every tick, and raised no
human-visible signal at all: busy, making no progress, healthy from every
signal except the tick log (#54).

So provisioning failures are counted per issue and bounded
(:class:`ProvisioningAttemptLedger`). The count is CONSECUTIVE — a successful
provisioning clears it — so a genuinely transient blip still recovers with no
human involved, which is the half a "provisioning failures are human-fixable
immediately" rule would have got wrong. Once the bound is spent the failure
stops being retryable: the issue is escalated to ``needs-human`` through the
injected :class:`ProvisioningEscalation`, and every later launch refuses BEFORE
running the recipe, so the loop terminates instead of reinstalling a toolchain
that is not going to appear.

The bound lives here rather than in the launch paths for the same reason the
commands do: five paths provision, and a ceiling enforced by two of them is not
a ceiling. What ENDS the refusal is the escalation itself: the ledger is
process-local, but the label is the repository's crash-safe truth, so a human
clearing it is read as the retry request and the budget is restored.

Authority
---------

Provisioning runs the configured commands at orchestrator host authority in the
worktree, and those commands resolve to the repository's own build files. That
is the same authority, in the same worktree, under which the configured
validation gate already runs the repository's build and test code
(``docs/architecture/validation.md``). Extending provisioning to the rework and
review launch paths therefore adds no class of executed code and no authority
that the gate in those same worktrees did not already carry — it makes the
gate's verdict mean what the record says it means.

One bound on that permission is enforced here and is checkable:
:meth:`WorktreeProvisioner._require_pinned_recipe` refuses to provision when the
recipe's source resolves inside the worktree being provisioned, so a candidate
cannot choose *which* commands run on it. What bounds the permission itself —
under what contract repository-controlled build code may execute at orchestrator
host authority at all — is not stated by any canonical document in this
repository. It is recorded as a CONTRACT GAP in **#55** and is not decided here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from ..events import EventName
from ..infra.config import Config
from ..infra.logging_config import issue_log
from ..ports import EventSink
from ..ports.command_runner import CommandRunner
from ..ports.event_sink import make_trace_event
from ..ports.working_copy import WorkingCopy
from .actions import Action, AddCommentAction, AddLabelAction
from .isolation import build_runtime_tool_env
from .label_manager import LabelManager
from .needs_human_block import NeedsHumanCause
from .session_launch_types import LaunchResult
from .transition_log import log_transition

logger = logging.getLogger(__name__)

#: How many CONSECUTIVE failed provisioning attempts one issue gets before the
#: failure is treated as environmental and handed to a human.
#:
#: Three, matching ``TECH_LEAD_LAUNCH_RETRY_LIMIT``: enough to ride out a
#: registry timeout or a half-written cache without reinstalling a toolchain
#: that is never going to appear. It is a constant rather than a configuration
#: key because the recipe it bounds is already the operator's own
#: (``Config.setup_worktree``) — what is bounded here is how many times the
#: orchestrator will re-run it before saying so, not what it runs.
PROVISIONING_ATTEMPT_LIMIT = 3

#: The one name this escalation goes by, in the applier's context line and in
#: the event's ``reason``, so an operator reading either finds the other.
PROVISIONING_ESCALATION_CONTEXT = "provisioning_attempts_exhausted"

#: Why the label and the comment are being applied, for the mutation log.
_ESCALATION_REASON = "worktree provisioning failed past its attempt bound"


class WorktreeProvisioningError(RuntimeError):
    """A worktree could not be provisioned for a session launch.

    Subclasses :class:`RuntimeError` because the launch paths that already
    treated a setup failure as a runtime error keep catching it unchanged.
    """


class ProvisioningAttemptsExhausted(WorktreeProvisioningError):
    """Provisioning failed past its bound: this is now a human's to fix.

    A distinct type rather than a flag on the message because it means
    something different to a caller: an ordinary
    :class:`WorktreeProvisioningError` says "this launch failed and the next
    tick may try again", and this one says "the retrying has stopped, and the
    issue has been escalated".
    """

    def __init__(self, message: str, *, issue_number: int, attempts: int) -> None:
        super().__init__(message)
        self.issue_number = issue_number
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class ProvisioningAttempt:
    """One recorded provisioning failure, and what it means for the next tick."""

    issue_number: int
    #: Consecutive failed provisioning attempts for this issue, including this one.
    attempts: int
    limit: int
    error: str

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.limit


class ProvisioningEscalation(Protocol):
    """How a provisioning failure past its bound reaches a human.

    Separate from the provisioner because the two answer different questions.
    The provisioner knows the worktree is not runnable and how many times it
    has not been; only the launcher knows how this repository tells an operator
    about it. Returns whether the escalation COMMITTED, so an escalation whose
    label write failed is retried by the next launch rather than leaving an
    issue silently un-retried and un-reported.
    """

    def escalate(self, attempt: ProvisioningAttempt) -> bool: ...

    def still_escalated(self, issue_number: int) -> bool:
        """Whether an escalation this owner raised is still in force.

        The durable escalation, not the in-memory count, is what says an issue
        is a human's. When a human clears it that IS the retry request, and a
        process-local ledger that kept refusing afterwards would make the
        escalation unclearable without a restart.
        """
        ...


class ApplyActions(Protocol):
    """The action-applier seam, in the shape both launchers already expose."""

    def __call__(self, actions: list[Action], *, context: str) -> bool: ...


class _Announcement(Enum):
    """How far this issue's escalation has got: unraised, being raised, raised.

    Three states rather than a flag because "nobody has raised it" and "a launch
    is raising it right now" are different answers to the only question the
    ledger is asked here — may I escalate? — and collapsing them is what lets two
    concurrent launches for one issue both post the operator comment.
    """

    NONE = "none"
    CLAIMED = "claimed"
    COMMITTED = "committed"


@dataclass
class _IssueProvisioningState:
    """Consecutive provisioning failures for one issue, and their escalation."""

    failures: int = 0
    last_error: str = ""
    #: How far the escalation for this issue's exhausted budget has got. Anything
    #: short of COMMITTED past the bound means the durable write has not landed,
    #: so a later launch retries it instead of treating an unreported issue as
    #: handled.
    announcement: _Announcement = _Announcement.NONE


class ProvisioningAttemptLedger:
    """The bound on how often one issue's worktree may fail to provision.

    Counts CONSECUTIVE failures, because that is the only count that separates
    the two cases the bound has to tell apart: a persistent environmental
    fault, which never succeeds and must stop, and a transient one, which
    succeeds on a later attempt and must not cost a human anything. A success
    therefore forgets the issue entirely rather than decrementing it.

    Keyed by issue number, which is the granularity ``needs-human`` is applied
    at — a coding launch and a review launch for the same issue share one
    environment and one escalation.
    """

    def __init__(self, *, limit: int = PROVISIONING_ATTEMPT_LIMIT) -> None:
        if limit < 1:
            raise ValueError(
                f"ProvisioningAttemptLedger limit must be >= 1, got {limit}"
            )
        self.limit = limit
        self._issues: dict[int, _IssueProvisioningState] = {}
        # Provisioning is deliberately unserialised (there is no shared
        # environment left to race over), so two worktrees really can be
        # provisioned at once. The counter they share is not a worktree, and a
        # lost increment here is a bound that quietly does not hold.
        self._lock = threading.Lock()

    def record_failure(self, issue_number: int, error: str) -> ProvisioningAttempt:
        """Spend one attempt of ``issue_number``'s budget."""
        with self._lock:
            state = self._issues.setdefault(issue_number, _IssueProvisioningState())
            state.failures = min(state.failures + 1, self.limit)
            state.last_error = error
            return self._attempt(issue_number, state)

    def record_success(self, issue_number: int) -> None:
        """Forget this issue: whatever was wrong with its worktree is not now."""
        self.forget(issue_number)

    def forget(self, issue_number: int) -> None:
        """Drop this issue's budget so the next attempt starts from a full one."""
        with self._lock:
            self._issues.pop(issue_number, None)

    def spent(self, issue_number: int) -> ProvisioningAttempt | None:
        """The exhausted budget for ``issue_number``, or ``None`` if it has one left."""
        with self._lock:
            state = self._issues.get(issue_number)
            if state is None or state.failures < self.limit:
                return None
            return self._attempt(issue_number, state)

    def announced(self, issue_number: int) -> bool:
        """Whether this issue's exhausted budget has been escalated to a human."""
        with self._lock:
            state = self._issues.get(issue_number)
            return state is not None and state.announcement is _Announcement.COMMITTED

    def begin_announcement(self, issue_number: int) -> bool:
        """Take the right to escalate this issue, or report that it is taken.

        Claiming and checking are ONE operation because the alternative is a
        check-then-announce the lock does not cover: two launches for the same
        issue — the ledger is keyed per issue precisely because a coding launch
        and a review launch share one environment and one escalation — would both
        read "not announced" and both post the operator comment.
        """
        with self._lock:
            state = self._issues.get(issue_number)
            if state is None or state.announcement is not _Announcement.NONE:
                return False
            state.announcement = _Announcement.CLAIMED
            return True

    def finish_announcement(self, issue_number: int, *, committed: bool) -> None:
        """Report what the claimed escalation did; a failed write frees the claim."""
        with self._lock:
            state = self._issues.get(issue_number)
            if state is None or state.announcement is not _Announcement.CLAIMED:
                return
            state.announcement = (
                _Announcement.COMMITTED if committed else _Announcement.NONE
            )

    def _attempt(
        self, issue_number: int, state: _IssueProvisioningState
    ) -> ProvisioningAttempt:
        return ProvisioningAttempt(
            issue_number=issue_number,
            attempts=state.failures,
            limit=self.limit,
            error=state.last_error,
        )


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
        escalation: ProvisioningEscalation,
        ledger: ProvisioningAttemptLedger | None = None,
    ) -> None:
        self._config = config
        self._command_runner = command_runner
        self._working_copy = working_copy
        self._escalation = escalation
        self._ledger = ledger or ProvisioningAttemptLedger()

    @property
    def has_commands(self) -> bool:
        """Whether this repository configures any provisioning commands."""
        return bool(self._config.setup_worktree)

    def provision(self, worktree_path: Path, *, issue_number: int) -> None:
        """Run the configured setup commands in ``worktree_path``.

        The candidate check runs on both outcomes. A setup command that alters
        the candidate and *then* fails would otherwise abort the launch with the
        alteration left in the worktree and never reported, so the two facts are
        gathered separately and both are named in the failure.

        ``issue_number`` is what the attempt budget is counted and escalated
        against. It is required rather than optional because a launch that
        cannot say which issue it is provisioning for cannot be bounded at all,
        and an unbounded path is the defect this budget closes (#54).

        Raises:
            ProvisioningAttemptsExhausted: this issue has already spent its
                consecutive-failure budget, or spent the last of it here. The
                recipe is NOT run in the former case.
            WorktreeProvisioningError: the recipe is not pinned outside the
                worktree, a command failed or timed out, or provisioning
                changed the candidate's committed state.
        """
        commands = list(self._config.setup_worktree)
        if not commands:
            return
        self._refuse_if_budget_spent(issue_number)
        failure = self._run_recipe(commands, worktree_path)
        if failure is None:
            # Whatever was wrong with this issue's environment is not wrong now,
            # so a later unrelated fault starts from a full budget (#54).
            self._ledger.record_success(issue_number)
            return
        raise self._bounded(self._ledger.record_failure(issue_number, str(failure)), failure)

    def _run_recipe(
        self, commands: list[str], worktree_path: Path
    ) -> WorktreeProvisioningError | None:
        """Run the pinned recipe; return why the worktree is not runnable, or ``None``.

        Refusals are RETURNED rather than raised so every reason a worktree is
        not runnable — including an unpinned recipe, which is as persistent as
        a missing toolchain — passes through the same attempt budget.
        """
        try:
            self._require_pinned_recipe(worktree_path)
        except WorktreeProvisioningError as unpinned:
            return unpinned
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
            return WorktreeProvisioningError(
                f"{setup_failure}; the candidate was also altered: {candidate_change}"
            )
        if setup_failure is not None:
            return setup_failure
        if candidate_change is not None:
            return WorktreeProvisioningError(candidate_change)
        return None

    def _refuse_if_budget_spent(self, issue_number: int) -> None:
        """Stop before the recipe runs when this issue has no attempts left.

        This is the half that ends the loop. Escalating and then provisioning
        anyway would keep re-running an ``npm ci`` that has already failed its
        way to a human; the point of the bound is that the next tick does not
        pay for it again.
        """
        spent = self._ledger.spent(issue_number)
        if spent is None:
            return
        if self._ledger.announced(issue_number) and not self._escalation.still_escalated(
            issue_number
        ):
            # A human cleared the escalation. That IS the retry request, and the
            # durable label - not this process's counter - is what says whether
            # an issue is still a human's. Without this the ledger would outlive
            # the escalation it raised and make it unclearable (#54).
            logger.info(
                "[launch] Provisioning escalation for #%d has been cleared; "
                "restoring its attempt budget",
                issue_number,
            )
            self._ledger.forget(issue_number)
            return
        self._announce(spent)
        raise ProvisioningAttemptsExhausted(
            f"provisioning for #{issue_number} failed {spent.attempts} consecutive "
            f"time(s) and is not being retried; last failure: {spent.error}",
            issue_number=issue_number,
            attempts=spent.attempts,
        )

    def _bounded(
        self, attempt: ProvisioningAttempt, failure: WorktreeProvisioningError
    ) -> WorktreeProvisioningError:
        """The error this failure becomes: retryable, or the end of retrying."""
        if not attempt.exhausted:
            logger.warning(
                "[launch] Provisioning failed for #%d (attempt %d of %d)",
                attempt.issue_number,
                attempt.attempts,
                attempt.limit,
            )
            return failure
        self._announce(attempt)
        return ProvisioningAttemptsExhausted(
            f"{failure}; provisioning has now failed {attempt.attempts} consecutive "
            "time(s) and is not being retried",
            issue_number=attempt.issue_number,
            attempts=attempt.attempts,
        )

    def _announce(self, attempt: ProvisioningAttempt) -> None:
        """Tell a human once, and keep retrying until that actually commits.

        The right to escalate is CLAIMED before the escalation runs, so a launch
        that arrives while another is mid-escalation says nothing rather than
        posting the operator a second copy of the same comment.
        """
        if not self._ledger.begin_announcement(attempt.issue_number):
            return
        logger.error(
            "[launch] Provisioning for #%d has failed %d consecutive time(s); "
            "escalating and no longer retrying it: %s",
            attempt.issue_number,
            attempt.attempts,
            attempt.error,
        )
        committed = self._escalation.escalate(attempt)
        self._ledger.finish_announcement(attempt.issue_number, committed=committed)
        if not committed:
            logger.error(
                "[launch] Could not escalate exhausted provisioning for #%d; the "
                "next launch attempt retries the escalation",
                attempt.issue_number,
            )

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
    one way for a coder and another way for a reviewer. The fresh coding and
    validation-retry paths keep their OWN failure handling rather than calling
    this, because a failure there must also clean up the pre-active worktree
    and release the claim those paths hold — but the attempt budget is spent
    inside the provisioner, so those paths are bounded (#54) whether or not
    they share this reporting shape.
    """
    try:
        provisioner.provision(worktree_path, issue_number=number)
    except Exception as e:
        log_transition(kind, number, "LAUNCHING", "FAILED", "setup commands failed")
        logger.error(issue_log(number, "FAILED: setup commands failed: %s"), e)
        events.publish(make_trace_event(
            EventName.SESSION_START_FAILED,
            {
                "issue_number": number,
                "session_name": session_name,
                **provisioning_failure_facts(e),
            },
        ))
        return LaunchResult(None, False, f"Setup commands failed: {e}")
    return None


def provisioning_failure_facts(error: BaseException) -> dict[str, object]:
    """What a launch-failed event says about a provisioning failure.

    ``attempts_exhausted`` is the fact a reader could not otherwise recover:
    whether the next tick will try this launch again, or whether the retrying
    has stopped and the issue is now a human's (#54). Built here so every path
    that reports one reports the same thing.
    """
    return {
        "reason": "setup_commands_failed",
        "attempts_exhausted": isinstance(error, ProvisioningAttemptsExhausted),
        "error": str(error),
    }


def build_launch_provisioner(
    *,
    config: Config,
    command_runner: CommandRunner,
    working_copy: WorkingCopy,
    apply_actions: ApplyActions,
    label_manager: LabelManager,
    events: EventSink,
    read_labels: Callable[[int], Sequence[str]],
) -> WorktreeProvisioner:
    """The provisioner a launcher uses: the recipe, its bound, and its escalation.

    A launcher wants one collaborator, not three. Assembling them here also
    keeps the escalation's wiring — which label, under which cause, read from
    where — beside the owner that decides when to raise it.
    """
    return WorktreeProvisioner(
        config=config,
        command_runner=command_runner,
        working_copy=working_copy,
        escalation=build_provisioning_escalation(
            apply_actions=apply_actions,
            label_manager=label_manager,
            events=events,
            read_labels=read_labels,
        ),
    )


@dataclass(frozen=True, slots=True)
class NeedsHumanProvisioningEscalation:
    """How an exhausted provisioning budget reaches a human in this repository.

    The same shape a failed rework worktree already uses — the shared
    ``needs-human`` block under ``SESSION_LIFECYCLE``, an operator comment, and
    the ``issue.needs_human`` event — because it is the same kind of fact: a
    launch-time environment failure a human, not another tick, has to fix.

    The DURABLE half is the label alone: it removes the issue from selection, a
    later launch reads it to decide whether the refusal still stands, and a
    human clears it to ask for a retry. The comment only explains it. So the
    label is applied first, on its own, and it alone decides whether this
    escalation committed — a comment that failed on an issue already correctly
    labelled must not send the next launch round the whole escalation again and
    post the operator a second copy. The event follows that durable commit (the
    discipline the claim quarantine keeps): a dashboard warning whose label
    never landed disappears on restart and takes the only signal with it.
    """

    apply_actions: ApplyActions
    label_manager: LabelManager
    events: EventSink
    #: Live label read for one issue. Must be the UNCACHED one: a stale answer
    #: gets this wrong in the expensive direction, leaving an issue refused
    #: after the human fixed it.
    read_labels: Callable[[int], Sequence[str]]

    def escalate(self, attempt: ProvisioningAttempt) -> bool:
        """Block the issue, explain why, and say whether the block landed."""
        if not self.apply_actions(
            [
                AddLabelAction(
                    issue_number=attempt.issue_number,
                    label=self.label_manager.needs_human,
                    reason=_ESCALATION_REASON,
                    needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                )
            ],
            context=PROVISIONING_ESCALATION_CONTEXT,
        ):
            return False
        self._explain(attempt)
        self.events.publish(make_trace_event(
            EventName.ISSUE_NEEDS_HUMAN,
            {
                "issue_number": attempt.issue_number,
                "reason": PROVISIONING_ESCALATION_CONTEXT,
                "attempts": attempt.attempts,
                "limit": attempt.limit,
                "error": attempt.error,
            },
        ))
        return True

    def _explain(self, attempt: ProvisioningAttempt) -> None:
        """Post the operator comment, best effort: the block already holds."""
        if self.apply_actions(
            [
                AddCommentAction(
                    number=attempt.issue_number,
                    comment=_provisioning_escalation_comment(attempt),
                    reason=_ESCALATION_REASON,
                )
            ],
            context=PROVISIONING_ESCALATION_CONTEXT,
        ):
            return
        logger.warning(
            "[launch] #%d is blocked for exhausted provisioning but its "
            "explanatory comment did not post; the label and the "
            "issue.needs_human event still carry the reason",
            attempt.issue_number,
        )

    def still_escalated(self, issue_number: int) -> bool:
        try:
            return self.label_manager.needs_human in frozenset(
                self.read_labels(issue_number)
            )
        except Exception:
            logger.exception(
                "[launch] Could not read labels for #%d; treating its "
                "provisioning escalation as still in force",
                issue_number,
            )
            # Fails CLOSED, the same direction the shared block reads in:
            # wrongly keeping the refusal costs one launch, wrongly dropping
            # it puts the repository back in the loop this bound removed.
            return True


def build_provisioning_escalation(
    *,
    apply_actions: ApplyActions,
    label_manager: LabelManager,
    events: EventSink,
    read_labels: Callable[[int], Sequence[str]],
) -> NeedsHumanProvisioningEscalation:
    """Assemble the needs-human escalation from launcher collaborators."""
    return NeedsHumanProvisioningEscalation(
        apply_actions=apply_actions,
        label_manager=label_manager,
        events=events,
        read_labels=read_labels,
    )


def _provisioning_escalation_comment(attempt: ProvisioningAttempt) -> str:
    """What an operator reads: what stopped, why retrying will not fix it, what to do."""
    return (
        "🛠️ **Worktree provisioning failed and is no longer being retried**\n\n"
        f"The configured `setup_worktree` recipe failed on {attempt.attempts} "
        f"consecutive launch attempt(s) for this issue (the bound is "
        f"{attempt.limit}).\n\n"
        f"Last failure:\n\n```\n{attempt.error}\n```\n\n"
        "A provisioning failure is normally environmental — a missing "
        "toolchain, a broken lockfile, an unreachable package registry — so "
        "another attempt would re-run the same recipe and fail the same way "
        "while holding a session slot. No further session for this issue will "
        "provision a worktree until a human clears this label.\n\n"
        "Fix the environment (or the recipe), then remove the label to retry."
    )
