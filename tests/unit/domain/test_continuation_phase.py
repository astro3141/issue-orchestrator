"""Phase derivation for one exact candidate (#149).

The predicate itself, isolated from stores and ownership. Two properties are
what the tests are for: the ORDER the rules are applied in is policy (a settled
candidate can never be re-derived as live), and every phase declares whether it
holds the issue, so a new phase cannot be added without answering that.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.continuation_descriptor import ContinuationDescriptor
from issue_orchestrator.domain.continuation_phase import (
    ContinuationFacts,
    ContinuationPhase,
    derive_continuation_phase,
)
from issue_orchestrator.domain.continuation_settlement import (
    ContinuationSettlement,
    ContinuationSettlementKind,
)
from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.review_verdict_binding import ReviewVerdictOutcome


def _descriptor(*actions: RequestedAction) -> ContinuationDescriptor:
    return ContinuationDescriptor(
        requested_actions=tuple(actions),
        implementation="",
        problems="",
        suite="publish_gate",
        command="make validate-pr-raw",
        profile="default",
    )


_UNSET = object()
"""Distinguishes "the test did not choose" from "the test chose ``None``".

``None`` is the load-bearing value in half these cases, so a default of ``None``
would make the absent-descriptor tests silently assert something else.
"""


def _facts(
    *,
    descriptor: ContinuationDescriptor | None | object = _UNSET,
    has_publication_evaluation: bool = True,
    latest_publication_passed: bool = False,
    revalidation_allowance_available: bool = True,
    review_verdict: ReviewVerdictOutcome | None = None,
    board_shows_pr_pending: bool = False,
    settlement: ContinuationSettlement | None = None,
    engine_is_executing: bool = False,
) -> ContinuationFacts:
    return ContinuationFacts(
        descriptor=(
            _descriptor(RequestedAction.CREATE_PR)
            if descriptor is _UNSET
            else descriptor  # type: ignore[arg-type]
        ),
        has_publication_evaluation=has_publication_evaluation,
        latest_publication_passed=latest_publication_passed,
        revalidation_allowance_available=revalidation_allowance_available,
        review_verdict=review_verdict,
        board_shows_pr_pending=board_shows_pr_pending,
        settlement=settlement,
        engine_is_executing=engine_is_executing,
    )


def _settlement(
    kind: ContinuationSettlementKind, pr_url: str | None = None
) -> ContinuationSettlement:
    return ContinuationSettlement(
        kind=kind, settled_at="2026-08-19T02:00:00Z", pr_url=pr_url
    )


class TestEveryPhaseDeclaresLiveness:
    def test_the_live_phases_are_exactly_those_with_work_outstanding(self) -> None:
        live = {phase for phase in ContinuationPhase if phase.live}

        assert live == {
            ContinuationPhase.EXECUTING,
            ContinuationPhase.RETRY_PENDING,
            ContinuationPhase.PASS_PENDING_REVIEW,
            ContinuationPhase.APPROVED_PENDING_PR,
        }


class TestNoRecordedIntent:
    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"latest_publication_passed": True},
            {"review_verdict": ReviewVerdictOutcome.APPROVED},
            {"board_shows_pr_pending": True},
            {"revalidation_allowance_available": False},
        ],
    )
    def test_absence_outranks_every_other_fact(self, overrides: dict) -> None:
        phase = derive_continuation_phase(_facts(descriptor=None, **overrides))

        assert phase is ContinuationPhase.NO_RECORDED_INTENT
        assert phase.live is False


class TestRetryAndExhaustion:
    def test_non_pass_with_the_allowance_unspent_is_retry_pending(self) -> None:
        assert (
            derive_continuation_phase(_facts()) is ContinuationPhase.RETRY_PENDING
        )

    def test_non_pass_with_the_allowance_spent_is_exhausted(self) -> None:
        phase = derive_continuation_phase(
            _facts(revalidation_allowance_available=False)
        )

        assert phase is ContinuationPhase.EXHAUSTED
        assert phase.live is False

    def test_a_candidate_the_contract_never_reported_on_is_not_live(self) -> None:
        phase = derive_continuation_phase(_facts(has_publication_evaluation=False))

        assert phase is ContinuationPhase.NOT_EVALUATED
        assert phase.live is False


class TestPassKeepsTheOperationLive:
    def test_pass_without_a_review_verdict_is_pass_pending_review(self) -> None:
        phase = derive_continuation_phase(_facts(latest_publication_passed=True))

        assert phase is ContinuationPhase.PASS_PENDING_REVIEW
        assert phase.live is True

    def test_pass_does_not_read_as_retry_pending(self) -> None:
        """The early-release reading #148's correction removed."""
        assert (
            derive_continuation_phase(_facts(latest_publication_passed=True))
            is not ContinuationPhase.RETRY_PENDING
        )


class TestReviewOutcomes:
    def test_changes_requested_exits_to_rework(self) -> None:
        phase = derive_continuation_phase(
            _facts(
                latest_publication_passed=True,
                review_verdict=ReviewVerdictOutcome.CHANGES_REQUESTED,
            )
        )

        assert phase is ContinuationPhase.EXIT_TO_REWORK
        assert phase.live is False

    def test_approved_with_create_pr_waits_for_the_pull_request(self) -> None:
        phase = derive_continuation_phase(
            _facts(
                latest_publication_passed=True,
                review_verdict=ReviewVerdictOutcome.APPROVED,
            )
        )

        assert phase is ContinuationPhase.APPROVED_PENDING_PR
        assert phase.live is True

    def test_approved_without_create_pr_settles(self) -> None:
        phase = derive_continuation_phase(
            _facts(
                descriptor=_descriptor(RequestedAction.PUSH_BRANCH),
                latest_publication_passed=True,
                review_verdict=ReviewVerdictOutcome.APPROVED,
            )
        )

        assert phase is ContinuationPhase.SETTLED_NO_PR
        assert phase.live is False


class TestSettlementOutranksContinuation:
    """Order is policy: a settled candidate is never re-derived as live."""

    def test_pr_pending_settles_even_before_a_review_verdict_exists(self) -> None:
        phase = derive_continuation_phase(
            _facts(latest_publication_passed=True, board_shows_pr_pending=True)
        )

        assert phase is ContinuationPhase.SETTLED_PR

    def test_pr_pending_outranks_an_approval_still_waiting_for_a_pr(self) -> None:
        phase = derive_continuation_phase(
            _facts(
                latest_publication_passed=True,
                review_verdict=ReviewVerdictOutcome.APPROVED,
                board_shows_pr_pending=True,
            )
        )

        assert phase is ContinuationPhase.SETTLED_PR

    def test_a_review_verdict_outranks_a_retryable_non_pass(self) -> None:
        """A reviewed candidate is past revalidation, whatever the gate last said."""
        phase = derive_continuation_phase(
            _facts(review_verdict=ReviewVerdictOutcome.CHANGES_REQUESTED)
        )

        assert phase is ContinuationPhase.EXIT_TO_REWORK


class TestTheRunsOwnSettlementTerminates:
    """The board never learns about a continuation's PR, so the run must say so."""

    def test_a_recorded_pull_request_settles_an_approval_the_board_is_silent_on(
        self,
    ) -> None:
        """The F1 loop: APPROVED + CREATE_PR + no ``pr-pending`` ran forever."""
        phase = derive_continuation_phase(
            _facts(
                latest_publication_passed=True,
                review_verdict=ReviewVerdictOutcome.APPROVED,
                board_shows_pr_pending=False,
                settlement=_settlement(
                    ContinuationSettlementKind.PULL_REQUEST_OPENED,
                    pr_url="https://example.test/pr/1",
                ),
            )
        )

        assert phase is ContinuationPhase.SETTLED_PR
        assert phase.live is False

    def test_a_run_that_owed_no_pull_request_settles_without_one(self) -> None:
        phase = derive_continuation_phase(
            _facts(
                descriptor=_descriptor(RequestedAction.PUSH_BRANCH),
                latest_publication_passed=True,
                settlement=_settlement(
                    ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED
                ),
            )
        )

        assert phase is ContinuationPhase.SETTLED_NO_PR
        assert phase.live is False

    def test_settlement_outranks_a_still_retryable_non_pass(self) -> None:
        phase = derive_continuation_phase(
            _facts(
                revalidation_allowance_available=True,
                settlement=_settlement(
                    ContinuationSettlementKind.PULL_REQUEST_OPENED,
                    pr_url="https://example.test/pr/1",
                ),
            )
        )

        assert phase is ContinuationPhase.SETTLED_PR

    def test_an_unsettled_approval_that_asked_for_a_pr_is_still_live(self) -> None:
        """Absence of settlement is what keeps a failed run retryable."""
        phase = derive_continuation_phase(
            _facts(
                latest_publication_passed=True,
                review_verdict=ReviewVerdictOutcome.APPROVED,
                settlement=None,
            )
        )

        assert phase is ContinuationPhase.APPROVED_PENDING_PR
        assert phase.live is True


class TestExecutionOutranksEveryDurableFact:
    """#139 spends the allowance BEFORE the gate runs, so mid-run facts lie."""

    def test_a_reserved_but_unreported_revalidation_is_not_exhausted(self) -> None:
        """The F2 window: allowance spent, latest evaluation still the failure."""
        phase = derive_continuation_phase(
            _facts(revalidation_allowance_available=False, engine_is_executing=True)
        )

        assert phase is ContinuationPhase.EXECUTING
        assert phase.live is True

    def test_the_same_facts_without_a_run_in_flight_are_exhausted(self) -> None:
        """A crash erases the claim, so #139's fail-closed direction survives."""
        phase = derive_continuation_phase(
            _facts(revalidation_allowance_available=False, engine_is_executing=False)
        )

        assert phase is ContinuationPhase.EXHAUSTED

    @pytest.mark.parametrize(
        "overrides",
        [
            {"descriptor": None},
            {"board_shows_pr_pending": True},
            {"review_verdict": ReviewVerdictOutcome.CHANGES_REQUESTED},
            {
                "settlement": ContinuationSettlement(
                    kind=ContinuationSettlementKind.PULL_REQUEST_OPENED,
                    settled_at="2026-08-19T02:00:00Z",
                    pr_url="https://example.test/pr/1",
                )
            },
        ],
    )
    def test_a_run_in_flight_outranks_every_terminal_fact(
        self, overrides: dict
    ) -> None:
        """Terminal facts are being WRITTEN by the run; it still holds the issue."""
        phase = derive_continuation_phase(
            _facts(engine_is_executing=True, **overrides)
        )

        assert phase is ContinuationPhase.EXECUTING
