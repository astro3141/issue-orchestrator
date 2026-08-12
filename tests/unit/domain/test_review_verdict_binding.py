"""The exact-SHA review verdict binding contract.

Covers the two properties that make the record an authority artifact rather
than a convenience: neither half can go missing, and validity is decided
against the commit that is current *now*, not against history.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.review_verdict_binding import (
    REVIEW_VERDICT_BINDING_FILENAME,
    BoundReviewVerdict,
    ReviewVerdictOutcome,
    normalize_reviewed_sha,
)


SHA_A = "a" * 40
SHA_B = "b" * 40


def _approval(reviewed_sha: str = SHA_A) -> BoundReviewVerdict:
    return BoundReviewVerdict(
        verdict=ReviewVerdictOutcome.APPROVED,
        reviewed_sha=reviewed_sha,
        decided_at="2026-08-12T00:00:00+00:00",
        completed_rounds=1,
    )


class TestBindingIsOneRecord:
    def test_artifact_filename_is_stable(self) -> None:
        assert REVIEW_VERDICT_BINDING_FILENAME == "review-verdict.json"

    def test_verdict_without_sha_does_not_parse(self) -> None:
        payload = _approval().to_payload()
        del payload["reviewed_sha"]
        with pytest.raises(ValueError, match="reviewed_sha"):
            BoundReviewVerdict.from_payload(payload)

    def test_sha_without_verdict_does_not_parse(self) -> None:
        payload = _approval().to_payload()
        del payload["verdict"]
        with pytest.raises(ValueError, match="verdict"):
            BoundReviewVerdict.from_payload(payload)

    def test_unknown_verdict_does_not_parse(self) -> None:
        payload = _approval().to_payload()
        payload["verdict"] = "disagree"
        with pytest.raises(ValueError, match="disagree"):
            BoundReviewVerdict.from_payload(payload)

    def test_round_trips_through_payload(self) -> None:
        binding = _approval()
        assert BoundReviewVerdict.from_payload(binding.to_payload()) == binding

    def test_payload_always_carries_both_halves(self) -> None:
        payload = _approval().to_payload()
        assert payload["verdict"] == "approved"
        assert payload["reviewed_sha"] == SHA_A


class TestShaIsCanonical:
    def test_uppercase_is_normalized(self) -> None:
        assert _approval(SHA_A.upper()).reviewed_sha == SHA_A

    @pytest.mark.parametrize("value", ["", "abc123", "a" * 39, "z" * 40])
    def test_non_canonical_sha_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="40-character hex"):
            normalize_reviewed_sha(value)

    def test_non_string_sha_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            normalize_reviewed_sha(None)

    def test_abbreviated_sha_cannot_be_bound(self) -> None:
        with pytest.raises(ValueError):
            _approval(SHA_A[:12])


class TestValidityPredicate:
    def test_approval_approves_the_commit_it_was_rendered_against(self) -> None:
        assert _approval().approves(SHA_A) is True
        assert _approval().is_stale_for(SHA_A) is False

    def test_approval_does_not_approve_a_moved_head(self) -> None:
        binding = _approval()
        assert binding.approves(SHA_B) is False
        assert binding.is_stale_for(SHA_B) is True

    def test_changes_requested_never_approves_even_its_own_commit(self) -> None:
        binding = BoundReviewVerdict(
            verdict=ReviewVerdictOutcome.CHANGES_REQUESTED,
            reviewed_sha=SHA_A,
            decided_at="2026-08-12T00:00:00+00:00",
            completed_rounds=2,
        )
        assert binding.covers(SHA_A) is True
        assert binding.approves(SHA_A) is False

    def test_predicate_rejects_a_non_canonical_candidate(self) -> None:
        with pytest.raises(ValueError, match="head_sha"):
            _approval().approves("deadbeef")


class TestRecordInvariants:
    def test_decided_at_is_required(self) -> None:
        with pytest.raises(ValueError, match="decided_at"):
            BoundReviewVerdict(
                verdict=ReviewVerdictOutcome.APPROVED,
                reviewed_sha=SHA_A,
                decided_at="   ",
                completed_rounds=1,
            )

    def test_completed_rounds_must_be_a_real_round(self) -> None:
        with pytest.raises(ValueError, match="completed_rounds"):
            BoundReviewVerdict(
                verdict=ReviewVerdictOutcome.APPROVED,
                reviewed_sha=SHA_A,
                decided_at="2026-08-12T00:00:00+00:00",
                completed_rounds=0,
            )
