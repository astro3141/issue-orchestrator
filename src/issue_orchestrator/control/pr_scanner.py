"""PR Scanner - discovers PRs needing review/rework.

This module scans GitHub for PRs that need attention:
- PRs with needs-code-review label (orphaned reviews)
- PRs with needs-rework label (changes requested)

It returns lists of PendingReview/PendingRework to be queued,
but does NOT modify state directly. The orchestrator decides
what to do with the results.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence, Protocol, Callable

from ..infra.config import Config
from ..events import EventName
from ..domain.models import PendingReview, PendingRework
from ..domain.issue_key import IssueKey
from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from .publication_authority import PublicationVerdictReader
from .review_validity import evaluate_review_validity
from .review_scope import ReviewScopeChecker, extract_issue_number_from_pr
from .rework_cycle_policy import (
    ReworkAdmission,
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
)
from ..ports import EventSink,  make_trace_event
from ..ports.pull_request_tracker import PRInfo
from ..infra import gh_audit
from ..infra.timeline_trace import is_timeline_trace_enabled

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class RepositoryScanner(Protocol):
    """Protocol for repository scanning operations."""

    def get_prs_with_label(self, label: str) -> list[PRInfo]: ...
    def create_issue_key(self, issue_number: int) -> IssueKey: ...
    def get_issue(self, issue_number: int) -> "Issue | None": ...


@dataclass
class ScanResult:
    """Result of scanning for PRs."""

    reviews_to_queue: list[PendingReview]
    reworks_to_queue: list[PendingRework]
    escalations: list[tuple[int, int, int]]  # (pr_number, issue_number, rework_cycle)


class PRScanner:
    """Scans for PRs needing review or rework.

    This is a stateless scanner. It queries GitHub for PRs with specific
    labels and returns what should be queued, but does not modify any state.

    The orchestrator calls this periodically and decides whether to actually
    queue the discovered items (avoiding duplicates, checking capacity, etc.).
    """

    def __init__(
        self,
        config: Config,
        repository: RepositoryScanner,
        events: EventSink,
        label_manager: "LabelManager | None" = None,
        issue_branches_fn: Callable[[], dict[int, str]] | None = None,
        *,
        publication_verdict: "PublicationVerdictReader",
    ):
        """Initialize the scanner.

        Args:
            config: Configuration with label settings
            repository: Adapter for GitHub operations
            events: EventSink for trace events
            label_manager: Label registry for prefix-aware queries.
            publication_verdict: The orchestrator's shared reader of the
                publication verdict (#45) — the refusal marker, the refusals
                whose write did not commit, and the candidate's own receipt.
                Required, with no default: a scanner built without it would
                queue reviews on the strength of a label alone, which is the
                defect, and there is no safe stand-in — an empty reader refuses
                everything and a permissive one restores the fail-open state.
        """
        self.config = config
        self.repository = repository
        self.events = events
        self._issue_branches = issue_branches_fn or (lambda: {})
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager
        self._publication_verdict = publication_verdict
        self._review_scope = ReviewScopeChecker(config, repository, log_prefix="SCANNER")

    @property
    def rework_budget(self) -> ReworkCycleBudget:
        """The rework-cycle owner this scanner decides through (#297).

        Built per read rather than cached, because the ceiling is a live
        configuration value the scanner has always re-read per decision. The
        continuation handoff builds the same owner over the same two inputs, so
        neither producer can invent its own cycle arithmetic or ceiling.
        """
        return ReworkCycleBudget(
            self._lm, max_rework_cycles=self.config.max_rework_cycles
        )

    def load_issue_branches(self) -> dict[int, str]:
        """Load the current issue->branch map for scan-time scoping."""
        return self._issue_branches()

    def scan_for_reviews(
        self,
        already_queued: Sequence[PendingReview],
        active_sessions: Sequence[str],  # session names
        issue_branches: dict[int, str] | None = None,
    ) -> list[PendingReview]:
        """Scan for PRs needing code review.

        Finds PRs with the code-review label that aren't already queued
        or being actively reviewed.

        Args:
            already_queued: Currently queued reviews (to avoid duplicates)
            active_sessions: Active session names (to skip PRs being reviewed)

        Returns:
            List of PendingReview for PRs that need to be queued
        """
        if not self.config.code_review_agent or not self.config.code_review_label:
            return []

        with gh_audit.context(
            reason=gh_audit.AuditReason.PR_SCAN,
            scope=gh_audit.AuditScope.PERIODIC,
        ):
            prs = self.repository.get_prs_with_label(self.config.code_review_label)
        results: list[PendingReview] = []

        queued_pr_numbers = {r.pr_number for r in already_queued}
        active_review_sessions = {s for s in active_sessions if s.startswith("review-")}
        issue_branches = issue_branches if issue_branches is not None else self.load_issue_branches()

        for pr in prs:
            # Skip if already queued
            if pr.number in queued_pr_numbers:
                continue

            # Skip if already being reviewed
            session_name = f"review-{pr.number}"
            if session_name in active_review_sessions:
                continue

            issue_number = extract_issue_number_from_pr(pr)

            # Skip PRs whose linked issue is outside configured scope
            scope = self._review_scope.check_issue_number(issue_number, pr.number)
            if not scope.in_scope:
                continue

            scoped = scope_prs_to_active_issue_branch(
                issue_number,
                [pr],
                issue_branches=issue_branches,
            )
            if not scoped.matching:
                logger.info(
                    "[SCANNER] Ignoring review PR from prior attempt: pr=%d issue=%d branch=%s expected_branch=%s",
                    pr.number,
                    issue_number,
                    pr.branch,
                    scoped.expected_branch,
                )
                continue

            issue = scope.issue if scope.issue is not None else self.repository.get_issue(issue_number)
            validity = evaluate_review_validity(
                config=self.config,
                label_manager=self._lm,
                issue=issue,
                publication_verdict=self._publication_verdict,
                pr=pr,
                review_label_confirmed=True,
            )
            if not validity.valid:
                logger.info(
                    "[SCANNER] Skipping stale review PR: pr=%d issue=%d reason=%s issue_labels=%s pr_labels=%s",
                    pr.number,
                    issue_number,
                    validity.reason,
                    ",".join(validity.issue_labels) or "(missing)",
                    ",".join(validity.pr_labels) or "(none)",
                )
                if validity.candidate_uncertified:
                    # Announced, not just logged. Every other refusal here
                    # decays on its own, so the log line is a passing note; this
                    # one holds until a new candidate is gated, and a PR the
                    # orchestrator did not create never gets one. The launcher
                    # already reports its refusals this way, so a PR that is
                    # dropped before ever reaching the queue is visible in the
                    # same place rather than only in the console (#45).
                    self.events.publish(
                        make_trace_event(
                            EventName.REVIEW_SKIPPED,
                            {
                                "pr_number": pr.number,
                                "issue_number": issue_number,
                                "reason": f"uncertified_candidate:{validity.reason}",
                            },
                        )
                    )
                continue

            review = PendingReview(
                issue_key=self.repository.create_issue_key(issue_number),
                pr_number=pr.number,
                pr_url=pr.url,
                branch_name=pr.branch,
                _issue_number=issue_number,
                issue_labels=validity.issue_labels,
            )
            results.append(review)
            logger.info("[SCANNER] Found orphaned PR #%d for code review", pr.number)

        if results:
            self.events.publish(
                make_trace_event(
                    EventName.SCANNER_REVIEWS_FOUND,
                    {"count": len(results)},
                )
            )

        return results

    def scan_for_reworks(
        self,
        already_queued: Sequence[PendingRework],
        active_sessions: Sequence[int],  # issue numbers being worked on
        issue_branches: dict[int, str] | None = None,
    ) -> tuple[list[PendingRework], list[tuple[int, int, int]]]:
        """Scan for PRs needing rework.

        Finds PRs with the needs-rework label that aren't already queued
        or being actively worked on.

        Args:
            already_queued: Currently queued reworks (to avoid duplicates)
            active_sessions: Issue numbers of active work sessions

        Returns:
            Tuple of (reworks to queue, escalations needed)
            Escalations are (pr_number, issue_number, rework_cycle) tuples
        """
        if not self.config.code_review_agent:
            return [], []

        rework_label = self._lm.needs_rework
        with gh_audit.context(
            reason=gh_audit.AuditReason.PR_SCAN,
            scope=gh_audit.AuditScope.PERIODIC,
        ):
            prs = self.repository.get_prs_with_label(rework_label)
        logger.info("[SCANNER] Found %d PRs with '%s' label", len(prs), rework_label)

        results: list[PendingRework] = []
        escalations: list[tuple[int, int, int]] = []

        queued_issue_ids = self._collect_queued_issue_ids(already_queued)
        active_issue_numbers = set(active_sessions)
        issue_branches = issue_branches if issue_branches is not None else self.load_issue_branches()

        for pr in prs:
            decision = self._decide_rework_candidate(pr, queued_issue_ids, active_issue_numbers)
            self._log_rework_decision(pr, decision, queued_issue_ids, active_issue_numbers)
            if decision.verdict is ReworkAdmissionVerdict.SKIP:
                continue
            if decision.escalates:
                escalations.append((pr.number, decision.issue_number, decision.rework_cycle))
                continue

            scoped = scope_prs_to_active_issue_branch(
                decision.issue_number,
                [pr],
                issue_branches=issue_branches,
            )
            if not scoped.matching:
                logger.info(
                    "[SCANNER] Ignoring rework PR from prior attempt: pr=%d issue=%d branch=%s expected_branch=%s",
                    pr.number,
                    decision.issue_number,
                    pr.branch,
                    scoped.expected_branch,
                )
                continue

            issue = self.repository.get_issue(decision.issue_number)
            if not issue:
                logger.warning(
                    "[SCANNER] PR #%d references issue #%d which doesn't exist, skipping",
                    pr.number, decision.issue_number
                )
                continue
            agent_type = issue.agent_type
            if not agent_type:
                logger.warning(
                    "[SCANNER] Issue #%d has no agent label, skipping PR #%d",
                    decision.issue_number, pr.number
                )
                continue
            results.append(
                PendingRework(
                    issue_key=self.repository.create_issue_key(decision.issue_number),
                    agent_type=agent_type,
                    rework_cycle=decision.rework_cycle,
                    issue_number=decision.issue_number,
                    pr_number=pr.number,
                )
            )
            logger.info("[SCANNER] Found PR #%d for rework (cycle %d)", pr.number, decision.rework_cycle)
            if is_timeline_trace_enabled():
                logger.info(
                    "[TIMELINE] scanner.rework_queue pr=%s issue=%s cycle=%s agent=%s",
                    pr.number,
                    decision.issue_number,
                    decision.rework_cycle,
                    agent_type,
                )

        if results or escalations:
            self.events.publish(
                make_trace_event(
                    EventName.SCANNER_REWORKS_FOUND,
                    {
                        "reworks": len(results),
                        "escalations": len(escalations),
                    },
                )
            )

        return results, escalations

    @staticmethod
    def _collect_queued_issue_ids(already_queued: Sequence[PendingRework]) -> set[int]:
        queued_issue_ids: set[int] = set()
        for rework in already_queued:
            issue_number = rework.resolve_issue_number()
            if issue_number is not None:
                queued_issue_ids.add(issue_number)
        return queued_issue_ids

    def _decide_rework_candidate(
        self,
        pr: PRInfo,
        queued_issue_ids: set[int],
        active_issue_numbers: set[int],
    ) -> ReworkAdmission:
        """Scope the PR to an in-scope issue, then let the cycle owner decide.

        Scope is the scanner's own question — it found the PR by label and must
        resolve the issue behind it — and everything after it is the shared
        rework-cycle policy, so the ordinary lane and the continuation handoff
        cannot drift apart on cycle arithmetic or the ceiling (#297).
        """
        budget = self.rework_budget
        scope = self._review_scope.check_pr(pr)
        issue_number = scope.issue_number

        # Skip PRs whose linked issue is outside configured scope
        if not scope.in_scope:
            return ReworkAdmission(
                verdict=ReworkAdmissionVerdict.SKIP,
                issue_number=issue_number,
                rework_cycle=0,
                reason="out_of_scope",
            )

        held = budget.already_held(
            issue_number,
            queued_issue_numbers=queued_issue_ids,
            active_issue_numbers=active_issue_numbers,
        )
        if held is not None:
            return held
        # Read once, and only once the cheap refusals are past: the linked
        # issue's labels matter because a publish failure marks the issue as
        # blocked-failed but may leave needs-rework standing on the PR.
        issue = scope.issue if scope.issue is not None else self.repository.get_issue(issue_number)
        return budget.admit(
            issue_number=issue_number,
            pr_labels=pr.labels,
            issue_labels=issue.labels if issue is not None else [],
            queued_issue_numbers=queued_issue_ids,
            active_issue_numbers=active_issue_numbers,
        )

    def _log_rework_decision(
        self,
        pr: PRInfo,
        decision: ReworkAdmission,
        queued_issue_ids: set[int],
        active_issue_numbers: set[int],
    ) -> None:
        if decision.reason == "blocking_label" and decision.blocking_labels:
            logger.debug(
                "[SCANNER] PR #%d already blocked (%s), skipping",
                pr.number,
                ", ".join(decision.blocking_labels),
            )
        if not is_timeline_trace_enabled():
            return
        logger.info(
            "[TIMELINE] scanner.rework_candidate pr=%s issue=%s labels=%s queued=%s active=%s",
            pr.number,
            decision.issue_number,
            ",".join(pr.labels),
            decision.issue_number in queued_issue_ids,
            decision.issue_number in active_issue_numbers,
        )
        if decision.verdict is ReworkAdmissionVerdict.SKIP:
            extra = (
                f" blocking={','.join(decision.blocking_labels)}"
                if decision.reason == "blocking_label" and decision.blocking_labels
                else ""
            )
            logger.info(
                "[TIMELINE] scanner.rework_skip pr=%s issue=%s reason=%s%s",
                pr.number,
                decision.issue_number,
                decision.reason,
                extra,
            )
            return
        if decision.escalates:
            logger.info(
                "[TIMELINE] scanner.rework_escalate pr=%s issue=%s cycle=%s max=%s",
                pr.number,
                decision.issue_number,
                decision.rework_cycle,
                self.config.max_rework_cycles,
            )

    def _get_rework_cycle_from_labels(self, labels: list[str]) -> int:
        """Extract rework cycle count from labels (rework-cycle-N).

        Returns the NEXT cycle number (e.g., rework-cycle-2 means next is cycle 3).
        Delegated to the shared rework-cycle owner so this scanner and the
        continuation handoff count with the same arithmetic (#297).
        """
        return self.rework_budget.next_cycle(labels)
