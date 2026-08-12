"""Durable records of one review exchange.

Focused on the verdict binding: what gets written, what deliberately does not,
and what a fresh process can reconstruct from storage alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.review_verdict_binding import (
    BoundReviewVerdict,
    ReviewVerdictOutcome,
)
from issue_orchestrator.execution.review_exchange_records import (
    bind_review_verdict,
    load_review_verdict,
    review_verdict_path,
)


SHA_A = "a" * 40
SHA_B = "b" * 40


class TestBindReviewVerdict:
    def test_binding_is_written_as_one_artifact(self, tmp_path: Path) -> None:
        binding = bind_review_verdict(
            exchange_dir=tmp_path,
            verdict=ReviewVerdictOutcome.APPROVED,
            presented_head_sha=SHA_A,
            completed_rounds=1,
        )

        assert binding is not None
        payload = json.loads(review_verdict_path(tmp_path).read_text())
        assert payload["verdict"] == "approved"
        assert payload["reviewed_sha"] == SHA_A
        assert payload["completed_rounds"] == 1

    def test_binding_survives_restart(self, tmp_path: Path) -> None:
        """Nothing in memory is required to read the binding back."""
        bind_review_verdict(
            exchange_dir=tmp_path,
            verdict=ReviewVerdictOutcome.APPROVED,
            presented_head_sha=SHA_A,
            completed_rounds=2,
        )

        reloaded = load_review_verdict(tmp_path)

        assert reloaded == BoundReviewVerdict(
            verdict=ReviewVerdictOutcome.APPROVED,
            reviewed_sha=SHA_A,
            decided_at=reloaded.decided_at if reloaded else "",
            completed_rounds=2,
        )
        assert reloaded is not None
        assert reloaded.approves(SHA_A) is True
        assert reloaded.approves(SHA_B) is False

    def test_changes_requested_is_bound_too(self, tmp_path: Path) -> None:
        binding = bind_review_verdict(
            exchange_dir=tmp_path,
            verdict=ReviewVerdictOutcome.CHANGES_REQUESTED,
            presented_head_sha=SHA_B,
            completed_rounds=3,
        )

        assert binding is not None
        assert binding.verdict is ReviewVerdictOutcome.CHANGES_REQUESTED
        assert binding.approves(SHA_B) is False

    @pytest.mark.parametrize("observed", [None, "", "abc123", "not-a-sha", "HEAD"])
    def test_unusable_head_records_nothing_rather_than_guessing(
        self,
        tmp_path: Path,
        observed: str | None,
    ) -> None:
        """No observed commit means no binding — never a fabricated one.

        An absent binding is a verdict no later gate can admit, which is the
        safe direction; a fabricated SHA would be the unsafe one. Returning
        rather than raising is the other half: an unusable observation must
        not turn a completed review into an exception.
        """
        assert (
            bind_review_verdict(
                exchange_dir=tmp_path,
                verdict=ReviewVerdictOutcome.APPROVED,
                presented_head_sha=observed,
                completed_rounds=1,
            )
            is None
        )
        assert not review_verdict_path(tmp_path).exists()


class TestLoadReviewVerdict:
    def test_absent_binding_reads_as_none(self, tmp_path: Path) -> None:
        assert load_review_verdict(tmp_path) is None

    def test_corrupt_binding_fails_loudly(self, tmp_path: Path) -> None:
        """A corrupt authority artifact is not an absent one."""
        review_verdict_path(tmp_path).write_text("{not json", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            load_review_verdict(tmp_path)

    def test_half_a_binding_fails_loudly(self, tmp_path: Path) -> None:
        review_verdict_path(tmp_path).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verdict": "approved",
                    "decided_at": "2026-08-12T00:00:00+00:00",
                    "completed_rounds": 1,
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="reviewed_sha"):
            load_review_verdict(tmp_path)
