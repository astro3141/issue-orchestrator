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
    )


class TestEveryPhaseDeclaresLiveness:
    def test_the_live_phases_are_exactly_the_three_with_work_outstanding(self) -> None:
        live = {phase for phase in ContinuationPhase if phase.live}

        assert live == {
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
