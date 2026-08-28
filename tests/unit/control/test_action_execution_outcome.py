"""The carrier that says where a completion's review exchange put its artifacts.

``ActionExecutionOutcome`` has two readers, and only one of them reads the
outcome. ``CompletionProcessor.process`` returns an ``early_result`` VERBATIM —
it never reaches ``build_processing_result``, which is the single place the
outcome's own ``review_exchange_run`` is consulted. So an early result that did
not carry the run itself would lose it, which is the same silent drop the type
was introduced to prevent, one exit further out (#180).

These are about :meth:`ActionExecutionOutcome.of`, the one place that
relationship is decided.

The same type also carries what the action-execution phase PROVED about the run
(#337). The publication settler that decides the zero-code lane runs INSIDE that
phase, and both of its answers — whether a code candidate survives, and whether
the posted comment is the whole delivery — are read after it returns, so they
leave on the outcome rather than in a log line.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.completion_types import (
    ActionExecutionOutcome,
    CodeCandidateSettlement,
    CompletionSettlement,
    ProcessingResult,
    ResultOnlyDelivery,
)
from issue_orchestrator.domain.review_exchange_run import ReviewExchangeRunAssets


def _run(root: Path, name: str) -> ReviewExchangeRunAssets:
    return ReviewExchangeRunAssets.from_run_dir(root / ".sessions" / name)


def _outcome(
    *,
    review_exchange_run: ReviewExchangeRunAssets | None,
    early_result: ProcessingResult | None = None,
    deferred: bool = False,
    settlement: CompletionSettlement | None = None,
) -> ActionExecutionOutcome:
    return ActionExecutionOutcome.of(
        branch="issue-1",
        pr_url=None,
        review_exchange_completed=True,
        review_exchange_run=review_exchange_run,
        settlement=settlement or CompletionSettlement.unsettled(),
        deferred=deferred,
        early_result=early_result,
    )


class TestTheEarlyResultNamesTheSameRun:
    def test_an_early_result_that_names_no_run_takes_the_outcomes(
        self, tmp_path: Path
    ) -> None:
        """The drop this type exists to prevent, on the exit that bypasses it.

        A planned action that fails, or a publish gate that refuses without a
        reroute, produces a result about a completion whose exchange already
        concluded and bound a verdict. Returned as-is with no run named, that
        verdict is unreadable and the continuation pays for a second full run.
        """
        exchange_run = _run(tmp_path, "exchange-1")

        outcome = _outcome(
            review_exchange_run=exchange_run,
            early_result=ProcessingResult(success=False, message="push failed"),
        )

        assert outcome.early_result is not None
        assert outcome.early_result.review_exchange_run == exchange_run
        assert outcome.review_exchange_run == exchange_run

    def test_a_result_that_named_its_own_run_keeps_it(self, tmp_path: Path) -> None:
        """The reroute allocates a SECOND exchange run, and owns that answer.

        Its result is evidence about the exchange it just ran, not about the
        one whose approval the failing validation superseded. Overwriting it
        with the outcome's run would bind the wrong commit's verdict.
        """
        first = _run(tmp_path, "exchange-1")
        rerouted = _run(tmp_path, "exchange-2")

        outcome = _outcome(
            review_exchange_run=first,
            early_result=ProcessingResult(
                success=False,
                message="reroute halted",
                review_exchange_run=rerouted,
            ),
        )

        assert outcome.early_result is not None
        assert outcome.early_result.review_exchange_run == rerouted

    def test_no_exchange_ran_leaves_the_early_result_naming_none(
        self, tmp_path: Path
    ) -> None:
        """Absent stays absent: there is no run, so nothing may be invented."""
        outcome = _outcome(
            review_exchange_run=None,
            early_result=ProcessingResult(success=False, message="push failed"),
        )

        assert outcome.early_result is not None
        assert outcome.early_result.review_exchange_run is None

    def test_the_supplied_result_is_not_mutated(self, tmp_path: Path) -> None:
        """The caller's own object is left alone; a copy carries the run.

        ``ProcessingResult`` is not frozen, and the early result reaching here
        is one an action handler still holds a reference to.
        """
        supplied = ProcessingResult(success=False, message="push failed")

        _outcome(review_exchange_run=_run(tmp_path, "exchange-1"), early_result=supplied)

        assert supplied.review_exchange_run is None


class TestOutcomesWithNoEarlyResult:
    def test_a_deferred_pass_carries_the_run_and_no_early_result(
        self, tmp_path: Path
    ) -> None:
        outcome = _outcome(review_exchange_run=None, deferred=True)

        assert outcome.deferred is True
        assert outcome.early_result is None
        assert outcome.review_exchange_run is None

    def test_a_completed_pass_carries_the_run_for_its_one_reader(
        self, tmp_path: Path
    ) -> None:
        exchange_run = _run(tmp_path, "exchange-1")

        outcome = _outcome(review_exchange_run=exchange_run)

        assert outcome.deferred is False
        assert outcome.early_result is None
        assert outcome.review_exchange_run == exchange_run


class TestTheSettlementLeavesTheExecutionPhase:
    """The proof the publication settler makes has to reach its two readers.

    The settler runs inside action execution; the code-validation gate and the
    completion action planner both run after it. An outcome that did not name
    the settlement would leave the pipeline's strongest statement about the run
    as a log line — which is exactly what an earlier round of #337 did.
    """

    def test_an_outcome_names_the_settlement_it_was_built_with(
        self, tmp_path: Path
    ) -> None:
        settlement = CompletionSettlement(
            code_candidate=CodeCandidateSettlement.settled_zero_code("no commits"),
            result_only=ResultOnlyDelivery.settled("no commits"),
        )

        outcome = _outcome(review_exchange_run=None, settlement=settlement)

        assert outcome.settlement == settlement

    def test_an_exit_that_proved_nothing_says_so_neutrally(
        self, tmp_path: Path
    ) -> None:
        """Fail-safe: unsettled keeps today's gate and today's lifecycle."""
        outcome = _outcome(review_exchange_run=None)

        assert outcome.settlement.code_candidate.offers_code_candidate is True
        assert outcome.settlement.result_only.delivered is False


class TestNarrowingTwoSettlingSeams:
    """Completion processing settles at two seams, and neither may erase the other.

    The pre-action owner settles a tech_lead planning run; the publication
    settler settles an ordinary zero-code run. They never both fire, so the
    combination must let a proof through from EITHER side while a phase that
    proved nothing stays neutral in both directions.
    """

    def test_a_later_proof_is_not_suppressed_by_an_earlier_neutral_answer(
        self,
    ) -> None:
        settled = CompletionSettlement(
            code_candidate=CodeCandidateSettlement.settled_zero_code("proved"),
            result_only=ResultOnlyDelivery.settled("proved"),
        )

        combined = CompletionSettlement.unsettled().narrowed_by(settled)

        assert combined.code_candidate.offers_code_candidate is False
        assert combined.code_candidate.detail == "proved"
        assert combined.result_only.delivered is True
        assert combined.result_only.detail == "proved"

    def test_an_earlier_proof_is_not_erased_by_a_later_neutral_answer(self) -> None:
        """The direction that matters most: a proof survives a silent phase."""
        settled = CompletionSettlement(
            code_candidate=CodeCandidateSettlement.settled_zero_code("proved"),
            result_only=ResultOnlyDelivery.settled("proved"),
        )

        combined = settled.narrowed_by(CompletionSettlement.unsettled())

        assert combined.code_candidate.offers_code_candidate is False
        assert combined.code_candidate.detail == "proved"
        assert combined.result_only.delivered is True

    def test_two_neutral_answers_stay_neutral(self) -> None:
        combined = CompletionSettlement.unsettled().narrowed_by(
            CompletionSettlement.unsettled()
        )

        assert combined == CompletionSettlement.unsettled()


class TestTheSettlementIsCarriedOntoTheResult:
    def test_carrying_names_both_halves_on_the_result(self) -> None:
        """Both readers read the ProcessingResult; both fields must be set."""
        settlement = CompletionSettlement(
            code_candidate=CodeCandidateSettlement.settled_zero_code("proved"),
            result_only=ResultOnlyDelivery.settled("proved"),
        )

        carried = settlement.carried_by(
            ProcessingResult(success=True, message="published")
        )

        assert carried.code_candidate.offers_code_candidate is False
        assert carried.result_only.delivered is True
        assert carried.message == "published"

    def test_the_supplied_result_is_not_mutated(self) -> None:
        supplied = ProcessingResult(success=True, message="published")

        CompletionSettlement(
            code_candidate=CodeCandidateSettlement.settled_zero_code("proved"),
            result_only=ResultOnlyDelivery.settled("proved"),
        ).carried_by(supplied)

        assert supplied.code_candidate.offers_code_candidate is True
        assert supplied.result_only.delivered is False
