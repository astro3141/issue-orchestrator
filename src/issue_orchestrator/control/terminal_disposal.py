"""Own the terminal disposal a finished session has already earned.

Disposal is the last step of a session's lifecycle: its terminal tab is closed
and its worktree removed under the configured cleanup policy. This module owns
all three parts of it —

- :func:`immediate_disposal_actions`, which turns the immediate cleanup facts a
  completion handoff filed into :class:`~.actions.CleanupSessionAction`. The
  :class:`~.planner.Planner` calls it on an ordinary planning tick;
- :class:`TerminalTeardown`, the disposal itself — close the tab, remove the
  checkout, announce it — wrapped by :class:`SessionDisposal` for an ordinary
  tick and by :class:`PausedSessionDisposal` for a paused one. The
  :class:`~.action_applier.ActionApplier` holds both and applies through them;
- :class:`PausedTerminalDisposal`, which runs the same two steps on a **paused**
  tick, where planning does not run at all (#167).

Pause is a barrier to admitting new work, not a freeze of work already admitted
(#161). A session that was already running when the pause took effect may still
reach terminal, and the disposal it earned must not be stranded behind the
planner's pause gate: otherwise a paused engine holds the finished session's
worktree until an operator either resumes — which reopens continuation
execution — or performs state surgery.

Three properties keep the paused pass from becoming a way to run planning.

**It disposes, and does nothing else.** It can only ever hand a
:class:`~.actions.CleanupSessionAction` to :class:`PausedSessionDisposal`. It admits
no #139 revalidation, reserves no #149 continuation run, cuts no checkout, opens
no reviewer exchange and creates no pull request, and no queued Actor, rework,
review, label or tech-lead action becomes reachable through it.

**It disposes only what a terminal session already earned.** Its input is the
immediate-cleanup fact the completion handoff files for a session that reached
a terminal status — never the deferred, PR-reviewed cleanup queue, whose
"has the PR been reviewed yet?" question is a live review-workflow decision and
stays behind the pause gate. Every existing guard on that fact still applies:
tech-lead artifact holds still withhold disposal, and a normal coding worktree
is still removed non-forced so uncommitted work is never silently discarded.

**It cannot cancel.** Ordinary disposal tears the issue's review exchange down on
its way past, and a pause must not become a teardown. This is not enforced by
the paused path being careful — a liveness check followed by a cancellation is
a check-then-act race, and in-flight work admitted before the pause (#161) can
become live inside that window and be killed by a probe that saw nothing.

It is enforced by capability instead. Cancellation lives in
:func:`~.review_exchange_lifecycle.cancel_issue_review_exchange`, which takes the
pair registry and the job supervisor as arguments. :class:`TerminalTeardown`
holds neither, and :class:`PausedSessionDisposal` holds only a
:class:`TerminalTeardown` and a closed-over read-only liveness predicate
(:func:`~.review_exchange_lifecycle.live_review_exchange_probe`). No reference
reachable from the paused path can release a pair or cancel a job, so no
interleaving produces a cancellation — there is no call to be raced into.

The liveness predicate remains, purely as a fail-safe withholding guard in front
of the non-cancelling teardown: an issue whose exchange still looks live keeps
its tab and its checkout too, because a disposal is part of that issue's
lifecycle and should not run underneath work that is still using it. Withholding
leaves the fact in place: the disposal happens once that work reaches terminal,
or once the engine resumes and ordinary planning takes it.

Nothing is lost by not cancelling while paused. ``has_active_pair`` reports true
for exactly the membership ``release`` pops, and ``has_matching`` for exactly the
jobs ``cancel_matching`` cancels, so on the only branch the paused path ever
reaches the cancellation it does not perform would have been a no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from ..domain.models import RETROSPECTIVE_REVIEW_TERMINAL_PREFIX
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .action_results import ActionResult
from .actions import CleanupSessionAction
from .completion_cleanup_state import CompletionCleanupStateOwner
from .review_exchange_lifecycle import cancel_issue_review_exchange
from .session_manager import SessionManager, SessionRef, SessionType

if TYPE_CHECKING:
    from ..domain.models import CleanupFacts, OrchestratorState
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from ..ports.worktree_manager import WorktreeManager
    from .background_job_supervisor import BackgroundJobSupervisor
    from .review_exchange_lifecycle import ReviewExchangeCancellation

logger = logging.getLogger(__name__)


def immediate_disposal_actions(facts: "CleanupFacts") -> list[CleanupSessionAction]:
    """Turn every immediate cleanup fact that may be disposed now into an action.

    Immediate cleanups are the sessions that reached terminal and need no
    review workflow first. They are ready EXCEPT for run assets that pending or
    active tech_lead work still references (#6771, #6780): disposing those
    before the investigation or health review launches deletes the artifact
    hints it was queued to read. The hold set is read by the cleanup-fact
    gatherer from the tech-lead-problem-artifact owner, which is also what
    retains these entries across the end-of-tick fact clear; they are re-planned
    once the hold releases.
    """
    actions: list[CleanupSessionAction] = []
    for cleanup in facts.immediate_cleanups:
        if cleanup.issue_number in facts.held_issue_numbers:
            logger.info(
                "Holding cleanup for issue #%d — pending or active tech_lead "
                "work still references its run assets",
                cleanup.issue_number,
            )
            continue
        actions.append(CleanupSessionAction(
            issue_number=cleanup.issue_number,
            pr_number=0,  # No PR for immediate cleanups
            terminal_id=cleanup.terminal_id,
            worktree_path=cleanup.worktree_path,
            close_tabs=facts.close_tabs,
            # A disposable tech-lead-investigation scratch worktree is always
            # removed, even when the config keeps worktrees (#6823).
            remove_worktrees=facts.remove_worktrees or cleanup.scratch_worktree,
            # Carry disposable identity so the applier force-removes ONLY the
            # scratch worktree (leftover artifacts must not leak it) (#6824 F8).
            disposable_worktree=cleanup.scratch_worktree,
            reason=f"session {cleanup.reason}",
        ))
    return actions


@dataclass(frozen=True)
class TerminalTeardown:
    """The disposal itself: close the tab, remove the checkout, announce it.

    The execution half of this module, held by the applier rather than spread
    through it: closing a terminal tab and removing a checkout is one operation
    on one finished session, and the rule about WHICH removal (forced only for
    a disposable scratch checkout) belongs with it.

    It holds no review-exchange owner. The pair registry and the background job
    supervisor are not fields here, and nothing that IS a field exposes them, so
    no call reachable through this object can release a pair or cancel a job.
    That is what lets :class:`PausedSessionDisposal` — which holds this and a
    read-only predicate and nothing else — be structurally incapable of
    cancellation rather than merely careful about it (#167).
    """

    sessions: SessionManager
    events: EventSink
    worktree_manager: Optional["WorktreeManager"] = None
    on_worktree_removed: Optional[Callable[[str], int]] = None

    def tear_down(self, action: CleanupSessionAction) -> "TeardownOutcome":
        """Close the tab, remove the checkout, announce the cleanup."""
        errors: list[str] = []
        self._close_terminal(action, errors)
        self._remove_worktree(action, errors)
        self._announce(action)
        return TeardownOutcome(tuple(errors))

    def _announce(self, action: CleanupSessionAction) -> None:
        """Publish the cleanup event; a failed announcement is not a failed
        disposal.

        Same reasoning as the worktree-removed callback below: the tab and the
        checkout are already gone by this point, so re-failing the action would
        retain the cleanup fact and retry a disposal that has nothing left to
        do. Guarding here also means no unguarded call remains on the paused
        path, which runs outside ``ActionApplier.apply``'s catch-all and would
        otherwise skip the tick heartbeat on a raise (#6824 R3, #167 N1).
        """
        try:
            self.events.publish(make_trace_event(
                EventName.CLEANUP_COMPLETED,
                {"issue_number": action.issue_number, "pr_number": action.pr_number},
            ))
        except Exception as e:
            logger.warning(
                issue_log(
                    action.issue_number,
                    "cleanup event publish failed (disposal already done): %s",
                ),
                e,
            )

    def _close_terminal(
        self, action: CleanupSessionAction, errors: list[str]
    ) -> None:
        """Close the terminal session if the configured policy says to."""
        if not (action.close_tabs and action.terminal_id):
            return
        try:
            ref = SessionRef(
                session_type=session_type_of(action.terminal_id),
                number=action.issue_number,
            )
            if self.sessions.exists(ref):
                self.sessions.stop(ref)
                logger.info(issue_log(action.issue_number, "Closed terminal session"))
        except Exception as e:
            errors.append(f"close session: {e}")
            logger.warning(issue_log(action.issue_number, "Failed to close session: %s"), e)

    def _remove_worktree(
        self, action: CleanupSessionAction, errors: list[str]
    ) -> None:
        """Remove the checkout if the configured policy says to."""
        if not (action.remove_worktrees and action.worktree_path):
            return
        if not self.worktree_manager:
            errors.append("no worktree_manager configured")
            return
        try:
            # Force removal ONLY for a disposable scratch worktree: it holds
            # throwaway agent artifacts, so a leftover untracked file must not
            # make ``git worktree remove`` fail (exit 128) and leak it. A normal
            # coding worktree stays non-forced so user work is never discarded
            # (#6824 F8).
            remove_worktree = self.worktree_manager.remove_checkout
            if action.disposable_worktree:
                remove_worktree = self.worktree_manager.remove_checkout_and_branch
            remove_worktree(Path(action.worktree_path), force=action.disposable_worktree)
            logger.info(issue_log(action.issue_number, "Removed worktree: %s"), action.worktree_path)
        except Exception as e:
            errors.append(f"remove worktree: {e}")
            logger.warning(issue_log(action.issue_number, "Failed to remove worktree: %s"), e)
            return
        # Removal SUCCEEDED (or the path was already gone). The "worktree is gone"
        # notification is a distinct concern: a callback failure must NOT re-fail
        # an already-completed removal, or the disposable cleanup would be retained
        # and retried forever against a now-absent path (#6824 R3).
        if self.on_worktree_removed:
            try:
                self.on_worktree_removed(action.worktree_path)
            except Exception as e:
                logger.warning(
                    issue_log(action.issue_number, "worktree-removed callback failed (worktree already gone): %s"),
                    e,
                )


@dataclass(frozen=True)
class TeardownOutcome:
    """What :meth:`TerminalTeardown.tear_down` could not do.

    Shaping the :class:`ActionResult` lives here so both disposal paths report
    a completed-or-failed teardown identically, and differ only in the details
    they attach.
    """

    errors: tuple[str, ...] = ()

    def as_result(
        self,
        action: CleanupSessionAction,
        **details: str | int | bool | list[str] | None,
    ) -> ActionResult:
        if self.errors:
            return ActionResult.fail(action, "; ".join(self.errors), **details)
        return ActionResult.ok(action, **details)


@dataclass(frozen=True)
class SessionDisposal:
    """Ordinary disposal: the issue's review-exchange teardown, then the tab and
    the checkout.

    Unconditional, because the plan that produced the cleanup owns everything
    else happening to that issue this tick — so the exchange is torn down on the
    way past. This is the ONLY disposal path that holds the review-exchange
    owners, and therefore the only one that can cancel.
    """

    teardown: TerminalTeardown
    pair_registry: Optional["PersistentExchangePairRegistry"] = None
    job_supervisor: Optional["BackgroundJobSupervisor"] = None

    def apply(self, action: CleanupSessionAction) -> ActionResult:
        """Dispose of the session: lifecycle teardown, tab, checkout."""
        cancellation = self._cancel_review_exchange(action)
        return self.teardown.tear_down(action).as_result(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            review_exchange_lifecycle_checked=cancellation is not None,
            cancelled_review_exchange_jobs=list(cancellation.cancelled_job_ids)
            if cancellation is not None
            else [],
        )

    def _cancel_review_exchange(
        self, action: CleanupSessionAction
    ) -> "ReviewExchangeCancellation | None":
        ref = _session_ref(action)
        if ref.session_type not in {SessionType.ISSUE, SessionType.REWORK}:
            return None
        return cancel_issue_review_exchange(
            issue_number=ref.number,
            reason="session-cleanup",
            pair_registry=self.pair_registry,
            job_supervisor=self.job_supervisor,
        )


@dataclass(frozen=True)
class PausedSessionDisposal:
    """The same disposal for a paused engine, with cancellation out of reach.

    A pause is a barrier to starting work, never a cancellation (#161). Work
    admitted before the pause keeps running and must finish on its own terms —
    including in the instants either side of any check this path performs. So
    the paused path does not decide *whether* to cancel; it has nothing to
    cancel with. Its two collaborators are a :class:`TerminalTeardown`, which
    holds no review-exchange owner, and ``review_exchange_is_live``, a closure
    over those owners exposing one boolean question and no way to mutate them
    (:func:`~.review_exchange_lifecycle.live_review_exchange_probe`).
    :func:`~.review_exchange_lifecycle.cancel_issue_review_exchange` takes the
    owners as arguments and cannot be reached from either.

    The predicate is a fail-safe withholding guard only: it decides whether the
    tab and the checkout may be disposed at all, not whether something gets torn
    down. A liveness change immediately after it is answered therefore costs at
    most one deferred disposal, never a cancelled exchange. Withholding defers
    rather than fails: the caller keeps the fact and retries once that work
    reaches terminal, or leaves it for ordinary planning after a resume.
    """

    teardown: TerminalTeardown
    review_exchange_is_live: Callable[[int], bool]

    def apply(self, action: CleanupSessionAction) -> ActionResult:
        if self.review_exchange_is_live(action.issue_number):
            logger.info(issue_log(
                action.issue_number,
                "Withholding paused terminal disposal: review-exchange work "
                "predating the pause is still live",
            ))
            return ActionResult.skip(
                action, "review exchange still live; pause is not cancellation"
            )
        return self.teardown.tear_down(action).as_result(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            # Machine-readable proof of the contract, not a summary of what was
            # attempted: this path cannot run the cancellation lifecycle, so a
            # consumer reading these two keys can tell a paused disposal from an
            # ordinary one and never sees a cancelled job from a paused tick.
            review_exchange_lifecycle_checked=False,
            cancelled_review_exchange_jobs=[],
        )


def _session_ref(action: CleanupSessionAction) -> SessionRef:
    if action.terminal_id:
        return SessionRef(
            session_type=session_type_of(action.terminal_id),
            number=action.issue_number,
        )
    logger.warning(
        "[APPLIER] CleanupSessionAction missing terminal_id; assuming "
        "issue session for review-exchange cleanup issue=%s pr=%s worktree=%s",
        action.issue_number,
        action.pr_number,
        action.worktree_path or "(none)",
    )
    return SessionRef(session_type=SessionType.ISSUE, number=action.issue_number)


def session_type_of(terminal_id: str) -> SessionType:
    """The session type a terminal id names."""
    if terminal_id.startswith(RETROSPECTIVE_REVIEW_TERMINAL_PREFIX):
        return SessionType.RETROSPECTIVE_REVIEW
    if terminal_id.startswith("review-"):
        return SessionType.REVIEW
    if terminal_id.startswith("rework-"):
        return SessionType.REWORK
    if terminal_id.startswith("tech-lead-"):
        return SessionType.TECH_LEAD
    return SessionType.ISSUE


class TerminalDisposalFacts(Protocol):
    """The fact source a paused disposal pass may read.

    Deliberately narrower than the full cleanup-fact gatherer: a paused pass
    must not read the deferred cleanup queue, and must not make the repository
    call that queue's reviewed-PR question needs.
    """

    def gather_terminal_disposal_facts(
        self, state: "OrchestratorState"
    ) -> "CleanupFacts | None": ...


class TerminalDisposalSeam(Protocol):
    """The single action a paused disposal pass may execute."""

    def dispose_terminal_session(
        self, action: CleanupSessionAction
    ) -> "ActionResult": ...


@dataclass(frozen=True)
class PausedDisposal:
    """What one paused disposal pass did, by issue number."""

    disposed: tuple[int, ...] = ()
    withheld: tuple[int, ...] = ()

    @property
    def acted(self) -> bool:
        return bool(self.disposed or self.withheld)


class PausedTerminalDisposal:
    """Run the terminal disposal a finished session earned, while paused (#167).

    Typed to the two things it needs and nothing else: a fact source that only
    knows terminal-disposal facts, and a seam that only disposes. There is no
    reachable path from here to any other planned action.
    """

    def __init__(
        self,
        *,
        state: "OrchestratorState",
        facts: TerminalDisposalFacts,
        seam: TerminalDisposalSeam,
    ) -> None:
        self._state = state
        self._facts = facts
        self._seam = seam

    def dispose(self) -> PausedDisposal:
        """Dispose every eligible terminal session; consume what was disposed.

        A disposal that did not happen — the seam refused because the issue's
        review exchange is still live, or the removal itself failed — leaves its
        fact in place, so the next paused tick retries it and a resume finds it
        exactly where ordinary planning expects it. A disposal that DID happen
        is consumed here, because a paused tick clears no facts of its own.
        """
        facts = self._facts.gather_terminal_disposal_facts(self._state)
        if facts is None:
            return PausedDisposal()

        cleanup_queue = CompletionCleanupStateOwner(self._state)
        disposed: list[int] = []
        withheld: list[int] = []
        for action in immediate_disposal_actions(facts):
            result = self._seam.dispose_terminal_session(action)
            if not result.success:
                withheld.append(action.issue_number)
                continue
            disposed.append(action.issue_number)
            cleanup_queue.discard_immediate(
                action.issue_number, action.worktree_path
            )

        outcome = PausedDisposal(tuple(disposed), tuple(withheld))
        if outcome.acted:
            logger.info(
                "[PAUSED_DISPOSAL] disposed=%s withheld=%s — the engine stays "
                "paused and starts nothing as a consequence",
                list(outcome.disposed) or "none",
                list(outcome.withheld) or "none",
            )
        return outcome
