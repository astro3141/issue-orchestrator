"""The terminal outcome of one continuation run (#149).

The settlement is the only fact that stops a control operation, so these tests
hold it to the two properties that matter: it cannot claim a pull request it has
no evidence of, and damage to a stored one never reads as "this continuation
never settled" — which would put a finished run, pull request and all, back on
the runner.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.continuation_settlement import (
    CONTINUATION_SETTLEMENT_SCHEMA_VERSION,
    ContinuationSettlement,
    ContinuationSettlementKind,
)

PR_URL = "https://example.test/owner/repo/pull/7"
SETTLED_AT = "2026-08-19T02:00:00Z"


def _opened(pr_url: str | None = PR_URL) -> ContinuationSettlement:
    return ContinuationSettlement(
        kind=ContinuationSettlementKind.PULL_REQUEST_OPENED,
        settled_at=SETTLED_AT,
        pr_url=pr_url,
    )


class TestEvidenceMatchesTheClaim:
    def test_a_pull_request_settlement_names_the_pull_request(self) -> None:
        assert _opened().opened_pull_request is True
        assert _opened().pr_url == PR_URL

    @pytest.mark.parametrize("pr_url", [None, "", "   "])
    def test_claiming_a_pull_request_without_one_is_refused(
        self, pr_url: str | None
    ) -> None:
        with pytest.raises(ValueError, match="pr_url"):
            _opened(pr_url)

    def test_settling_without_a_pull_request_may_not_carry_one(self) -> None:
        """The two kinds are answers to different questions, not a flag."""
        with pytest.raises(ValueError, match="pr_url"):
            ContinuationSettlement(
                kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
                settled_at=SETTLED_AT,
                pr_url=PR_URL,
            )

    def test_nothing_further_requested_is_not_a_pull_request(self) -> None:
        settlement = ContinuationSettlement(
            kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
            settled_at=SETTLED_AT,
        )

        assert settlement.opened_pull_request is False

    @pytest.mark.parametrize("settled_at", ["", "   "])
    def test_a_settlement_without_a_time_is_refused(self, settled_at: str) -> None:
        with pytest.raises(ValueError, match="settled_at"):
            ContinuationSettlement(
                kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
                settled_at=settled_at,
            )


class TestRoundTrip:
    @pytest.mark.parametrize(
        "settlement",
        [
            ContinuationSettlement(
                kind=ContinuationSettlementKind.PULL_REQUEST_OPENED,
                settled_at=SETTLED_AT,
                pr_url=PR_URL,
            ),
            ContinuationSettlement(
                kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
                settled_at=SETTLED_AT,
            ),
        ],
    )
    def test_every_settlement_survives_storage(
        self, settlement: ContinuationSettlement
    ) -> None:
        assert (
            ContinuationSettlement.from_payload(settlement.to_payload()) == settlement
        )


class TestDamageIsNeverAbsence:
    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "pull_request_opened", "settled_at": SETTLED_AT},
            {
                "schema_version": CONTINUATION_SETTLEMENT_SCHEMA_VERSION,
                "kind": "abandoned",
                "settled_at": SETTLED_AT,
            },
            {
                "schema_version": CONTINUATION_SETTLEMENT_SCHEMA_VERSION,
                "kind": "pull_request_opened",
                "settled_at": SETTLED_AT,
            },
            {
                "schema_version": CONTINUATION_SETTLEMENT_SCHEMA_VERSION,
                "kind": "nothing_further_requested",
                "settled_at": 17,
            },
            {
                "schema_version": CONTINUATION_SETTLEMENT_SCHEMA_VERSION + 1,
                "kind": "nothing_further_requested",
                "settled_at": SETTLED_AT,
            },
        ],
    )
    def test_a_settlement_it_cannot_read_exactly_raises(
        self, payload: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError):
            ContinuationSettlement.from_payload(payload)
