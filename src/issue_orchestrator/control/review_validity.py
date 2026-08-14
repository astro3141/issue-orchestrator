"""Shared validity checks for pending code reviews.

One owner for a question three callers ask at different moments — the PR
scanner when it discovers work, startup when it rebuilds the queue after a
restart, and the launcher just before it spawns the reviewer. They must not be
able to answer it differently, so the refusals live here as ordered decision
tables rather than as branches spelled out at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .publication_authority import publication_gate_failed

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from .label_manager import LabelManager


@dataclass(frozen=True)
class ReviewValidity:
    """Whether a queued/discovered review is still valid to process."""

    valid: bool
    reason: str
    issue_labels: tuple[str, ...] = ()
    pr_labels: tuple[str, ...] = ()
    blocking_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Refusal:
    """One reason a review may not proceed, and the labels that prove it."""

    reason: str
    blocking_labels: tuple[str, ...] = ()


def _refuse_if(reason: str, refused: bool) -> _Refusal | None:
    return _Refusal(reason) if refused else None


def _refuse_blocking(reason: str, blocking: Sequence[str]) -> _Refusal | None:
    return _Refusal(reason, tuple(blocking)) if blocking else None


def _first(refusals: Sequence["_Refusal | None"]) -> _Refusal | None:
    for refusal in refusals:
        if refusal is not None:
            return refusal
    return None


def _pr_refusal(
    *,
    config: "Config",
    label_manager: "LabelManager",
    pr: "PRInfo",
    review_label_confirmed: bool,
) -> _Refusal | None:
    """Why the PR itself is not a valid review subject, if it is not."""
    review_label_missing = bool(
        config.code_review_label
        and not review_label_confirmed
        and config.code_review_label not in pr.labels
    )
    return _first(
        (
            _refuse_if("pr_not_open", pr.state.lower() != "open"),
            _refuse_if("review_label_missing", review_label_missing),
            _refuse_blocking("pr_blocked", label_manager.get_blocking(pr.labels)),
            _refuse_if(
                "pr_needs_rework", label_manager.needs_rework in pr.labels
            ),
        )
    )


def _issue_refusal(
    *,
    label_manager: "LabelManager",
    issue: "Issue",
) -> _Refusal | None:
    """Why the linked issue withholds authority for a review, if it does.

    ``issue_publication_gate_failed`` is the ordering rule of #21: validation
    precedes review, so a candidate whose publication gate did not pass has no
    authority to advance, however the review was triggered. The trigger state
    an earlier candidate left on the PR is not authority for this one (#45).
    """
    return _first(
        (
            _refuse_blocking(
                "issue_blocked", label_manager.get_blocking(issue.labels)
            ),
            _refuse_if(
                "issue_publication_gate_failed",
                publication_gate_failed(label_manager, issue.labels),
            ),
            _refuse_if(
                "issue_needs_rework", label_manager.needs_rework in issue.labels
            ),
        )
    )


def evaluate_review_validity(
    *,
    config: "Config",
    label_manager: "LabelManager",
    issue: "Issue | None",
    pr: "PRInfo | None" = None,
    review_label_confirmed: bool = False,
) -> ReviewValidity:
    """Return whether a review is still valid for queue/launch processing."""
    issue_labels = tuple(issue.labels) if issue is not None else ()
    pr_labels = tuple(pr.labels) if pr is not None else ()

    refusal = (
        _pr_refusal(
            config=config,
            label_manager=label_manager,
            pr=pr,
            review_label_confirmed=review_label_confirmed,
        )
        if pr is not None
        else None
    )
    if refusal is None and issue is not None:
        refusal = _issue_refusal(label_manager=label_manager, issue=issue)

    if refusal is None:
        return ReviewValidity(
            valid=True,
            reason="ok",
            issue_labels=issue_labels,
            pr_labels=pr_labels,
        )
    return ReviewValidity(
        valid=False,
        reason=refusal.reason,
        issue_labels=issue_labels,
        pr_labels=pr_labels,
        blocking_labels=refusal.blocking_labels,
    )
