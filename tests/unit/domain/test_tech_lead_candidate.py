"""The exact-candidate value objects a Tech Lead disposition binds to (#345).

The identity, the verdict, and the standing are separate types on purpose, and
each of these tests fixes one thing the type must make impossible: a candidate
that cannot name its commit, a verdict that does not say which commit it judged,
a disposition with nothing actionable behind it, or a standing whose author
forgot to say whether it permits authority.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.tech_lead_artifacts import TechLeadDecision
from issue_orchestrator.domain.tech_lead_candidate import (
    CandidateStanding,
    TechLeadCandidate,
    TechLeadCandidateDisposition,
    TechLeadCandidateVerdict,
)

CANDIDATE_A = "a" * 40
CANDIDATE_B = "b" * 40


class TestCandidateIdentity:
    def test_a_bound_candidate_covers_its_own_commit(self) -> None:
        candidate = TechLeadCandidate(101, CANDIDATE_A.upper())

        assert candidate.head_sha == CANDIDATE_A
        assert candidate.covers(CANDIDATE_A) is True
        assert candidate.covers(CANDIDATE_B) is False

    def test_an_unobserved_head_is_carried_but_covers_nothing(self) -> None:
        candidate = TechLeadCandidate(101)

        assert candidate.is_bound is False
        assert candidate.covers(CANDIDATE_A) is False
        assert candidate.short_sha == "unknown"

    def test_an_abbreviated_commit_is_refused_rather_than_expanded(self) -> None:
        with pytest.raises(ValueError):
            TechLeadCandidate(101, "a1b2c3d")

    def test_an_unusable_observation_does_not_raise_at_the_covers_seam(self) -> None:
        """The caller is asking "may this still apply", and the answer is no."""
        candidate = TechLeadCandidate(101, CANDIDATE_A)

        assert candidate.covers(None) is False
        assert candidate.covers("not-a-sha") is False

    def test_a_candidate_round_trips_through_its_payload(self) -> None:
        candidate = TechLeadCandidate(101, CANDIDATE_A)

        assert TechLeadCandidate.from_payload(candidate.to_payload()) == candidate

    def test_a_malformed_stored_candidate_raises(self) -> None:
        with pytest.raises(ValueError):
            TechLeadCandidate.from_payload({"pr_number": "101", "head_sha": CANDIDATE_A})


class TestCandidateStanding:
    def test_only_a_current_candidate_permits_authority(self) -> None:
        assert CandidateStanding.CURRENT.permits_authority is True
        assert CandidateStanding.MOVED.permits_authority is False
        assert CandidateStanding.UNREADABLE.permits_authority is False
        assert CandidateStanding.UNBOUND.permits_authority is False


class TestCandidateVerdict:
    def test_a_verdict_names_the_commit_it_judged(self) -> None:
        verdict = TechLeadCandidateVerdict.from_mapping(
            {
                "pr_number": 101,
                "candidate_sha": CANDIDATE_A,
                "disposition": "pass",
                "rationale": "Conforms.",
            },
            index=1,
        )

        assert verdict.candidate == TechLeadCandidate(101, CANDIDATE_A)
        assert verdict.disposition is TechLeadCandidateDisposition.PASS

    def test_a_verdict_without_its_commit_is_a_contract_violation(self) -> None:
        with pytest.raises(ValueError, match="candidate_sha"):
            TechLeadCandidateVerdict.from_mapping(
                {"pr_number": 101, "disposition": "pass", "rationale": "Conforms."},
                index=1,
            )

    def test_an_unknown_disposition_is_refused_not_downgraded(self) -> None:
        with pytest.raises(ValueError, match="unknown disposition"):
            TechLeadCandidateVerdict.from_mapping(
                {
                    "pr_number": 101,
                    "candidate_sha": CANDIDATE_A,
                    "disposition": "approve",
                    "rationale": "Conforms.",
                },
                index=1,
            )

    def test_every_disposition_must_state_its_reason(self) -> None:
        for disposition in ("pass", "rework", "human_a"):
            with pytest.raises(ValueError, match="non-empty rationale"):
                TechLeadCandidateVerdict.from_mapping(
                    {
                        "pr_number": 101,
                        "candidate_sha": CANDIDATE_A,
                        "disposition": disposition,
                        "rationale": "   ",
                    },
                    index=1,
                )

    def test_only_pass_projects_the_merge_facing_label(self) -> None:
        assert TechLeadCandidateDisposition.PASS.projects_reviewed_label is True
        assert TechLeadCandidateDisposition.REWORK.projects_reviewed_label is False
        assert TechLeadCandidateDisposition.HUMAN_A.projects_reviewed_label is False


class TestDecisionCarriesVerdicts:
    def _payload(self, verdicts: list[dict[str, object]]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "summary": "Batch contract review.",
            "findings": [
                {
                    "id": "T1",
                    "title": "Missing owner",
                    "classification": "systemic",
                    "evidence": ["pr-101-diff.txt"],
                }
            ],
            "proposed_actions": [],
            "candidate_verdicts": verdicts,
        }

    def test_the_decision_exposes_one_verdict_per_candidate(self) -> None:
        decision = TechLeadDecision.from_agent_payload(
            self._payload(
                [
                    {
                        "pr_number": 101,
                        "candidate_sha": CANDIDATE_A,
                        "disposition": "pass",
                        "rationale": "Conforms.",
                        "finding_ids": ["T1"],
                    }
                ]
            )
        )

        assert decision.verdict_for(101) is not None
        assert decision.verdict_for(102) is None

    def test_two_verdicts_for_one_candidate_are_refused(self) -> None:
        with pytest.raises(ValueError, match="multiple candidate verdicts"):
            TechLeadDecision.from_agent_payload(
                self._payload(
                    [
                        {
                            "pr_number": 101,
                            "candidate_sha": CANDIDATE_A,
                            "disposition": "pass",
                            "rationale": "Conforms.",
                        },
                        {
                            "pr_number": 101,
                            "candidate_sha": CANDIDATE_A,
                            "disposition": "rework",
                            "rationale": "Actually not.",
                        },
                    ]
                )
            )

    def test_a_verdict_citing_an_unknown_finding_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown finding ids"):
            TechLeadDecision.from_agent_payload(
                self._payload(
                    [
                        {
                            "pr_number": 101,
                            "candidate_sha": CANDIDATE_A,
                            "disposition": "rework",
                            "rationale": "See T9.",
                            "finding_ids": ["T9"],
                        }
                    ]
                )
            )

    def test_a_decision_with_no_verdicts_is_still_valid(self) -> None:
        """An audit that judged no candidate projects nothing, and that is fine."""
        decision = TechLeadDecision.from_agent_payload(self._payload([]))

        assert decision.candidate_verdicts == ()

    def test_the_verdicts_survive_a_round_trip(self) -> None:
        decision = TechLeadDecision.from_agent_payload(
            self._payload(
                [
                    {
                        "pr_number": 101,
                        "candidate_sha": CANDIDATE_A,
                        "disposition": "human_a",
                        "rationale": "Whose call?",
                    }
                ]
            )
        )

        reloaded = TechLeadDecision.from_agent_payload(decision.to_dict())

        assert reloaded.candidate_verdicts == decision.candidate_verdicts
