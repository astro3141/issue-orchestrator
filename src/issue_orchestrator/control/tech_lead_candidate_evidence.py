"""What the independent Reviewer already decided about each audited candidate.

#344 measured the gap this closes: a Tech Lead batch review received the PR's
diff and metadata and *nothing about the review that preceded it*, while the
shipped Tech Lead data-source contract forbids the agent from fetching missing
GitHub context itself. So the session could not establish the one prerequisite
the merge contract assumes — that an independent reviewer approved THIS commit
— and could not go and find out either.

This module answers that question per candidate, and the manifest downloader
stages the answers into the same ``tech-lead-data`` directory the manifest lives
in — so the agent reads a file instead of making a network call it is not
allowed to make. The two halves are split on the layer boundary they belong to:
the policy of what counts as complete evidence is control's and lives here, and
WRITING the file beside the manifest is the downloader's, which is already the
owner of everything a batch review reads about its candidates. Control builds
the set; nothing in this module touches a filesystem.

Three properties are the contract:

* **Exact-candidate, not PR-level.** Every fact is read against
  ``(issue, candidate_sha)``. A ``code-reviewed`` label outlives the candidate
  that earned it, so it is not evidence and is never consulted here; a verdict
  bound to some other commit is a :attr:`~..domain.tech_lead_candidate.
  TechLeadCandidateEvidence.gap`, not a near-miss.
* **Nothing agent-authored.** The verdict and its commit come from the
  orchestrator's own durable candidate record, and the publication half from
  the same :class:`~.publication_authority.PublicationVerdictReader` every
  other reader of that verdict uses.
* **Absence is loud.** Each entry states, in one string per owner, exactly why
  it does not establish the fact. The completion gate refuses a PASS whose
  evidence carries a gap, so an unstaged, unreadable, misbound or
  changes-requested candidate cannot be waved through — and an operator reading
  the staged file can see which of those it was.
* **One string per owner, not one per entry (#370).** The reviewer's approval
  and the repository's mandatory validation are two facts established by two
  owners, so they carry two gaps — ``gap`` and ``validation_gap``. They were
  one string until the Tech Lead model session stopped executing repository
  validation itself: with the validation owner now outside the model sandbox
  entirely, a settlement that could only say "something about this commit was
  not shown" could not say whether the missing thing was a review or a
  validation run, and those have different remedies.

Which ISSUE a pull request belongs to is read from the branch, the way every
other PR-to-issue association in this orchestrator is read. That is an
association, not an identity: the candidate's identity is the observed head
SHA, and it never comes from a branch name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..domain.attempt import AttemptKey
from ..domain.branch_naming import extract_issue_number_from_branch
from ..domain.review_verdict_binding import ReviewVerdictOutcome
from ..domain.tech_lead_candidate import (
    TechLeadCandidate,
    TechLeadCandidateEvidence,
    TechLeadCandidateEvidenceSet,
)
from ..domain.tech_lead_manifest import PRToReview
from ..ports.tech_lead_candidate_evidence import TechLeadCandidateEvidenceSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.review_verdict_binding import BoundReviewVerdict
    from ..infra.validation_profiles import ValidationProfileRegistry
    from ..ports import RepositoryHost
    from ..ports.candidate_review_verdicts import CandidateReviewVerdictStore
    from ..ports.execution_identity_store import CandidateExecutionIdentityStore
    from .publication_authority import PublicationVerdictReader
    from .publication_evidence import PublicationCertification

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DurableCandidateEvidence:
    """Reads the durable per-candidate evidence the orchestrator itself filed.

    Two narrow views over one record: what the reviewer decided about the
    candidate, and who executed it. Both are the same views the Foundation
    admission evidence is written through, so this reader cannot see a
    different answer from the gate that will later consume it.
    """

    review_verdicts: "CandidateReviewVerdictStore"
    execution_identities: "CandidateExecutionIdentityStore"
    publication_verdict: "PublicationVerdictReader"
    profiles: "ValidationProfileRegistry"

    def evidence_for(
        self, entry: PRToReview, *, repository_host: "RepositoryHost"
    ) -> TechLeadCandidateEvidence:
        candidate = entry.candidate()
        if not candidate.is_bound:
            unbound = (
                f"PR #{candidate.pr_number} was selected without an"
                " observable head commit, so no evidence can be bound to it"
            )
            return TechLeadCandidateEvidence(
                candidate=candidate, gap=unbound, validation_gap=unbound
            )
        issue_number = extract_issue_number_from_branch(entry.branch)
        if issue_number is None:
            unlocatable = (
                f"branch {entry.branch!r} names no issue, so the durable"
                " candidate record for this commit cannot be located"
            )
            return TechLeadCandidateEvidence(
                candidate=candidate, gap=unlocatable, validation_gap=unlocatable
            )
        key = AttemptKey(repository_host.create_issue_key(issue_number), candidate.head_sha)
        try:
            verdict = self.review_verdicts.read(key)
            identities = self.execution_identities.read(key)
        except (OSError, ValueError) as exc:
            # A damaged authority artifact is not an absent one, and it is not
            # a crash either: the batch review still runs, this candidate
            # simply cannot be PASSed.
            logger.error(
                "[tech_lead] Candidate evidence for PR #%d @ %s is unreadable: %s",
                candidate.pr_number,
                candidate.short_sha,
                exc,
            )
            unreadable = f"the durable candidate record could not be read: {exc}"
            return TechLeadCandidateEvidence(
                candidate=candidate,
                issue_number=issue_number,
                gap=unreadable,
                validation_gap=unreadable,
            )
        certification = self.publication_verdict.certifies_candidate(
            issue_key=key.issue_key,
            head_sha=candidate.head_sha,
            profiles=self.profiles,
        )
        return TechLeadCandidateEvidence(
            candidate=candidate,
            issue_number=issue_number,
            reviewer_verdict=verdict.verdict.value if verdict is not None else "",
            reviewed_sha=verdict.reviewed_sha if verdict is not None else "",
            decided_at=verdict.decided_at if verdict is not None else "",
            completed_rounds=verdict.completed_rounds if verdict is not None else 0,
            reviewer_principal=(
                identities.reviewer.principal.agent_label
                if identities is not None
                else ""
            ),
            actor_principal=(
                identities.actor.principal.agent_label if identities is not None else ""
            ),
            publication_certified=certification.admitted,
            publication_reason=certification.reason,
            gap=_reviewer_gap(candidate, verdict=verdict),
            validation_gap=_validation_gap(candidate, certification=certification),
        )


def _reviewer_gap(
    candidate: TechLeadCandidate,
    *,
    verdict: "BoundReviewVerdict | None",
) -> str:
    """Why this candidate is not proven independently reviewed, or ``""``.

    Ordered most-specific first, and every branch refuses: a PASS may only rest
    on a positive, exact answer about this commit.

    The publication half used to end this same chain. It is
    :func:`_validation_gap` now (#370), and the split is not cosmetic: the two
    facts are established by two owners, and the one the Tech Lead model
    session no longer executes at all — the repository's mandatory validation —
    must be nameable on its own or settlement cannot say which of the two it
    refused a PASS for.
    """
    if verdict is None:
        return (
            "no independent reviewer verdict is recorded for"
            f" {candidate.short_sha}; a PR-level review label is evidence about"
            " the pull request, not about this commit"
        )
    # Defence for a future non-attempt-backed implementation of the verdict
    # port, and unreachable through the shipped one: ``Attempt.__post_init__``
    # already rejects a stored verdict that does not cover its own key, so
    # ``AttemptReviewVerdictStore.read`` raises on a misbound record and the
    # caller reports it as "unreadable" before this line is asked. A port whose
    # store does not enforce that invariant would reach here, and the gap it
    # needs is this one rather than the absence above.
    if not candidate.covers(verdict.reviewed_sha):
        return (
            f"the recorded reviewer verdict is bound to"
            f" {verdict.reviewed_sha[:12]}, which is other work than"
            f" {candidate.short_sha}"
        )
    if verdict.verdict is not ReviewVerdictOutcome.APPROVED:
        return (
            f"the independent reviewer did not approve {candidate.short_sha}"
            f" (verdict={verdict.verdict.value})"
        )
    return ""


def _validation_gap(
    candidate: TechLeadCandidate,
    *,
    certification: "PublicationCertification",
) -> str:
    """Why the orchestrator's own gate is not proven passed here, or ``""``.

    The certification reader already refuses in every direction that matters —
    no receipt for this commit, a receipt from a contract that is no longer the
    required one, a failed or unreadable run — and carries its own reason for
    which one it was. This adds the candidate's identity to that reason and
    nothing else: the answer is the validation owner's, not this module's, and
    re-deriving it here is how two readers of one gate start disagreeing.
    """
    if certification.admitted:
        return ""
    return (
        f"{candidate.short_sha} has no publication-gate certification"
        f" ({certification.reason}); the repository's mandatory validation is"
        " executed by the orchestrator, so a candidate without its receipt was"
        " never shown to pass it"
    )


def build_candidate_evidence(
    entries: Sequence[PRToReview],
    *,
    source: TechLeadCandidateEvidenceSource,
    repository_host: "RepositoryHost",
) -> TechLeadCandidateEvidenceSet:
    """Assemble the evidence set for every audited candidate."""
    return TechLeadCandidateEvidenceSet(
        entries=tuple(
            source.evidence_for(entry, repository_host=repository_host)
            for entry in entries
        )
    )


__all__ = [
    "DurableCandidateEvidence",
    "build_candidate_evidence",
]
