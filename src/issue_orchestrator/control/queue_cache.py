"""Queue cache mutations and eligibility policy.

This module centralizes queue eligibility and mutations so call sites
cannot bypass scope policy when updating ``state.cached_queue_issues``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import time
import traceback
from typing import TYPE_CHECKING

from .issue_scope import evaluate_issue_scope, issue_scope_skip_detail, outside_single_issue_scope
from .session_history import SessionHistoryOwner

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..domain.models import OrchestratorState
    from ..ports.issue import Issue
    from ..ports.queue_cache_store import QueueCacheStore

logger = logging.getLogger(__name__)


class QueueMutationStatus(str, Enum):
    """Result status for queue cache mutation operations."""

    ACCEPTED = "accepted"
    REJECTED_OUT_OF_SCOPE = "rejected_out_of_scope"
    REJECTED_EXCLUDED = "rejected_excluded"


@dataclass(frozen=True)
class QueueMutationOutcome:
    """Outcome details from queue cache mutations."""

    status: QueueMutationStatus
    in_queue: bool
    updated: bool


_UI_VISIBILITY_STALENESS_SECONDS = 120
_SUSPICIOUS_SHRINK_MIN_REMOVALS = 10
_SUSPICIOUS_SHRINK_MIN_RATIO = 0.5
QUEUE_SHRINK_CONFIRM_DELAY_SECONDS = 60.0


class QueueCache:
    """Only writer for queue cache state."""

    def __init__(
        self,
        config: "Config",
        state: "OrchestratorState",
        queue_cache_store: "QueueCacheStore | None" = None,
    ):
        self._config = config
        self._state = state
        self._store = queue_cache_store

    def _history_owner(self) -> SessionHistoryOwner:
        """The owner of every question this cache asks of session history.

        Bound to live state rather than to the current list object: recovery,
        publish-retry finalization and reset paths replace
        ``state.session_history`` wholesale, and this cache outlives some of
        those calls.
        """
        return SessionHistoryOwner(lambda: self._state.session_history)

    def replace_from_refresh(self, issues: list["Issue"]) -> list["Issue"]:
        """Replace queue from fetched issues using canonical eligibility policy."""
        prior_scope = list(self._state.cached_scope_issues)
        prior_queue = list(self._state.cached_queue_issues)
        prior_count = len(prior_queue)
        scope = [issue for issue in issues if _matches_scope(self._config, issue)]
        queue = [issue for issue in scope if self.evaluate_issue(issue) == QueueMutationStatus.ACCEPTED]
        retainable_removed_numbers = _retainable_removed_numbers(self, prior_queue, queue)
        if _is_suspicious_shrink(prior_count, len(retainable_removed_numbers)):
            if _pending_shrink_confirmed(self._state, retainable_removed_numbers):
                logger.warning(
                    "[QUEUE_CACHE] confirmed large queue shrink: prior=%d candidate=%d "
                    "removing=%d",
                    prior_count,
                    len(queue),
                    len(retainable_removed_numbers),
                )
                clear_queue_shrink_confirmation(self._state)
            else:
                _record_pending_shrink(
                    self._state,
                    prior_count=prior_count,
                    candidate_count=len(queue),
                    missing_numbers=retainable_removed_numbers,
                )
                logger.warning(
                    "[QUEUE_CACHE] suspicious queue shrink retained pending confirmation: "
                    "prior=%d candidate=%d missing=%d confirm_at=%.3f",
                    prior_count,
                    len(queue),
                    len(retainable_removed_numbers),
                    self._state.queue_pending_shrink_confirm_at,
                )
                self._state.cached_scope_issues = _merge_issue_lists(prior_scope, scope)
                self._state.cached_queue_issues = _merge_issue_lists(prior_queue, queue)
                self.prune_refresh_timestamps()
                return self._state.cached_queue_issues
        else:
            if queue_shrink_confirmation_pending(self._state):
                logger.info("[QUEUE_CACHE] clearing unconfirmed queue shrink; refresh recovered")
            clear_queue_shrink_confirmation(self._state)

        if prior_count > 0 and not queue:
            rejected = len(issues) - len(queue)
            active_count = len(self._state.active_sessions)
            history_count = len(self._state.session_history)
            logger.warning(
                "[QUEUE_CACHE] replace_from_refresh dropping in-memory queue from %d to 0 "
                "(fetched=%d, rejected_by_eligibility=%d, active_sessions=%d, session_history=%d); "
                "downstream save_snapshot will wipe persisted cache\nstack:\n%s",
                prior_count, len(issues), rejected, active_count, history_count,
                "".join(traceback.format_stack(limit=10)),
            )
        self._state.cached_scope_issues = scope
        self._state.cached_queue_issues = queue
        self.prune_refresh_timestamps()
        return queue

    def upsert_refreshed_issue(self, issue: "Issue") -> QueueMutationOutcome:
        """Upsert a refreshed issue while enforcing queue eligibility policy."""
        was_present = any(cached.number == issue.number for cached in self._state.cached_queue_issues)
        self._state.cached_scope_issues = [
            cached for cached in self._state.cached_scope_issues if cached.number != issue.number
        ]
        self._state.cached_queue_issues = [
            cached for cached in self._state.cached_queue_issues if cached.number != issue.number
        ]
        if _matches_scope(self._config, issue):
            self._state.cached_scope_issues.append(issue)
        status = self.evaluate_issue(issue)
        if status == QueueMutationStatus.ACCEPTED:
            self._state.cached_queue_issues.append(issue)
            return QueueMutationOutcome(status=status, in_queue=True, updated=was_present)
        return QueueMutationOutcome(status=status, in_queue=False, updated=False)

    def remove_issue(self, issue_number: int) -> None:
        """Remove issue from cached queue and refresh metadata."""
        self._state.cached_scope_issues = [
            issue for issue in self._state.cached_scope_issues if issue.number != issue_number
        ]
        self._state.cached_queue_issues = [
            issue for issue in self._state.cached_queue_issues if issue.number != issue_number
        ]
        clear_issue_refresh(self._state, issue_number)
        self.prune_refresh_timestamps()

    def remove_issue_and_save(self, issue_number: int) -> None:
        """Remove an issue and persist the resulting warm-restart snapshot."""
        self.remove_issue(issue_number)
        self.save_snapshot()

    def evaluate_issue(self, issue: "Issue") -> QueueMutationStatus:
        """Evaluate whether issue can be in queue cache.

        The duplicate-launch guard has two halves, and they answer the same
        question about different kinds of work. The session-derived half —
        ``session_history`` ∪ ``active_sessions`` — covers work a terminal is
        doing. The control-operation half covers work the ORCHESTRATOR is doing
        about one exact candidate with no terminal at all (#146), which is
        invisible to every session-derived signal by construction.

        The second half reads the RECONCILED projection
        (``ControlOperationOwnership.reconcile``), never a durable lease row: a
        row that outlived the operation it named would otherwise exclude this
        issue for good. Both halves report ``REJECTED_EXCLUDED``, which keeps
        the issue visible to :meth:`reconciliation_only_issues` — an issue held
        by a live control operation still needs its other state reconciled.

        The history half asks the history owner which entries still CLAIM
        their issue, not merely which issues appear in the list (#195). A
        session that ended leaving no owner has its claim released once the
        engine gives the issue back; the entry stays as the operator's record
        of that failure, and a record is not a claim.
        """
        if not _matches_scope(self._config, issue):
            return QueueMutationStatus.REJECTED_OUT_OF_SCOPE

        excluded_numbers = set(self._history_owner().claiming_issue_numbers())
        excluded_numbers.update(session.issue.number for session in self._state.active_sessions)
        if issue.number in excluded_numbers:
            return QueueMutationStatus.REJECTED_EXCLUDED

        if self._state.control_operation_exclusions.excludes_issue(issue.key):
            return QueueMutationStatus.REJECTED_EXCLUDED

        if self._config.filtering.issue and issue.number != self._config.filtering.issue:
            return QueueMutationStatus.REJECTED_OUT_OF_SCOPE

        return QueueMutationStatus.ACCEPTED

    def is_outside_engine_scope(self, issue: "Issue") -> bool:
        """Whether this engine's configured scope excludes the issue outright.

        The unshadowed scope question, and the one every caller asking "is this
        issue mine to act on at all?" must use. :meth:`evaluate_issue` answers
        the composite question "may this issue enter the queue?", and it reports
        the duplicate-launch guard BEFORE the ``--issue`` filter — so an issue in
        ``session_history`` or ``active_sessions`` reads ``REJECTED_EXCLUDED``
        and never reaches its ``--issue`` check. Reading scope off that verdict
        lets an operator-narrowed run widen back out through any issue it has
        already seen.

        Answers scope only. ``REJECTED_EXCLUDED`` means in scope but already
        claimed this run, which is a different question and stays with
        :meth:`evaluate_issue`.
        """
        return not evaluate_issue_scope(
            self._config, issue, include_issue_number_filter=True
        ).in_scope

    def is_outside_single_issue_scope(self, issue: "Issue") -> bool:
        """Whether ``--issue N`` alone excludes the issue, asking nothing else.

        The unshadowed question again — see :meth:`is_outside_engine_scope` for
        why the queue verdict cannot answer it — but bounded to the operator's
        single-issue narrowing. For callers that hold local evidence about an
        issue (a persisted ``in-progress`` label, a worktree with partial work)
        and must act on it even when GitHub's current snapshot has drifted out
        of the label, milestone, or open-state gates. Dropping such an issue is
        the bug those callers exist to prevent; ``--issue N`` is the one gate
        that still binds them, because the operator asked for it this run.

        Use :meth:`is_outside_engine_scope` instead whenever the fresh GitHub
        snapshot really is the authority on whether the issue is ours.
        """
        return outside_single_issue_scope(self._config, issue)

    def reconciliation_only_issues(self) -> list["Issue"]:
        """In-scope issues the duplicate-launch guard keeps out of the queue (#46).

        Scheduling eligibility and reconciliation visibility are different
        questions asked of the same cache. :meth:`evaluate_issue` answers the
        first, and ``REJECTED_EXCLUDED`` is the ONE branch that means "in scope,
        but not launchable again this run" — a completed session, a running one,
        a reconciled live control operation (#146), or the awaiting-merge
        presentation record startup rehydrates into
        ``session_history``. Reconciliation must still see those issues, or state
        recorded against them (a provider block, say) can never be retired by its
        owner once the queue drops them.

        Deliberately NOT the whole in-scope set: everything
        ``REJECTED_OUT_OF_SCOPE`` covers stays excluded here exactly as it is
        from the queue. The engine's single-issue scope is re-asked through
        :meth:`is_outside_engine_scope` for the precedence reason documented
        there — an operator-narrowed run would otherwise widen back out here.
        Disjoint from the queue by construction, so callers can concatenate the
        two without deduplicating.
        """
        queued = {issue.number for issue in self._state.cached_queue_issues}
        return [
            issue
            for issue in self._state.cached_scope_issues
            if issue.number not in queued
            and self.evaluate_issue(issue) is QueueMutationStatus.REJECTED_EXCLUDED
            and not self.is_outside_engine_scope(issue)
        ]

    def abandoned_after_completion_issues(self) -> list["Issue"]:
        """Reconciliation-visible issues NOTHING is holding any more (#195).

        The discrimination inside ``REJECTED_EXCLUDED`` that
        :meth:`reconciliation_only_issues` deliberately does not draw. That set
        is "in scope, not launchable again this run", which lumps four
        situations together; three of them have an owner still answering for
        the issue and must keep answering exactly as they do today:

        - a RUNNING session (``active_sessions``) — the terminal owns it;
        - a live control operation (``control_operation_exclusions``) — the
          orchestrator itself owns it, with no terminal at all (#146);
        - the awaiting-merge presentation record startup rehydrates into
          ``session_history`` — the awaiting-merge reconciler owns it.

        What is left is the abandoned-after-completion case: a session that
        ended, was disposed, and left the issue to nobody
        (``ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES``). "Is this issue's
        ``in-progress`` label stale?" is a reconciliation question by the very
        distinction :meth:`reconciliation_only_issues` documents, and this is
        the subset of it that can be answered honestly without stepping on
        another owner's issue.

        A strict subset of :meth:`reconciliation_only_issues`, so it is
        disjoint from the queue for the same reason and callers may concatenate
        the two without deduplicating.
        """
        abandoned = self._history_owner().abandoned_after_completion_issue_numbers()
        if not abandoned:
            return []
        running = {session.issue.number for session in self._state.active_sessions}
        return [
            issue
            for issue in self.reconciliation_only_issues()
            if issue.number in abandoned
            and issue.number not in running
            and not self._state.control_operation_exclusions.excludes_issue(issue.key)
        ]

    def prune_refresh_timestamps(self) -> None:
        """Prune refresh timestamp map to currently tracked issue IDs."""
        if (
            not self._state.issue_refresh_timestamps
            and not self._state.issue_last_refreshed_at
            and not self._state.awaiting_merge_drift_scan_timestamps
        ):
            return
        keep_numbers = {issue.number for issue in self._state.cached_scope_issues}
        keep_numbers.update(issue.number for issue in self._state.cached_queue_issues)
        keep_numbers.update(session.issue.number for session in self._state.active_sessions)
        keep_numbers.update(entry.issue_number for entry in self._state.session_history)
        keep_numbers.update(self._visible_issue_numbers())
        self._state.issue_refresh_timestamps = {
            issue_number: refreshed_at
            for issue_number, refreshed_at in self._state.issue_refresh_timestamps.items()
            if issue_number in keep_numbers
        }
        self._state.issue_last_refreshed_at = {
            issue_number: refreshed_at
            for issue_number, refreshed_at in self._state.issue_last_refreshed_at.items()
            if issue_number in keep_numbers
        }
        self._state.awaiting_merge_drift_scan_timestamps = {
            issue_number: scanned_at
            for issue_number, scanned_at in self._state.awaiting_merge_drift_scan_timestamps.items()
            if issue_number in keep_numbers
        }

    def _visible_issue_numbers(self) -> set[int]:
        """Return issues that the UI is actively displaying and should keep fresh."""
        if self._state.ui_visible_updated_at <= 0:
            return set()
        if (time.time() - self._state.ui_visible_updated_at) > _UI_VISIBILITY_STALENESS_SECONDS:
            return set()
        return set(self._state.ui_visible_issue_numbers)

    def save_snapshot(self) -> None:
        """Persist the current in-scope queue snapshot for warm restarts.

        The durable snapshot covers in-scope issues and the delta watermark;
        runtime scheduling priority remains in memory.
        """
        if self._store is None:
            raise RuntimeError("QueueCacheStore is required to persist queue cache snapshot")
        self._store.save_snapshot(
            self._state.cached_scope_issues,
            self._state.queue_delta_watermark,
            repo=self._config.repo or "",
        )


def record_issue_refreshes(
    state: "OrchestratorState",
    refreshed_numbers: set[int],
    refreshed_at: float,
) -> None:
    """Record freshness for tracked issues in both dashboard freshness maps."""
    if not refreshed_numbers:
        return
    for issue_number in refreshed_numbers:
        state.issue_refresh_timestamps[issue_number] = refreshed_at
        state.issue_last_refreshed_at[issue_number] = refreshed_at


def clear_issue_refresh(state: "OrchestratorState", issue_number: int) -> None:
    """Clear freshness metadata for an issue from both dashboard freshness maps."""
    state.issue_refresh_timestamps.pop(issue_number, None)
    state.issue_last_refreshed_at.pop(issue_number, None)
    state.awaiting_merge_drift_scan_timestamps.pop(issue_number, None)


def queue_shrink_confirmation_pending(state: "OrchestratorState") -> bool:
    """Whether a large queue shrink is waiting for a confirming refresh."""
    return bool(state.queue_pending_shrink_missing_issue_numbers)


def queue_shrink_confirmation_due(state: "OrchestratorState", now: float) -> bool:
    """Whether the pending queue shrink should force a confirmation scan."""
    return (
        queue_shrink_confirmation_pending(state)
        and state.queue_pending_shrink_confirm_at > 0
        and now >= state.queue_pending_shrink_confirm_at
    )


def clear_queue_shrink_confirmation(state: "OrchestratorState") -> None:
    """Clear any pending large-shrink confirmation state."""
    state.queue_pending_shrink_missing_issue_numbers = []
    state.queue_pending_shrink_confirm_at = 0.0
    state.queue_pending_shrink_prior_count = 0
    state.queue_pending_shrink_candidate_count = 0


def _matches_scope(config: "Config", issue: "Issue") -> bool:
    """Apply label/milestone/exclude-label scope checks for an issue."""
    return issue_scope_skip_detail(config, issue) is None


def _is_suspicious_shrink(prior_count: int, removed_count: int) -> bool:
    if prior_count <= 0 or removed_count < _SUSPICIOUS_SHRINK_MIN_REMOVALS:
        return False
    return (removed_count / prior_count) >= _SUSPICIOUS_SHRINK_MIN_RATIO


def _retainable_removed_numbers(
    cache: QueueCache,
    prior_queue: list["Issue"],
    queue: list["Issue"],
) -> set[int]:
    candidate_numbers = {issue.number for issue in queue}
    return {
        issue.number
        for issue in prior_queue
        if issue.number not in candidate_numbers
        and cache.evaluate_issue(issue) == QueueMutationStatus.ACCEPTED
    }


def _pending_shrink_confirmed(
    state: "OrchestratorState",
    missing_numbers: set[int],
) -> bool:
    """Return true only when every pending missing issue is still missing."""
    pending = set(state.queue_pending_shrink_missing_issue_numbers)
    return bool(pending) and pending.issubset(missing_numbers)


def _record_pending_shrink(
    state: "OrchestratorState",
    *,
    prior_count: int,
    candidate_count: int,
    missing_numbers: set[int],
) -> None:
    existing_confirm_at = state.queue_pending_shrink_confirm_at
    state.queue_pending_shrink_missing_issue_numbers = sorted(missing_numbers)
    if existing_confirm_at > 0:
        state.queue_pending_shrink_confirm_at = existing_confirm_at
    else:
        state.queue_pending_shrink_confirm_at = (
            time.time() + QUEUE_SHRINK_CONFIRM_DELAY_SECONDS
        )
    state.queue_pending_shrink_prior_count = prior_count
    state.queue_pending_shrink_candidate_count = candidate_count


def _merge_issue_lists(
    prior: list["Issue"],
    current: list["Issue"],
) -> list["Issue"]:
    current_numbers = {issue.number for issue in current}
    return [
        *(issue for issue in current),
        *(issue for issue in prior if issue.number not in current_numbers),
    ]
