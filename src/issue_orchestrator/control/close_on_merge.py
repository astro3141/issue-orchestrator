"""Close-on-merge fallback and awaiting-merge fact builders.

GitHub's closing-keyword parse was the ONLY close-on-merge mechanism. It is
word-boundary sensitive and easily defeated — a hand-authored recovery PR whose
body contained literal ``\\n`` escapes ("...\\n\\nCloses #45.") left the issue
open after its PR merged. The awaiting-merge reconciler shed the stale labels
and terminalized history but issued no close, so the first planning pass after
a restart relaunched a coding session on the already-merged issue (porchpin
case file #81, ``merged-unclosed-issue-relaunched-after-restart``).

The merged-terminal discovery therefore reads the issue's live state once, at
the terminal transition (not per-tick — the #6600 rollup-noise removal is
untouched), and carries ``issue_open`` on the reconciliation fact so the
Planner can order a close on the terminal-recovery owner command. Only a
MERGED PR earns the fallback: closed-unmerged PRs keep their drift-path
behavior, and intentionally reopened issues (porchpin case file #59) are never
touched — their history entries are already terminal and cannot re-fire.

WHY THIS MODULE'S ANSWER CHANGED (#113)
=======================================

``merged + issue OPEN`` was then found to describe two OPPOSITE situations —
the failed auto-close above, and a merge that deliberately did not close its
issue (a PR that says ``Refs #45``, landing partial work) — identical on every
field this owner read. The commit above (#6956) could not tell them apart and
resolved every one of them the same way: close the issue. So the deliberate
merge had no honest owner. Its issue was either closed against the author's
intent, or — when it never reached this module at all — left parked on a stale
``pr-pending`` forever, needing a human to strip the label (#113).

GitHub's REGISTERED closing linkage (``PullRequest.closingIssuesReferences`` —
GitHub's own resolution of the closing keywords, not a text parse of ours) is
the disambiguator, and it routes the outcome here so both cases share one
owner. It is not a perfect one: an EMPTY registration still means either "the
author never asked for a close" or "the author asked in a form GitHub failed
to parse", and no field separates those. A choice had to be made.

**#113 resolves an empty registration as a deliberate continuation, and this
SUPERSEDES #6956's opposite resolution of the same condition.** No close; the
ordinary terminal recovery sheds the stale ``pr-pending`` so the still-open
issue rejoins selection. The fallback close now fires ONLY when the PR did
register this issue — i.e. only on a genuinely failed auto-close.

Residual exposure, stated deliberately: the literal-``\\n`` PR at the top of
this docstring registers nothing, so under the new rule it lands on the
continuation side. It is no longer closed; ``pr-pending`` is shed and the
already-merged issue returns to ordinary selection, where a planning pass may
relaunch a coding session on it — porchpin #81's symptom, reached by a
different route and no longer wedged behind a permanent stale label. That
trade was taken because #6956's failure mode was worse and silent: it closed
issues whose authors had deliberately left them open. The repair for a
defeated keyword belongs on the PR, where GitHub can parse it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, cast

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredAwaitingMergeReconciliation,
)
from ..ports.repository_host import RepositoryHostError
from .actions import ActionResult, CloseIssueAction
from .awaiting_merge_post_publish_policy import normalized_state
from .queue_cache import record_issue_refreshes

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, SessionHistoryEntry
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import ClosingIssueReferencesRead
    from ..ports.repository_host import RepositoryHost
    from .actions import RecoverTerminalIssueAction

logger = logging.getLogger(__name__)


def close_on_merge_evidence(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    closing_issue_references: "Callable[[int], ClosingIssueReferencesRead]",
    issue_number: int,
    pr_number: int,
    merged_at: str | None,
    on_issue_read: Callable[[], None] | None = None,
) -> bool | None:
    """Whether the issue behind a merged PR needs the fallback close — the
    single owner of the destructive precondition.

    True only on positive evidence of the failure this fallback exists for:
    GitHub registered the PR as closing the issue, the issue is open, AND no
    ``closed`` event exists at/after the PR's ``merged_at`` — i.e. the
    auto-close never fired for this merge. An open issue that HAS a close event
    since the merge was auto-closed and then deliberately reopened; it is never
    re-closed. A missing ``merged_at`` means no evidence either way — never
    infer a destructive close from ``state == open`` alone.

    ``merged + issue OPEN`` cannot on its own tell a failed auto-close apart
    from a deliberate non-closing merge (a PR that says ``Refs #45``, landing
    partial work): the two are identical on every field this owner used to
    read. GitHub's REGISTERED closing linkage separates them, so it is read
    here and routes the outcome (#113):

    - registered as closing this issue → the pre-existing evidence rule below
      decides, unchanged;
    - answered, but this issue is not in the set → a deliberate non-closing
      merge. Return False: no close, and the caller's ordinary terminal
      recovery sheds the stale ``pr-pending`` so the still-open issue rejoins
      selection instead of parking forever. This branch REVERSES #6956, which
      closed the issue on exactly this condition; the module docstring records
      why the ambiguity is resolved this way and what it costs;
    - unreadable → return None. Fail closed: no close AND no shed. Treating an
      unreadable relation as an empty one would shed a queue-gating label on a
      guess.

    Net effect on the destructive write: it is strictly NARROWER than before.
    The close now requires a registered reference on top of every pre-existing
    condition, so no issue closes today that would not have closed before.

    Called from BOTH phases: discovery (to plan the close attempt) and the
    apply-time owner command, which revalidates immediately before the
    destructive write — the planner's bit is advisory and can go stale in the
    discovery→apply gap (a human closing and reopening in between).

    Returns None when the evidence cannot be read (transient repository-host
    error) so the caller can retry without mutating: fail-open recreates the
    relaunch bug, and raising aborts the caller's tick. An issue the host
    reports as missing is treated as not-open — there is nothing to close.
    """
    try:
        issue = get_issue(issue_number)
    except RepositoryHostError:
        logger.warning(
            "Unable to check issue state for merged PR close fallback: "
            "issue=#%d; retrying without mutation",
            issue_number,
        )
        return None
    if issue is None:
        return False
    if on_issue_read is not None:
        on_issue_read()
    if normalized_state(issue.state) == "closed":
        # Terminal already; no linkage read needed, so the common
        # merged-and-auto-closed path pays nothing extra.
        return False
    linkage = _closing_linkage(
        closing_issue_references=closing_issue_references,
        issue_number=issue_number,
        pr_number=pr_number,
    )
    if linkage is None:
        return None
    if not linkage.closes(issue_number):
        logger.info(
            "PR #%d merged without registering issue #%d as a closing "
            "reference; treating as a deliberate non-closing merge — no "
            "close, stale pr-pending shed by terminal recovery",
            pr_number,
            issue_number,
        )
        return False
    if not merged_at:
        logger.warning(
            "Merged PR for issue #%d carries no merged_at; skipping close-on-"
            "merge fallback — open state alone is not evidence of a failed "
            "auto-close",
            issue_number,
        )
        return False
    try:
        auto_close_fired = closed_on_or_after(issue_number, merged_at)
    except RepositoryHostError:
        logger.warning(
            "Unable to read close events for merged PR close fallback: "
            "issue=#%d; retrying without mutation",
            issue_number,
        )
        return None
    if auto_close_fired:
        logger.info(
            "Issue #%d was closed at/after its PR merge and deliberately "
            "reopened; close-on-merge fallback will not re-close it",
            issue_number,
        )
        return False
    return True


def _closing_linkage(
    *,
    closing_issue_references: "Callable[[int], ClosingIssueReferencesRead]",
    issue_number: int,
    pr_number: int,
) -> "ClosingIssueReferencesRead | None":
    """The PR's registered closing linkage, or None when it is unreadable.

    Collapses the two unreadable shapes — a repository-host failure and a
    provider answer that carried no relation — into the single "we do not
    know" outcome the caller fails closed on. It never turns either into a
    KNOWN-empty set, which is what would shed a queue-gating label on a guess.
    """
    try:
        linkage = closing_issue_references(pr_number)
    except RepositoryHostError:
        logger.warning(
            "Unable to read closing-issue linkage for merged PR #%d "
            "(issue #%d); leaving the issue reconcilable without mutation",
            pr_number,
            issue_number,
        )
        return None
    if not linkage.is_known:
        logger.warning(
            "Closing-issue linkage for merged PR #%d (issue #%d) is "
            "unreadable; neither closing nor shedding pr-pending",
            pr_number,
            issue_number,
        )
        return None
    return linkage


def should_close_merged_issue(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    closing_issue_references: "Callable[[int], ClosingIssueReferencesRead]",
    state: "OrchestratorState",
    entry: "SessionHistoryEntry",
    pr_number: int,
    merged_at: str | None,
    now: float,
) -> bool | None:
    """Discovery-phase wrapper: the shared evidence rule plus queue-cache
    freshness bookkeeping for the issue read."""
    return close_on_merge_evidence(
        get_issue=get_issue,
        closed_on_or_after=closed_on_or_after,
        closing_issue_references=closing_issue_references,
        issue_number=entry.issue_number,
        pr_number=pr_number,
        merged_at=merged_at,
        on_issue_read=lambda: record_issue_refreshes(
            state, {entry.issue_number}, now,
        ),
    )


def run_close_on_merge_fallback(
    *,
    repository_host: object,
    action: "RecoverTerminalIssueAction",
    close: "Callable[[CloseIssueAction], ActionResult]",
) -> tuple[bool, str | None]:
    """Apply-time owner of the fallback close. Returns (close_applied, error).

    Revalidates the destructive precondition against live state immediately
    before the write — the planner's ``close_issue`` bit is advisory and can
    go stale in the discovery→apply gap (a human closing, or closing and
    deliberately reopening, in between). Only the shared evidence rule (issue
    open AND no close event since ``merged_at``) authorizes the close.

    Ordering: the close is the FIRST mutation of terminal recovery, before
    the label shed — a closed issue can never re-enter the work queue, so a
    later shed or history failure is safe and retryable. The reverse order
    would open a window (queue-gating labels shed, close failed, process
    restarted) where the first planning pass relaunches the issue through
    exactly the hole this fallback closes. A close that succeeds while
    history finalization fails reconciles terminal-via-issue-closure on the
    next pass — idempotent.

    A non-None error means fail WITHOUT any further mutation (no shed, no
    history): unreadable evidence or a failed close both leave the entry
    reconcilable for retry.
    """
    host = cast("RepositoryHost", repository_host)
    evidence = close_on_merge_evidence(
        get_issue=host.get_issue,
        closed_on_or_after=host.issue_closed_on_or_after,
        closing_issue_references=host.read_pr_closing_issue_references,
        issue_number=action.issue_number,
        pr_number=action.pr_number,
        merged_at=action.merged_at or None,
    )
    if evidence is None:
        return False, (
            "close-on-merge revalidation unreadable; awaiting-merge history "
            "left reconcilable for retry"
        )
    if not evidence:
        # Already closed, or deliberately reopened after an auto-close — no
        # destructive write; the caller proceeds with shed + history.
        return False, None
    result = close(CloseIssueAction(
        issue_number=action.issue_number,
        comment=close_on_merge_comment(action.pr_url, action.pr_number),
        reason=action.status_reason or action.reason,
    ))
    if not result.success:
        return False, (
            "close-on-merge fallback failed; awaiting-merge history left "
            f"reconcilable for retry: {result.error}"
        )
    return True, None


def close_on_merge_comment(pr_url: str, pr_number: int) -> str:
    """Explanatory comment posted when the fallback closes an issue.

    Must describe the ONLY condition that reaches it: the PR registered this
    issue as a closing reference and merged, yet GitHub's auto-close did not
    fire. A merge that registered nothing is a deliberate non-closing merge
    under #113 and never reaches this comment — see the module docstring.
    ``test_close_on_merge_comment_states_the_surviving_trigger`` pins the text
    to that trigger so it cannot drift back to the pre-#113 wording.
    """
    return (
        f"Closing: {pr_url or f'PR #{pr_number}'} registered this issue as a "
        "closing reference and merged, but GitHub's auto-close did not fire. "
        "The orchestrator closed it during awaiting-merge reconciliation. If "
        "this issue still has remaining scope, reopen it."
    )


def reconciliation_fact(
    *,
    entry: "SessionHistoryEntry",
    pr_number: int,
    status: AwaitingMergeTerminalStatus,
    reason: str,
    source: AwaitingMergeReconciliationSource,
    issue_open: bool = False,
    merged_at: str | None = None,
) -> DiscoveredAwaitingMergeReconciliation:
    return DiscoveredAwaitingMergeReconciliation(
        issue_number=entry.issue_number,
        pr_number=pr_number,
        pr_url=entry.pr_url or "",
        status=status,
        status_reason=reason,
        source=source,
        issue_open=issue_open,
        merged_at=merged_at,
    )


def pr_terminal_reason(status: AwaitingMergeTerminalStatus) -> str:
    if status == "merged":
        return "PR merged; awaiting merge reconciled"
    return "PR closed; awaiting merge reconciled"
