"""What a completion LEARNED about its issue's pull request, and the reading of it.

A bare ``pr_url: str | None`` cannot carry this. ``None`` is produced by three
different events — the branch carries no pull request, the session's known pull
request could not be READ, and the lookup was never attempted — and #337's
terminal disposition must treat them differently: only an OBSERVED absence is
evidence that nothing is in flight. An unreadable lookup is a fact about the
forge, not about the issue, and reading it as "no pull request" is what would
let a rework session whose pull request is open and unmerged be closed as an
evidence-only run (#337 round 3, F2).

The fact and the reading of it live together because the verdict is only as
good as the read that produced it: every exit from :func:`observe_pull_request`
states which of the three it is, so no caller can construct an absence it did
not observe by simply failing to look.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..domain.models import Session, SessionStatus
from ..domain.session_key import TaskKind
from ..infra.logging_config import log_context
from ..ports import RepositoryHost
from ..ports.pull_request_tracker import PRInfo

logger = logging.getLogger(__name__)


class PullRequestPresence(Enum):
    """The three answers a pull-request lookup can actually give.

    ``UNKNOWN`` is the fail-closed value, and it is what every path that did not
    look — a non-``COMPLETED`` status, a task kind whose lookup is skipped, a
    raised read — reports.
    """

    OBSERVED_PRESENT = "observed_present"
    OBSERVED_NONE = "observed_none"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PullRequestObservation:
    """One completion's read of whether its issue has a pull request.

    Carries the verdict together with what the read found, so a consumer that
    needs the url (history, trace events, the create_pr error downgrade) and a
    consumer that needs the VERDICT (the result-only terminal disposition) take
    the same fact rather than one re-deriving the other from a missing string.

    ``detail`` is the reading owner's own sentence, so the log of a refused
    disposition says which read failed and on what.
    """

    presence: PullRequestPresence
    url: str | None = None
    number: int | None = None
    infos: tuple[PRInfo, ...] = ()
    detail: str = ""

    @classmethod
    def observed(
        cls,
        *,
        url: str,
        number: int | None,
        infos: Sequence[PRInfo] = (),
        detail: str = "",
    ) -> "PullRequestObservation":
        """A pull request for this issue was read and exists."""
        return cls(
            presence=PullRequestPresence.OBSERVED_PRESENT,
            url=url,
            number=number,
            infos=tuple(infos),
            detail=detail,
        )

    @classmethod
    def observed_none(cls, detail: str) -> "PullRequestObservation":
        """The lookup SUCCEEDED and found no pull request for this issue."""
        return cls(presence=PullRequestPresence.OBSERVED_NONE, detail=detail)

    @classmethod
    def unknown(cls, detail: str) -> "PullRequestObservation":
        """Nothing was learned — not looked up, or the read failed."""
        return cls(presence=PullRequestPresence.UNKNOWN, detail=detail)

    @property
    def observed_absent(self) -> bool:
        """True only for a lookup that ran and found nothing."""
        return self.presence is PullRequestPresence.OBSERVED_NONE


def observe_pull_request(
    session: Session,
    status: SessionStatus,
    *,
    repository_host: RepositoryHost,
    pr_url_hint: str | None = None,
) -> PullRequestObservation:
    """Read whether this completion's issue has a pull request.

    ``pr_url_hint`` short-circuits the branch lookup (dry-run mode).

    The two paths that do not look at all — a non-``COMPLETED`` status and a
    retrospective review, whose pull request is not this session's subject —
    report ``UNKNOWN`` rather than an absence they never checked.
    """
    if status != SessionStatus.COMPLETED:
        return PullRequestObservation.unknown(
            f"session status is {status.value}; no pull request was looked up"
        )

    if session.key.task == TaskKind.RETROSPECTIVE_REVIEW:
        return PullRequestObservation.unknown(
            "retrospective review sessions do not look up a pull request"
        )

    if pr_url_hint:
        return _observe_from_hint(session, pr_url_hint, repository_host)

    return _observe_from_branch_or_review_fallback(session, repository_host)


def _observe_from_hint(
    session: Session,
    pr_url_hint: str,
    repository_host: RepositoryHost,
) -> PullRequestObservation:
    """The hint IS the observation: the processor opened or reused that PR."""
    pr_number: int | None = None
    prs: list[PRInfo] = []

    match = re.search(r"/pull/(\d+)", pr_url_hint)
    if match:
        pr_number = int(match.group(1))
        try:
            pr_info = repository_host.get_pr(pr_number)
        except Exception as e:
            logger.warning("Failed to fetch PR %s for PR hint: %s", pr_number, e)
        else:
            if pr_info:
                prs = [pr_info]

    logger.info(
        "[PR_HINT] Using PR from completion processor: %s (number=%s)",
        pr_url_hint,
        pr_number,
        extra=log_context(
            issue_key=session.key.issue.stable_id(), session_id=session.terminal_id
        ),
    )
    return PullRequestObservation.observed(
        url=pr_url_hint,
        number=pr_number,
        infos=prs,
        detail=f"completion processor reported pull request {pr_url_hint}",
    )


def _observe_from_branch_or_review_fallback(
    session: Session,
    repository_host: RepositoryHost,
) -> PullRequestObservation:
    logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
    start = time.monotonic()
    pr_infos = repository_host.get_prs_for_branch(session.branch_name)
    duration = time.monotonic() - start
    logger.info(
        "Fetched PRs for branch in %.2fs: branch=%s count=%d",
        duration,
        session.branch_name,
        len(pr_infos),
        extra=log_context(
            issue_key=session.key.issue.stable_id(), session_id=session.terminal_id
        ),
    )
    if pr_infos:
        return PullRequestObservation.observed(
            url=pr_infos[0].url,
            number=pr_infos[0].number,
            infos=pr_infos,
            detail=f"branch {session.branch_name} carries an open pull request",
        )

    if session.pr_number is None:
        return PullRequestObservation.observed_none(
            f"branch {session.branch_name} carries no pull request and the"
            " session references none"
        )

    try:
        review_pr = repository_host.get_pr(session.pr_number)
    except Exception as e:
        logger.warning(
            "Failed to fetch PR %s for review session fallback: %s",
            session.pr_number,
            e,
        )
        # A read that RAISED proves nothing about the pull request this session
        # was launched against — and this session HAS one, which is precisely
        # the fact a terminal close must not be allowed to ignore.
        return PullRequestObservation.unknown(
            f"pull request #{session.pr_number} for this session could not be"
            f" read: {e}"
        )

    if review_pr:
        return PullRequestObservation.observed(
            url=review_pr.url,
            number=review_pr.number,
            infos=[review_pr],
            detail=f"session references pull request {review_pr.url}",
        )

    return PullRequestObservation.observed_none(
        f"branch {session.branch_name} carries no pull request and"
        f" #{session.pr_number} no longer exists"
    )


__all__ = [
    "PullRequestObservation",
    "PullRequestPresence",
    "observe_pull_request",
]
