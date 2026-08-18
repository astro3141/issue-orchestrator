"""Rehydrate awaiting-merge history for locally ``pr-pending`` issues.

Startup's side of the awaiting-merge lifecycle. The local label store is the
crash-safe record that an issue moved into PR flow; this owner turns each such
record back into the ``completed`` session-history entry the awaiting-merge
reconciler scans, so a restart does not lose the issue's place in the pipeline.

It is the ONLY producer of that rehydration, and therefore decides which PR an
issue's recovered entry keys on. It used to accept an open PR and nothing else,
which silently excluded the case where the PR had already merged while the
orchestrator was down: the reconciler never saw those issues, so nothing ever
shed their ``pr-pending`` and the planner parked them forever on
``reason=pr_pending`` (#113). A merged PR is now a first-class recovery source,
handed to the reconciler exactly as an open one is — the merged-PR routing
(close-on-merge vs. deliberate non-closing merge) belongs to that owner, not
to this one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from ..domain.models import SessionHistoryEntry
from ..infra.analysis import analyze_issue
from ..ports.repository_host import RepositoryHostError
from .awaiting_merge_drift_policy import classify_pr_set
from .queue_cache import record_issue_refreshes

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..ports.label_store import LabelStore
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager
    from .queue_cache import QueueCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrPendingRecoverySource:
    """Which PR (if any) a locally ``pr-pending`` issue should be recovered on.

    ``recover`` carries the PR URL to record on the rehydrated history entry;
    ``skip`` carries why no entry can be built. The two never both hold data.
    """

    outcome: Literal["recover", "skip"]
    pr_url: str = ""
    skip_reason: str = ""

    def __post_init__(self) -> None:
        if self.outcome == "recover" and not self.pr_url:
            raise ValueError("recover source requires a pr_url")
        if self.outcome == "skip" and not self.skip_reason:
            raise ValueError("skip source requires a skip_reason")

    @staticmethod
    def recover(pr_url: str) -> "PrPendingRecoverySource":
        return PrPendingRecoverySource("recover", pr_url=pr_url)

    @staticmethod
    def skip(reason: str) -> "PrPendingRecoverySource":
        return PrPendingRecoverySource("skip", skip_reason=reason)


def resolve_pr_pending_recovery_source(
    *,
    issue_number: int,
    has_open_pr: bool,
    open_pr_url: str | None,
    get_prs_for_issue: "Callable[[int], list[PRInfo]]",
) -> PrPendingRecoverySource:
    """Pick the PR a locally ``pr-pending`` issue's history entry keys on.

    An open PR (already established by the caller's issue analysis) wins and
    costs nothing extra. Only when there is none — the stale case — does this
    pay one PR-set read, and it defers to ``classify_pr_set`` so the
    "latest terminal PR decides" precedence stays owned in one place.

    The two facts the analysis holds are taken separately, and deliberately.
    Collapsing them (``pr_url if has_open_pr else None``) would send the
    "open PR, URL unknown" shape down the no-open-PR branch, where it pays a
    PR-set read it cannot use and comes back with the self-contradicting
    reason "no open or merged PR (PR set resolves to open)". That shape is a
    caller-side gap in the analysis, not a stale-PR case: it is named as such
    and costs no read.

    A merged PR is recovered rather than skipped: its issue is still open and
    still labelled ``pr-pending``, and only the awaiting-merge reconciler can
    decide whether that means a failed auto-close or a deliberate non-closing
    merge. Skipping it, as this used to, left nobody to decide at all (#113).
    """
    if has_open_pr:
        if not open_pr_url:
            return PrPendingRecoverySource.skip(
                "issue analysis reports an open PR but carries no PR URL"
            )
        return PrPendingRecoverySource.recover(open_pr_url)
    try:
        prs = get_prs_for_issue(issue_number)
    except RepositoryHostError as exc:
        return PrPendingRecoverySource.skip(f"associated PRs unreadable: {exc}")
    classification = classify_pr_set(prs)
    if classification.outcome == "merged" and classification.pr is not None:
        return PrPendingRecoverySource.recover(classification.pr.url)
    return PrPendingRecoverySource.skip(
        f"no open or merged PR (PR set resolves to {classification.outcome})"
    )


@dataclass(frozen=True)
class PrPendingHistoryRecovery:
    """Owner of startup's ``pr-pending`` → session-history rehydration."""

    repository_host: "RepositoryHost"
    label_manager: "LabelManager"
    label_store: "LabelStore"
    queue_cache: "QueueCache"
    session_exists: Callable[[str], bool]
    repo: str | None

    def recover(
        self,
        state: "OrchestratorState",
        issue_branches: dict[int, str],
    ) -> int:
        """Rehydrate history for every locally ``pr-pending`` issue.

        Returns the number of issues recovered.
        """
        tracked_history = {entry.issue_number for entry in state.session_history}
        local_pr_pending = sorted(
            issue_number
            for issue_number, labels in self.label_store.load_all().items()
            if self.label_manager.is_pr_pending(sorted(labels))
        )

        recovered = 0
        for issue_number in local_pr_pending:
            if issue_number in tracked_history:
                continue
            if self._recover_issue(state, issue_number, issue_branches):
                tracked_history.add(issue_number)
                recovered += 1

        if recovered:
            logger.info(
                "[startup] Recovered %d pr-pending issue(s) into dashboard history",
                recovered,
            )
        return recovered

    def _recover_issue(
        self,
        state: "OrchestratorState",
        issue_number: int,
        issue_branches: dict[int, str],
    ) -> bool:
        issue = self.repository_host.get_issue(issue_number)
        if issue is None:
            logger.warning(
                "[startup] Failed to refetch locally pr-pending issue for "
                "dashboard recovery: issue=%d",
                issue_number,
            )
            return False

        if self.queue_cache.is_outside_engine_scope(issue):
            logger.info(
                "[startup] Skipping pr-pending dashboard recovery for "
                "out-of-scope issue=%d",
                issue_number,
            )
            return False

        analysis = analyze_issue(
            issue=issue,
            repo=self.repo,
            issue_branches=issue_branches,
            check_session_fn=lambda n: self.session_exists(f"issue-{n}"),
            pr_tracker=self.repository_host,
        )
        source = resolve_pr_pending_recovery_source(
            issue_number=issue_number,
            has_open_pr=analysis.has_open_pr,
            open_pr_url=analysis.pr_url,
            get_prs_for_issue=self._prs_for_issue,
        )
        if source.outcome == "skip":
            logger.warning(
                "[startup] Skipping pr-pending dashboard recovery without "
                "open or merged PR: issue=%d (%s)",
                issue_number,
                source.skip_reason,
            )
            return False

        state.session_history.append(
            SessionHistoryEntry(
                issue_number=issue.number,
                title=issue.title,
                agent_type=issue.agent_type or "agent:unknown",
                status="completed",
                runtime_minutes=0,
                pr_url=source.pr_url,
                status_reason="Recovered awaiting merge state on startup",
                completed_at=datetime.now(timezone.utc),
                issue_labels=tuple(issue.labels),
            )
        )
        record_issue_refreshes(state, {issue.number}, time.time())
        return True

    def _prs_for_issue(self, issue_number: int) -> "list[PRInfo]":
        return self.repository_host.get_prs_for_issue(issue_number, state="all")
