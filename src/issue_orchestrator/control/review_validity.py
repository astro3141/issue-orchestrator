"""Shared validity checks for pending code reviews.

One owner for a question three callers ask at different moments — the PR
scanner when it discovers work, startup when it rebuilds the queue after a
restart, and the launcher just before it spawns the reviewer. They must not be
able to answer it differently, so the refusals live here as ordered decision
tables rather than as branches spelled out at each call site.

The tables have two kinds of entry, and the difference matters. The label-shaped
refusals are *negative*: they name reasons a review may not proceed, and a
review passes them by their absence, which is why leftover trigger state from an
earlier candidate could once carry a rejected one through. The last entry is
*positive*: the candidate must produce evidence that it, specifically, cleared
the publication gate (:mod:`.publication_evidence`). Absence refuses there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from .label_manager import LabelManager
    from .publication_authority import PublicationVerdictReader


@dataclass(frozen=True)
class ReviewValidity:
    """Whether a queued/discovered review is still valid to process."""

    valid: bool
    reason: str
    issue_labels: tuple[str, ...] = ()
    pr_labels: tuple[str, ...] = ()
    blocking_labels: tuple[str, ...] = ()
    candidate_uncertified: bool = False
    """Whether the refusal was the *positive* rule, not a label-shaped one.

    The label-shaped refusals all decay on their own: a block is lifted, a
    rework finishes, a PR is reopened. This one does not — it says the commit
    at the PR's head has no publication receipt, and only a new candidate can
    change that. A PR the orchestrator did not create (a human-opened one
    carrying the review trigger) will therefore wear this refusal forever, so
    readers that report refusals announce this one rather than leaving it in a
    console log nobody is watching (#45).
    """


@dataclass(frozen=True)
class _Refusal:
    """One reason a review may not proceed, and the labels that prove it."""

    reason: str
    blocking_labels: tuple[str, ...] = ()
    candidate_uncertified: bool = False


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
    publication_verdict: "PublicationVerdictReader",
) -> _Refusal | None:
    """Why the linked issue withholds authority for a review, if it does.

    ``issue_publication_gate_failed`` is the ordering rule of #21: validation
    precedes review, so a candidate whose publication gate did not pass has no
    authority to advance, however the review was triggered. The trigger state
    an earlier candidate left on the PR is not authority for this one (#45).
    A refusal the gate could not write to the issue counts here too: it is the
    same verdict, and only its record is missing.

    All three refusals here are issue-scoped, which is why they cannot be the
    whole rule: they say the issue is currently unrefused, not that *this
    candidate* was ever gated. :func:`_candidate_refusal` asks that.
    """
    return _first(
        (
            _refuse_blocking(
                "issue_blocked", label_manager.get_blocking(issue.labels)
            ),
            _refuse_if(
                "issue_publication_gate_failed",
                publication_verdict.refuses_issue(
                    label_manager, issue.labels, issue_number=issue.number
                ),
            ),
            _refuse_if(
                "issue_needs_rework", label_manager.needs_rework in issue.labels
            ),
        )
    )


def _candidate_refusal(
    *,
    config: "Config",
    issue: "Issue | None",
    pr: "PRInfo | None",
    publication_verdict: "PublicationVerdictReader",
) -> _Refusal | None:
    """Why this candidate has not proven it cleared the publication gate.

    The positive requirement, and the one that does not decay: it names the
    exact commit a reviewer would see, so no amount of leftover label state can
    stand in for it, and a head that moves after queueing is judged as the new
    head rather than on the old one's evidence (#45).

    Both identity halves are read from the live facts this seam was already
    handed — the issue's canonical key and the PR's current head — and either
    being absent refuses inside the evidence owner rather than here.
    """
    certification = publication_verdict.certifies_candidate(
        issue_key=issue.key if issue is not None else None,
        head_sha=pr.head_sha if pr is not None else None,
        profiles=config.validation_profiles(),
    )
    if certification.admitted:
        return None
    return _Refusal(certification.reason, candidate_uncertified=True)


def evaluate_review_validity(
    *,
    config: "Config",
    label_manager: "LabelManager",
    issue: "Issue | None",
    publication_verdict: "PublicationVerdictReader",
    pr: "PRInfo | None" = None,
    review_label_confirmed: bool = False,
) -> ReviewValidity:
    """Return whether a review is still valid for queue/launch processing.

    ``publication_verdict`` is required rather than defaulted, and arrives as
    one collaborator rather than as its separate records: a call site that
    silently supplied an empty one — or only some of them — would answer this
    question differently from the others, the drift this single seam exists to
    make impossible (#45).

    The refusals are ordered cheapest-and-most-specific first, and the
    candidate's publication evidence is asked last so a PR that is closed, or
    an issue that is blocked, still reports *that* rather than the generic
    "this candidate was never gated".
    """
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
        refusal = _issue_refusal(
            label_manager=label_manager,
            issue=issue,
            publication_verdict=publication_verdict,
        )
    if refusal is None:
        refusal = _candidate_refusal(
            config=config,
            issue=issue,
            pr=pr,
            publication_verdict=publication_verdict,
        )

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
        candidate_uncertified=refusal.candidate_uncertified,
    )
