"""The per-candidate leaf-contract record (#345).

Two states and no third: a RESOLVED contract names its executable issue and
carries that issue's staged body first, an UNRESOLVED one carries only the gap
that says what could not be read. These fix what the type makes impossible — a
half-filled entry a reader could mistake for a thin contract rather than an
absent one.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.canonical_context import (
    CanonicalSource,
    CanonicalSourceKind,
)
from issue_orchestrator.domain.tech_lead_candidate import TechLeadCandidate
from issue_orchestrator.domain.tech_lead_candidate_contract import (
    TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME,
    TechLeadCandidateContract,
    TechLeadCandidateContractSet,
    candidate_sources_dirname,
)

CANDIDATE = TechLeadCandidate(346, "a" * 40)
LEAF = 345
SPEC = 335


def _source(
    number: int, kind: CanonicalSourceKind, *, required: bool = True
) -> CanonicalSource:
    return CanonicalSource(
        kind=kind,
        issue_number=number,
        required=required,
        fetched_at="2026-08-29T06:00:00Z",
        staged=True,
        updated_at="2026-08-28T09:00:00Z",
        body_sha256="b" * 64,
    )


def _resolved(*sources: CanonicalSource) -> TechLeadCandidateContract:
    return TechLeadCandidateContract(
        candidate=CANDIDATE,
        issue_number=LEAF,
        sources_dir=(
            f"{TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME}/"
            f"{candidate_sources_dirname(CANDIDATE)}"
        ),
        sources=sources
        or (
            _source(LEAF, CanonicalSourceKind.SUBJECT),
            _source(SPEC, CanonicalSourceKind.GOVERNING),
        ),
    )


class TestResolvedContract:
    def test_it_names_its_leaf_and_the_sources_that_leaf_declared(self) -> None:
        contract = _resolved()

        assert contract.establishes_leaf_contract is True
        assert contract.issue_number == LEAF
        assert [source.issue_number for source in contract.governing_sources] == [SPEC]

    def test_the_payload_carries_what_a_reader_verifies_against(self) -> None:
        payload = _resolved().to_payload()

        assert payload["pr_number"] == CANDIDATE.pr_number
        assert payload["candidate_sha"] == CANDIDATE.head_sha
        assert payload["issue_number"] == LEAF
        assert payload["gap"] == ""
        assert payload["sources"][0]["body_sha256"] == "b" * 64

    def test_the_bundle_directory_is_bound_to_the_audited_commit(self) -> None:
        assert candidate_sources_dirname(CANDIDATE) == "pr-346-aaaaaaaaaaaa"

    def test_a_resolved_contract_must_stage_its_own_leaf_first(self) -> None:
        with pytest.raises(ValueError, match="as its first source"):
            _resolved(
                _source(SPEC, CanonicalSourceKind.GOVERNING),
                _source(LEAF, CanonicalSourceKind.SUBJECT),
            )

    def test_a_resolved_contract_must_name_its_issue(self) -> None:
        with pytest.raises(ValueError, match="must name the executable issue"):
            TechLeadCandidateContract(
                candidate=CANDIDATE,
                sources_dir="x",
                sources=(_source(LEAF, CanonicalSourceKind.SUBJECT),),
            )

    def test_a_resolved_contract_must_say_where_its_bytes_are(self) -> None:
        with pytest.raises(ValueError, match="where its staged sources"):
            TechLeadCandidateContract(
                candidate=CANDIDATE,
                issue_number=LEAF,
                sources=(_source(LEAF, CanonicalSourceKind.SUBJECT),),
            )

    def test_the_same_issue_cannot_be_staged_twice(self) -> None:
        with pytest.raises(ValueError, match="same issue twice"):
            _resolved(
                _source(LEAF, CanonicalSourceKind.SUBJECT),
                _source(LEAF, CanonicalSourceKind.GOVERNING),
            )


class TestUnresolvedContract:
    def test_a_gap_establishes_nothing_and_claims_nothing(self) -> None:
        contract = TechLeadCandidateContract(
            candidate=CANDIDATE, gap="the issue could not be read"
        )

        assert contract.establishes_leaf_contract is False
        assert contract.sources == ()
        assert contract.to_payload()["gap"] == "the issue could not be read"

    def test_a_gap_may_not_also_claim_staged_content(self) -> None:
        """Absent and thin must not read the same."""
        with pytest.raises(ValueError, match="must not claim staged sources"):
            TechLeadCandidateContract(
                candidate=CANDIDATE,
                gap="unreadable",
                sources=(_source(LEAF, CanonicalSourceKind.SUBJECT),),
            )


class TestContractSet:
    def test_only_resolved_candidates_are_reported_contracted(self) -> None:
        other = TechLeadCandidate(347, "c" * 40)
        contracts = TechLeadCandidateContractSet(
            entries=(
                _resolved(),
                TechLeadCandidateContract(candidate=other, gap="unreadable"),
            )
        )

        assert contracts.contracted_pr_numbers() == frozenset({346})

    def test_the_payload_tells_the_run_what_the_file_is_for(self) -> None:
        payload = TechLeadCandidateContractSet(entries=(_resolved(),)).to_payload()

        assert payload["sources_root"] == TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME
        assert len(payload["candidates"]) == 1
        # Provenance, never authority — and the run is told which.
        assert "authority" in payload["guidance"]
        assert "gap" in payload["guidance"]
