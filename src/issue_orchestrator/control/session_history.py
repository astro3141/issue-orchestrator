"""Session history ownership helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable, MutableSequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from ..domain.models import (
    ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES,
    AwaitingMergeTerminalStatus,
    BLOCKED_HISTORY_STATUSES,
    RECONCILABLE_HISTORY_STATUSES,
    SessionHistoryEntry,
    SessionHistoryStatus,
)


logger = logging.getLogger(__name__)


CLOSED_ISSUE_HISTORY_STATUS_REASON = "Issue closed; history reconciled"


@dataclass(frozen=True)
class HistoryReconciliationMutation:
    """Details of an applied history reconciliation mutation."""

    issue_number: int
    pr_url: str
    previous_status: SessionHistoryStatus
    status: AwaitingMergeTerminalStatus
    status_reason: str


HistoryReconciliationNoopReason: TypeAlias = Literal["missing", "not_reconcilable"]


@dataclass(frozen=True)
class HistoryReconciliationNoop:
    """Details of a history reconciliation no-op."""

    issue_number: int
    pr_url: str
    reason: HistoryReconciliationNoopReason
    current_status: SessionHistoryStatus | None = None


HistoryReconciliationResult: TypeAlias = (
    HistoryReconciliationMutation | HistoryReconciliationNoop
)


@dataclass(frozen=True)
class ClosedIssueHistoryMutation:
    """Details of a history mutation after a tracked issue closed."""

    issue_number: int
    previous_status: SessionHistoryStatus
    status: AwaitingMergeTerminalStatus
    status_reason: str


ClosedIssueHistoryNoopReason: TypeAlias = Literal["missing", "already_terminal"]


@dataclass(frozen=True)
class ClosedIssueHistoryNoop:
    """Details of a closed-issue history reconciliation no-op."""

    issue_number: int
    reason: ClosedIssueHistoryNoopReason
    current_status: SessionHistoryStatus | None = None


ClosedIssueHistoryResult: TypeAlias = (
    ClosedIssueHistoryMutation | ClosedIssueHistoryNoop
)


ISSUE_CLOSED_RECONCILABLE_HISTORY_STATUSES: frozenset[SessionHistoryStatus] = (
    BLOCKED_HISTORY_STATUSES | RECONCILABLE_HISTORY_STATUSES
)


@dataclass(frozen=True)
class ClaimReleaseResult:
    """What :meth:`SessionHistoryOwner.release_claim` actually released (#195).

    Reports the number of entries whose duplicate-launch claim was dropped, so
    a caller can tell a real release from a no-op; the statuses they held, so
    the release can be audited against what the session actually did; and how
    many abandoned releases this run has now spent on the issue, which is the
    number the relaunch budget is measured against.
    """

    issue_number: int
    released_entries: int
    statuses: tuple[SessionHistoryStatus, ...]
    releases_granted: int


class SessionHistoryOwner:
    """Owns controlled mutations of session history entries.

    The owner resolves the session-history sequence *at mutation time* rather
    than capturing a list reference at construction. Recovery, publish-retry
    finalization, and reset paths replace ``state.session_history`` with a
    brand-new list object (see ``retry_history_state`` and
    ``publish_retry_finalize``). An owner that captured the original list would
    then mutate an orphaned collection while awaiting-merge discovery and the
    dashboard projection read the replacement list -- the split brain that left
    closed/merged PR-backed entries stuck in the Awaiting Merge lane (#6692).

    Construct with either:

    - a concrete ``MutableSequence`` -- backward compatible; correct for
      short-lived owners built per operation immediately before use, and for
      tests; or
    - a zero-argument provider returning the current sequence -- for long-lived
      owners bound to mutable orchestrator state, e.g.
      ``SessionHistoryOwner(lambda: state.session_history)``.
    """

    _history_provider: Callable[[], MutableSequence[SessionHistoryEntry]]

    def __init__(
        self,
        session_history: (
            MutableSequence[SessionHistoryEntry]
            | Callable[[], MutableSequence[SessionHistoryEntry]]
        ),
    ) -> None:
        if callable(session_history):
            self._history_provider = session_history
        else:
            captured = session_history
            self._history_provider = lambda: captured

    @property
    def session_history(self) -> MutableSequence[SessionHistoryEntry]:
        """Resolve the current session-history sequence at access time."""
        return self._history_provider()

    def reconcile_awaiting_merge(
        self,
        *,
        issue_number: int,
        pr_url: str,
        status: AwaitingMergeTerminalStatus,
        status_reason: str,
        before_transition: Callable[[SessionHistoryEntry], None] | None = None,
    ) -> HistoryReconciliationResult:
        """Mark the latest matching awaiting-merge history entry terminal.

        ``before_transition`` lets a durable owner record facts derived from
        the reconcilable entry before this process-local projection becomes
        terminal. An exception leaves the entry unchanged so reconciliation
        can retry without losing the durable fact.
        """
        entry = self._find_latest_matching_entry(issue_number, pr_url)
        if entry is None:
            # Likely cause: pr_url string mismatch (trailing slash, scheme, etc.)
            # — not necessarily an actually-missing history row.
            known_for_issue = [
                e.pr_url for e in self.session_history if e.issue_number == issue_number
            ]
            logger.warning(
                "reconcile_awaiting_merge: no entry for issue=#%d pr_url=%r; "
                "known pr_urls for issue=%r",
                issue_number,
                pr_url,
                known_for_issue,
            )
            return HistoryReconciliationNoop(
                issue_number=issue_number,
                pr_url=pr_url,
                reason="missing",
            )
        if entry.status not in RECONCILABLE_HISTORY_STATUSES:
            logger.info(
                "reconcile_awaiting_merge: not reconcilable issue=#%d pr_url=%s "
                "current_status=%s (expected one of %s)",
                issue_number,
                pr_url,
                entry.status,
                sorted(RECONCILABLE_HISTORY_STATUSES),
            )
            return HistoryReconciliationNoop(
                issue_number=issue_number,
                pr_url=pr_url,
                reason="not_reconcilable",
                current_status=entry.status,
            )

        previous_status = entry.status
        if before_transition is not None:
            before_transition(entry)
        entry.status = status
        entry.status_reason = status_reason
        logger.info(
            "reconcile_awaiting_merge: mutated issue=#%d pr_url=%s %s -> %s (%s)",
            issue_number,
            pr_url,
            previous_status,
            status,
            status_reason,
        )
        return HistoryReconciliationMutation(
            issue_number=issue_number,
            pr_url=pr_url,
            previous_status=previous_status,
            status=status,
            status_reason=status_reason,
        )

    def reconcile_closed_issue(
        self,
        *,
        issue_number: int,
        status_reason: str,
    ) -> ClosedIssueHistoryResult:
        """Mark the latest retry-blocking history entry terminal when its issue closed."""
        entry = self._find_latest_issue_entry(issue_number)
        if entry is None:
            logger.info(
                "reconcile_closed_issue: no history entry for issue=#%d", issue_number
            )
            return ClosedIssueHistoryNoop(issue_number=issue_number, reason="missing")
        if entry.status not in ISSUE_CLOSED_RECONCILABLE_HISTORY_STATUSES:
            logger.info(
                "reconcile_closed_issue: already terminal issue=#%d status=%s",
                issue_number,
                entry.status,
            )
            return ClosedIssueHistoryNoop(
                issue_number=issue_number,
                reason="already_terminal",
                current_status=entry.status,
            )

        previous_status = entry.status
        entry.status = "closed"
        entry.status_reason = status_reason
        logger.info(
            "reconcile_closed_issue: mutated issue=#%d %s -> closed (%s)",
            issue_number,
            previous_status,
            status_reason,
        )
        return ClosedIssueHistoryMutation(
            issue_number=issue_number,
            previous_status=previous_status,
            status="closed",
            status_reason=status_reason,
        )

    def claiming_issue_numbers(self) -> frozenset[int]:
        """Issues this run's history still holds a duplicate-launch claim on.

        The session-derived half of ``QueueCache.evaluate_issue``'s guard, and
        the planner's "already had a session this run" gate, read THIS rather
        than every entry (#195). An entry whose claim has been released stays
        in the history for the operator to read, but stops answering "this run
        already worked the issue" — that is the whole difference between the
        record and the claim.
        """
        return frozenset(
            entry.issue_number
            for entry in self.session_history
            if not entry.claim_released
        )

    def abandoned_after_completion_issue_numbers(self) -> frozenset[int]:
        """Issues whose history says the last session left no owner behind (#195).

        The LATEST still-claiming entry decides, in the same spirit as every
        other question this owner answers (:meth:`_find_latest_issue_entry`).
        An issue that failed publication once and later completed with a PR is
        owned by the awaiting-merge reconciler now, and must not read as
        abandoned because an older row still says ``validation_failed``. An
        issue whose claim is ALREADY released is not abandoned either — it has
        been given back, so naming it again would release it every tick.

        History-only: whether anything ELSE currently owns the issue — a
        running session, a live control operation — is the queue owner's
        question, not this one's.
        """
        latest_by_issue: dict[int, SessionHistoryStatus] = {}
        for entry in self.session_history:
            if entry.claim_released:
                latest_by_issue.pop(entry.issue_number, None)
                continue
            latest_by_issue[entry.issue_number] = entry.status
        return frozenset(
            issue_number
            for issue_number, status in latest_by_issue.items()
            if status in ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES
        )

    def abandoned_releases_granted(self, issue_number: int) -> int:
        """How many times this run has already handed the issue back (#195).

        The relaunch budget's counter, and it needs no state of its own: the
        release marks the entries it retires, so the entries that carry BOTH a
        released claim and an abandoned-after-completion status are exactly the
        automatic attempts this run has already granted. Entries released for
        any other reason are excluded, so a future release path cannot silently
        consume this budget.
        """
        return sum(
            1
            for entry in self.session_history
            if entry.issue_number == issue_number
            and entry.claim_released
            and entry.status in ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES
        )

    def release_claim(self, issue_number: int) -> ClaimReleaseResult:
        """Give an abandoned issue back to scheduling, keeping its record (#195).

        The in-memory counterpart of what a restart used to be needed for:
        ``session_history`` is per-process, so restarting dropped every claim
        at once and the next tick could reach the next attempt. This drops the
        claim for ONE issue that has provably lost its owner, and drops nothing
        else — the status, the reason, the PR URL and the worktree path all
        stay, so the dashboard and failure diagnosis still see the session that
        failed.

        Nothing durable is touched, so no allowance is created or refunded:
        labels, attempt receipts and failure counters are untouched, and the
        scheduler re-decides from them on the next pass.

        The release is NOT where the relaunch budget is enforced. The budget
        bounds how many releases carry an automatic next attempt with them, and
        the exhausting one still has to happen -- it simply arrives with a
        blocking label, which the caller plants first. Refusing the mutation
        here would leave the issue stranded behind a stale ``in-progress``
        label with no escalation, which is the failure #195 exists to remove.
        What this reports is the counter itself, so the decision is auditable
        from the outcome (see :class:`ClaimReleaseResult`).
        """
        released = [
            entry
            for entry in self.session_history
            if entry.issue_number == issue_number and not entry.claim_released
        ]
        for entry in released:
            entry.claim_released = True
        granted = self.abandoned_releases_granted(issue_number)
        logger.info(
            "release_claim: issue=#%d released=%d entry/entries "
            "(record retained, abandoned releases granted this run=%d)",
            issue_number,
            len(released),
            granted,
        )
        return ClaimReleaseResult(
            issue_number=issue_number,
            released_entries=len(released),
            statuses=tuple(entry.status for entry in released),
            releases_granted=granted,
        )

    def _find_latest_matching_entry(
        self,
        issue_number: int,
        pr_url: str,
    ) -> SessionHistoryEntry | None:
        # The newest matching entry is canonical; do not fall back to older
        # duplicate PR history once the latest matching row is terminal.
        for entry in reversed(self.session_history):
            if entry.issue_number != issue_number:
                continue
            if entry.pr_url != pr_url:
                continue
            return entry
        return None

    def _find_latest_issue_entry(
        self,
        issue_number: int,
    ) -> SessionHistoryEntry | None:
        for entry in reversed(self.session_history):
            if entry.issue_number == issue_number:
                return entry
        return None
