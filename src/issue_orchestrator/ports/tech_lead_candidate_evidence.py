"""Port for the exact-candidate evidence a Tech Lead batch review is staged with.

The manifest downloader materializes everything a batch-review session reads
about its candidates. Since #345 that includes what the *independent Reviewer*
already decided about each candidate commit — the prerequisite the merge
contract assumes, which the Tech Lead's own data-source contract forbids it from
fetching for itself.

Naming the capability rather than the storage is what keeps the downloader from
knowing that the answer happens to live in attempt sidecars, and what lets the
policy that decides "is this evidence complete" stay in the control layer where
every other admission rule lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..domain.tech_lead_candidate import TechLeadCandidateEvidence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.tech_lead_manifest import PRToReview
    from .repository_host import RepositoryHost


class TechLeadCandidateEvidenceSource(Protocol):
    """What is durably known about ONE audited candidate's review."""

    def evidence_for(
        self, entry: "PRToReview", *, repository_host: "RepositoryHost"
    ) -> TechLeadCandidateEvidence:
        """The reviewer/publication evidence for ``entry``'s exact commit.

        Never raises for missing or damaged evidence: an implementation reports
        it as a :attr:`~..domain.tech_lead_candidate.TechLeadCandidateEvidence.gap`
        so the batch review still runs and that candidate simply cannot be
        passed. Incomplete evidence is not permissive evidence.
        """
        ...


class NoTechLeadCandidateEvidence:
    """The explicitly-named "this composition staged no evidence source".

    A null object rather than ``None``, for the reason
    :data:`~.provider_readiness.NO_PROVIDER_READINESS_PROBE` is one: a
    composition without an evidence source must SAY so in the staged file, so
    every candidate reads as unproven. Silence would be indistinguishable from
    a real refusal, and a later reader could not tell a wiring omission from
    one.
    """

    def evidence_for(
        self, entry: "PRToReview", *, repository_host: "RepositoryHost"
    ) -> TechLeadCandidateEvidence:
        return TechLeadCandidateEvidence(
            candidate=entry.candidate(),
            gap=(
                "no exact-candidate review evidence source is wired into this"
                " orchestrator, so nothing can prove an independent reviewer"
                " approved this commit"
            ),
        )


NO_TECH_LEAD_CANDIDATE_EVIDENCE: TechLeadCandidateEvidenceSource = (
    NoTechLeadCandidateEvidence()
)


__all__ = [
    "NO_TECH_LEAD_CANDIDATE_EVIDENCE",
    "NoTechLeadCandidateEvidence",
    "TechLeadCandidateEvidenceSource",
]
