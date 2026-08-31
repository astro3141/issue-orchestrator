"""Which terminal a reviewer round reaches (#180).

The policy is an ORDER, and the order is only visible when the conditions
overlap: a hand-off round that also reports no progress, an approval from a
caller that owns no coder, a budget that is one round from being spent. Driving
those through a full exchange takes a spawned pair per case and pins the
ordering only incidentally, so they are asserted here, directly, against the
owner that carries the counter across rounds.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.review_artifacts import ReviewDecision, ReviewScope
from issue_orchestrator.domain.review_exchange import ReviewExchangeResponse
from issue_orchestrator.domain.review_exchange_rework import ReviewExchangeRework
from issue_orchestrator.domain.review_exchange_summary import (
    ReviewExchangeReason,
    ReviewExchangeStatus,
)
from issue_orchestrator.execution.review_exchange_terminals import (
    ReviewerRoundTerminals,
)

APPROVED = ReviewExchangeStatus.OK, ReviewExchangeReason.REVIEWER_OK
HANDED_OFF = (
    ReviewExchangeStatus.STOPPED,
    ReviewExchangeReason.REVIEWER_REQUESTED_CHANGES,
)
NO_PROGRESS = (
    ReviewExchangeStatus.STOPPED,
    ReviewExchangeReason.REVIEWER_REPORTS_NO_PROGRESS,
)
SCOPE_CONFLICT = (
    ReviewExchangeStatus.STOPPED,
    ReviewExchangeReason.REVIEWER_SCOPE_CONFLICT,
)


def _decision(
    *,
    verdict: str = "changes_requested",
    scope: ReviewScope = "in_contract",
) -> ReviewDecision:
    return ReviewDecision(
        verdict=verdict,  # type: ignore[arg-type]
        risk="low",
        scope=scope,
    )


def _reviewer(
    response_type: str, *, getting_closer: bool | None = None
) -> ReviewExchangeResponse:
    return ReviewExchangeResponse(
        response_type=response_type,
        response_text="See review report.",
        getting_closer=getting_closer,
    )


def _terminals(
    rework: ReviewExchangeRework, *, max_no_progress: int = 2
) -> ReviewerRoundTerminals:
    return ReviewerRoundTerminals(rework=rework, max_no_progress=max_no_progress)


class TestApprovalOutranksEverything:
    """The one answer no other policy may override, in either mode."""

    @pytest.mark.parametrize("rework", list(ReviewExchangeRework))
    def test_an_approval_ends_the_exchange_approved(
        self, rework: ReviewExchangeRework
    ) -> None:
        assert _terminals(rework).for_round(_reviewer("ok"), _decision()) == APPROVED

    def test_an_approval_ends_it_even_after_the_budget_is_spent(self) -> None:
        """A spent budget is not a verdict: it only decides non-approvals."""
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE, max_no_progress=1)
        terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())

        assert terminals.for_round(_reviewer("ok"), _decision()) == APPROVED


class TestTheHandOffPrecedesTheNoProgressBudget:
    """A caller with no coder has nothing to measure progress BETWEEN."""

    def test_the_first_non_approving_round_hands_off(self) -> None:
        terminals = _terminals(ReviewExchangeRework.HAND_OFF, max_no_progress=3)

        assert (
            terminals.for_round(_reviewer("changes_requested", getting_closer=True), _decision())
            == HANDED_OFF
        )

    def test_a_no_progress_round_still_hands_off_rather_than_reporting_it(
        self,
    ) -> None:
        """Both conditions hold; the reason has to be the one that is true.

        ``REVIEWER_REPORTS_NO_PROGRESS`` would be a claim about successive
        coder turns in an exchange that ran none, and it is not the reason the
        continuation's rejection has to carry back.
        """
        terminals = _terminals(ReviewExchangeRework.HAND_OFF, max_no_progress=1)

        assert (
            terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())
            == HANDED_OFF
        )

    def test_a_disagreement_hands_off_too(self) -> None:
        """Any non-approval, not just ``changes_requested``."""
        terminals = _terminals(ReviewExchangeRework.HAND_OFF)

        assert terminals.for_round(_reviewer("disagree"), _decision()) == HANDED_OFF


class TestTheNoProgressBudgetInAnExchangeThatOwnsACoder:
    def test_a_round_under_budget_continues_into_a_coder_turn(self) -> None:
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE, max_no_progress=2)

        assert (
            terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())
            is None
        )

    def test_the_budget_stops_the_exchange_when_it_is_reached(self) -> None:
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE, max_no_progress=2)
        terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())

        assert (
            terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())
            == NO_PROGRESS
        )

    def test_a_getting_closer_round_resets_the_count(self) -> None:
        """The budget measures CONSECUTIVE stalls, not stalls in total.

        Without the reset a long exchange that recovered would still be halted
        by rounds it had already worked past.
        """
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE, max_no_progress=2)
        terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())
        terminals.for_round(_reviewer("changes_requested", getting_closer=True), _decision())

        assert (
            terminals.for_round(_reviewer("changes_requested", getting_closer=False), _decision())
            is None
        )

    def test_an_unstated_getting_closer_counts_as_progress(self) -> None:
        """Only an explicit ``False`` is a stall; ``None`` is no claim at all."""
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE, max_no_progress=1)

        assert (
            terminals.for_round(_reviewer("changes_requested", getting_closer=None), _decision())
            is None
        )

    def test_a_zero_budget_disables_the_bound(self) -> None:
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE, max_no_progress=0)

        for _ in range(5):
            assert (
                terminals.for_round(
                    _reviewer("changes_requested", getting_closer=False),
                    _decision(),
                )
                is None
            )


class TestAnOutOfContractFindingIsNotOrdinaryRework:
    """#399 F4/F5/F6: mutation authority is decided before rework is."""

    @pytest.mark.parametrize("rework", list(ReviewExchangeRework))
    def test_a_scope_conflict_ends_the_exchange_in_either_mode(
        self, rework: ReviewExchangeRework
    ) -> None:
        # Not `reviewer_requested_changes`: that terminal claims a rework
        # round could resolve it, and the admitted contract says none can.
        assert (
            _terminals(rework).for_round(
                _reviewer("changes_requested", getting_closer=True),
                _decision(scope="out_of_contract"),
            )
            == SCOPE_CONFLICT
        )

    def test_a_scope_conflict_never_reaches_the_coder(self) -> None:
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE)

        # An in-exchange lane with budget to spare would ordinarily continue
        # into a coder turn. It must not: the coder turn is where #398's
        # candidate acquired its second file.
        assert (
            terminals.for_round(
                _reviewer("changes_requested", getting_closer=True),
                _decision(scope="out_of_contract"),
            )
            is not None
        )

    def test_an_ordinary_finding_still_reworks_normally(self) -> None:
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE)

        assert (
            terminals.for_round(
                _reviewer("changes_requested", getting_closer=True),
                _decision(),
            )
            is None
        )

    def test_a_disagreement_is_not_a_scope_conflict(self) -> None:
        # F5: the reviewer being WRONG stays the disagreement path. Only an
        # explicit marker routes to the scope terminal.
        terminals = _terminals(ReviewExchangeRework.IN_EXCHANGE)

        assert (
            terminals.for_round(_reviewer("disagree"), _decision(verdict="disagree"))
            is None
        )

    def test_an_approval_still_outranks_the_marker(self) -> None:
        # The decision type refuses approved+out_of_contract outright, but the
        # transport field and the decision are separate values, so the order
        # is asserted rather than assumed.
        assert (
            _terminals(ReviewExchangeRework.IN_EXCHANGE).for_round(
                _reviewer("ok"),
                _decision(scope="out_of_contract"),
            )
            == APPROVED
        )
