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
untouched), and carries the resulting ``merged_disposition`` on the
reconciliation fact so the Planner can order a close on the terminal-recovery
owner command (it was a bare ``issue_open`` bool until #113). Only a
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
owner. It answers the deliberate ``Refs #45`` merge outright: this issue is
simply not in the closing set, so no close, and the stale ``pr-pending`` is
shed so the still-open issue rejoins selection instead of parking forever.
That branch SUPERSEDES #6956, which closed the issue on exactly this
condition.

An EMPTY registration is the one shape the linkage alone cannot settle — it
means either "the author never asked for a close" or "the author asked in a
form GitHub failed to parse". Authorship settles it; see below.

The continuation shed is deliberately NARROW, and this is a correctness
property, not a detail (#113 review round 2). A continuation merge proves
exactly one thing: ``pr-pending`` is stale. The issue is intentionally still
OPEN, so its other labels — ``publish-failed``, a ``blocked:*`` reason, a
``publish-fail-count-N`` — describe its *current* condition and are untouched
by the merge. Routing this branch through the ordinary terminal recovery would
shed all of them, silently unblocking an issue on evidence that said nothing
about why it was blocked. So the disposition is carried as a typed
``MergedIssueDisposition`` and narrows the recovery's label authority to
``TerminalRecoveryLabelScope.STALE_PR_PENDING``; the terminal full shed is
reached only by the dispositions that genuinely mean "this issue's work has
landed".

AUTHORSHIP SETTLES AN EMPTY REGISTRATION (#113 review round 3)
=============================================================

An empty registration is only ambiguous for a PR this orchestrator did not
write. Every PR it opens is built by ``build_pr_body``, which puts
``Closes #<issue>`` on the first line and ``ORCHESTRATOR_PR_MARKER`` at the
bottom — so on a marked PR, "registered nothing" cannot mean "the author
deliberately left the issue open". The author is the orchestrator and it
always asked for the close; an empty registration can only mean GitHub's
word-boundary-sensitive parse was defeated. That is the literal-``\\n`` PR at
the top of this docstring, and it is a failed auto-close, unambiguously.

So the empty registration routes on authorship, and the relaunch regression an
earlier round of #113 accepted as a residual cost is not taken at all:

- marked body → failed auto-close. The pre-existing evidence rule decides, so
  the close still requires ``merged_at`` plus no close event since it — an
  intentionally reopened issue (porchpin #59) is still never re-closed;
- unmarked body → deliberate continuation. No close; only ``pr-pending`` sheds;
- unreadable body → ``UNREADABLE``. Fail closed, exactly as for an unreadable
  linkage: reading an unread body as "unmarked" would resolve a failed
  auto-close as a continuation on a guess.

A NON-empty registration that omits this issue never consults authorship.
GitHub demonstrably parsed that body and registered what it asked for, so this
issue's absence from the set is a positive fact about the author's intent —
including on a marked PR whose body a human rewrote before merging.

The repair for a defeated keyword on a hand-authored PR still belongs on the
PR, where GitHub can parse it; nothing here can tell that case apart from a
deliberate ``Refs`` merge, and this module does not guess.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Callable, cast

from ..domain.models import (
    ORCHESTRATOR_PR_MARKER,
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredAwaitingMergeReconciliation,
    MergedIssueDisposition,
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


class MergedPrAuthorship(Enum):
    """Who wrote a merged PR, as far as its body can establish.

    Consulted for exactly one condition — a merged PR whose registered closing
    linkage is EMPTY — because that is the only shape the linkage cannot
    settle on its own (see the module docstring).

    - :attr:`ORCHESTRATOR` — the body carries ``ORCHESTRATOR_PR_MARKER``, the
      established authorship signal (``startup_manager``,
      ``retrospective_review``). Every such body was written by
      ``build_pr_body``, which opens with ``Closes #<issue>``, so the author
      always asked for the close.
    - :attr:`FOREIGN` — the body was read and carries no marker. Someone else
      wrote it, so an empty registration is a statement of their intent.
    - :attr:`UNREADABLE` — the body could not be read. Never collapsed into
      ``FOREIGN``: that would decide a failed auto-close is a deliberate
      continuation on a guess, the same fail-open the linkage read refuses.
    """

    ORCHESTRATOR = "orchestrator"
    FOREIGN = "foreign"
    UNREADABLE = "unreadable"


def close_on_merge_evidence(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    closing_issue_references: "Callable[[int], ClosingIssueReferencesRead]",
    read_merged_pr_body: Callable[[], str | None],
    issue_number: int,
    pr_number: int,
    merged_at: str | None,
    on_issue_read: Callable[[], None] | None = None,
) -> MergedIssueDisposition:
    """What a merged PR establishes about its issue — the single owner of the
    destructive precondition, and of how much label authority the merge carries.

    :attr:`~MergedIssueDisposition.CLOSE_AND_RECOVER` only on positive evidence
    of the failure this fallback exists for: GitHub registered the PR as
    closing the issue, the issue is open, AND no ``closed`` event exists
    at/after the PR's ``merged_at`` — i.e. the auto-close never fired for this
    merge. An open issue that HAS a close event since the merge was auto-closed
    and then deliberately reopened; it is never re-closed. A missing
    ``merged_at`` means no evidence either way — never infer a destructive
    close from ``state == open`` alone.

    ``merged + issue OPEN`` cannot on its own tell a failed auto-close apart
    from a deliberate non-closing merge (a PR that says ``Refs #45``, landing
    partial work): the two are identical on every field this owner used to
    read. GitHub's REGISTERED closing linkage separates them, so it is read
    here and routes the outcome (#113):

    - registered as closing this issue → the pre-existing evidence rule below
      decides between ``CLOSE_AND_RECOVER`` and ``RECOVER``, unchanged;
    - registered a NON-empty set that omits this issue → ``CONTINUE``: a
      deliberate non-closing merge. No close, and the recovery's label
      authority narrows to the one label the merge actually proves stale
      (``pr-pending``), so the still-open issue rejoins selection instead of
      parking forever WITHOUT losing unrelated failure/blocking state that
      still describes it. This branch REVERSES #6956, which closed the issue
      on exactly this condition;
    - registered NOTHING → the linkage alone cannot say whether the author
      never asked for a close or asked in a form GitHub failed to parse, so
      ``read_merged_pr_body`` decides it. An orchestrator-authored body always
      asked (``build_pr_body`` writes ``Closes #<issue>``), so a marked PR is a
      failed auto-close and falls through to the evidence rule below; an
      unmarked one is a continuation; an unreadable one is ``UNREADABLE``;
    - unreadable linkage → ``UNREADABLE``. Fail closed: no close AND no shed.
      Treating an unreadable relation as an empty one would shed a
      queue-gating label on a guess.

    Net effect on the destructive writes, versus #6956: the close is strictly
    NARROWER — it now requires either a registered reference or an
    orchestrator-authored body, on top of every pre-existing condition, so no
    issue closes today that would not have closed before; and the continuation
    branch removes strictly fewer labels than the terminal recovery it used to
    route through.

    Called from BOTH phases: discovery (to plan the close attempt) and the
    apply-time owner command, which revalidates immediately before the
    destructive write — the planner's bit is advisory and can go stale in the
    discovery→apply gap (a human closing and reopening in between, or editing
    the merged PR's body). Discovery already holds the PR, so its body costs
    nothing there; apply re-reads it live, exactly as it does the issue state.

    Returns ``UNREADABLE`` when the evidence cannot be read (transient
    repository-host error) so the caller can retry without mutating: fail-open
    recreates the relaunch bug, and raising aborts the caller's tick. An issue
    the host reports as missing is treated as not-open — there is nothing to
    close, and nothing survives that a narrow shed would protect.
    """
    try:
        issue = get_issue(issue_number)
    except RepositoryHostError:
        logger.warning(
            "Unable to check issue state for merged PR close fallback: "
            "issue=#%d; retrying without mutation",
            issue_number,
        )
        return MergedIssueDisposition.UNREADABLE
    if issue is None:
        return MergedIssueDisposition.RECOVER
    if on_issue_read is not None:
        on_issue_read()
    if normalized_state(issue.state) == "closed":
        # Terminal already; no linkage read needed, so the common
        # merged-and-auto-closed path pays nothing extra.
        return MergedIssueDisposition.RECOVER
    linkage = _closing_linkage(
        closing_issue_references=closing_issue_references,
        issue_number=issue_number,
        pr_number=pr_number,
    )
    if linkage is None:
        return MergedIssueDisposition.UNREADABLE
    if not linkage.closes(issue_number):
        unregistered = _unregistered_merge_disposition(
            linkage=linkage,
            read_merged_pr_body=read_merged_pr_body,
            issue_number=issue_number,
            pr_number=pr_number,
        )
        if unregistered is not None:
            return unregistered
        # None means "this is a failed auto-close after all" — fall through to
        # the same evidence rule a registered linkage reaches.
    return _failed_auto_close_disposition(
        closed_on_or_after=closed_on_or_after,
        issue_number=issue_number,
        merged_at=merged_at,
    )


def _unregistered_merge_disposition(
    *,
    linkage: "ClosingIssueReferencesRead",
    read_merged_pr_body: Callable[[], str | None],
    issue_number: int,
    pr_number: int,
) -> MergedIssueDisposition | None:
    """The verdict for a merge GitHub did not register as closing this issue.

    Returns ``None`` — and only for an orchestrator-authored PR that
    registered NOTHING — to mean "not a continuation after all; this is a
    failed auto-close", leaving the destructive decision to the caller's
    unchanged evidence rule. Every other shape answers here.
    """
    if linkage.issue_numbers:
        # GitHub parsed this body and registered what it asked for; this
        # issue's absence is a positive fact about the author's intent, so
        # authorship cannot overturn it (see the module docstring).
        logger.info(
            "PR #%d merged registering closing references %s, which do not "
            "include issue #%d; treating as a deliberate non-closing merge — "
            "no close, and only the stale pr-pending is shed",
            pr_number,
            list(linkage.issue_numbers),
            issue_number,
        )
        return MergedIssueDisposition.CONTINUE
    authorship = _merged_pr_authorship(
        read_merged_pr_body=read_merged_pr_body,
        issue_number=issue_number,
        pr_number=pr_number,
    )
    if authorship is MergedPrAuthorship.UNREADABLE:
        return MergedIssueDisposition.UNREADABLE
    if authorship is MergedPrAuthorship.FOREIGN:
        logger.info(
            "PR #%d merged without registering any closing reference and was "
            "not authored by the orchestrator; treating as a deliberate "
            "non-closing merge for issue #%d — no close, and only the stale "
            "pr-pending is shed",
            pr_number,
            issue_number,
        )
        return MergedIssueDisposition.CONTINUE
    logger.warning(
        "Orchestrator-authored PR #%d merged without registering issue #%d as "
        "a closing reference; its body always asks for the close, so this is "
        "a defeated closing keyword (failed auto-close), not a deliberate "
        "continuation",
        pr_number,
        issue_number,
    )
    return None


def _merged_pr_authorship(
    *,
    read_merged_pr_body: Callable[[], str | None],
    issue_number: int,
    pr_number: int,
) -> MergedPrAuthorship:
    """Whether the orchestrator wrote the merged PR, by its body marker.

    A body that cannot be read — a repository-host failure, or a PR the host
    no longer reports — is ``UNREADABLE``, never ``FOREIGN``. The caller fails
    closed on it and retries, rather than shedding a queue-gating label off an
    unread body.
    """
    try:
        body = read_merged_pr_body()
    except RepositoryHostError:
        logger.warning(
            "Unable to read merged PR #%d's body for the close-on-merge "
            "authorship check (issue #%d); leaving the issue reconcilable "
            "without mutation",
            pr_number,
            issue_number,
        )
        return MergedPrAuthorship.UNREADABLE
    if body is None:
        logger.warning(
            "Merged PR #%d is no longer readable for the close-on-merge "
            "authorship check (issue #%d); leaving the issue reconcilable "
            "without mutation",
            pr_number,
            issue_number,
        )
        return MergedPrAuthorship.UNREADABLE
    if ORCHESTRATOR_PR_MARKER in body:
        return MergedPrAuthorship.ORCHESTRATOR
    return MergedPrAuthorship.FOREIGN


def _failed_auto_close_disposition(
    *,
    closed_on_or_after: "Callable[[int, str], bool]",
    issue_number: int,
    merged_at: str | None,
) -> MergedIssueDisposition:
    """The unchanged reopen guard, reached once the merge is established as a
    failed auto-close (registered linkage, or an orchestrator-authored PR that
    registered nothing).

    A missing ``merged_at`` means no evidence either way, and a close event
    at/after the merge means the auto-close DID fire and a human deliberately
    reopened (porchpin #59). Neither ever earns the destructive close.
    """
    if not merged_at:
        logger.warning(
            "Merged PR for issue #%d carries no merged_at; skipping close-on-"
            "merge fallback — open state alone is not evidence of a failed "
            "auto-close",
            issue_number,
        )
        return MergedIssueDisposition.RECOVER
    try:
        auto_close_fired = closed_on_or_after(issue_number, merged_at)
    except RepositoryHostError:
        logger.warning(
            "Unable to read close events for merged PR close fallback: "
            "issue=#%d; retrying without mutation",
            issue_number,
        )
        return MergedIssueDisposition.UNREADABLE
    if auto_close_fired:
        logger.info(
            "Issue #%d was closed at/after its PR merge and deliberately "
            "reopened; close-on-merge fallback will not re-close it",
            issue_number,
        )
        return MergedIssueDisposition.RECOVER
    return MergedIssueDisposition.CLOSE_AND_RECOVER


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


def merged_issue_disposition(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    closing_issue_references: "Callable[[int], ClosingIssueReferencesRead]",
    state: "OrchestratorState",
    entry: "SessionHistoryEntry",
    pr_number: int,
    merged_at: str | None,
    pr_body: str,
    now: float,
) -> MergedIssueDisposition:
    """Discovery-phase wrapper: the shared evidence rule plus queue-cache
    freshness bookkeeping for the issue read.

    Discovery already holds the merged PR, so its ``pr_body`` is handed in
    rather than re-fetched — the authorship check costs no GitHub call on this
    path at all. The apply-time owner re-reads it live instead, because like
    the issue state it can change in the discovery→apply gap.
    """
    return close_on_merge_evidence(
        get_issue=get_issue,
        closed_on_or_after=closed_on_or_after,
        closing_issue_references=closing_issue_references,
        read_merged_pr_body=lambda: pr_body,
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
    deliberately reopening, in between; or editing the merged PR's body, which
    is why the authorship read below is live rather than a discovery-time
    bit). Only the shared evidence rule (issue open AND no close event since
    ``merged_at``) authorizes the close.

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
    disposition = close_on_merge_evidence(
        get_issue=host.get_issue,
        closed_on_or_after=host.issue_closed_on_or_after,
        closing_issue_references=host.read_pr_closing_issue_references,
        read_merged_pr_body=lambda: _live_pr_body(host, action.pr_number),
        issue_number=action.issue_number,
        pr_number=action.pr_number,
        merged_at=action.merged_at or None,
    )
    if disposition is MergedIssueDisposition.UNREADABLE:
        return False, (
            "close-on-merge revalidation unreadable; awaiting-merge history "
            "left reconcilable for retry"
        )
    if disposition is not MergedIssueDisposition.CLOSE_AND_RECOVER:
        # Already closed, deliberately reopened after an auto-close, or a
        # continuation merge — no destructive write; the caller proceeds with
        # the shed its own (possibly narrowed) label scope authorizes.
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


def _live_pr_body(host: "RepositoryHost", pr_number: int) -> str | None:
    """The merged PR's body, read live for the apply-time authorship check.

    ``None`` when the host no longer reports the PR — the caller treats that
    as unreadable and retries rather than inferring authorship from silence.
    A ``RepositoryHostError`` propagates for the same treatment. Reached only
    when the linkage came back registering nothing, so the ordinary
    merged-and-registered close pays no extra PR read.
    """
    pr = host.get_pr(pr_number)
    return None if pr is None else pr.body


def close_on_merge_comment(pr_url: str, pr_number: int) -> str:
    """Explanatory comment posted when the fallback closes an issue.

    Must describe the conditions that reach it, and only those: the PR merged
    without GitHub's auto-close firing, AND either it registered this issue as
    a closing reference or the orchestrator authored it (in which case its body
    asked for the close and GitHub's parse was defeated). A hand-authored merge
    that registered nothing is a deliberate non-closing merge under #113 and
    never reaches this comment — see the module docstring.
    ``test_close_on_merge_comment_states_the_surviving_triggers`` pins the text
    to those triggers so it cannot drift back to the pre-#113 wording, which
    asserted the opposite of the registered case.
    """
    return (
        f"Closing: {pr_url or f'PR #{pr_number}'} merged, but GitHub's "
        "auto-close did not fire. The orchestrator closed this issue during "
        "awaiting-merge reconciliation — the PR either registered it as a "
        "closing reference, or was authored by the orchestrator, whose PR "
        "bodies always ask to close their issue. If this issue still has "
        "remaining scope, reopen it."
    )


def reconciliation_fact(
    *,
    entry: "SessionHistoryEntry",
    pr_number: int,
    status: AwaitingMergeTerminalStatus,
    reason: str,
    source: AwaitingMergeReconciliationSource,
    merged_disposition: MergedIssueDisposition = MergedIssueDisposition.RECOVER,
    merged_at: str | None = None,
) -> DiscoveredAwaitingMergeReconciliation:
    return DiscoveredAwaitingMergeReconciliation(
        issue_number=entry.issue_number,
        pr_number=pr_number,
        pr_url=entry.pr_url or "",
        status=status,
        status_reason=reason,
        source=source,
        merged_disposition=merged_disposition,
        merged_at=merged_at,
    )


def pr_terminal_reason(status: AwaitingMergeTerminalStatus) -> str:
    if status == "merged":
        return "PR merged; awaiting merge reconciled"
    return "PR closed; awaiting merge reconciled"
