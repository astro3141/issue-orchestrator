"""The trusted zero-code selector for an ORDINARY completion (#337).

Five facts decide the lane, and every one of them is required. These tests fix
each refusal individually, because the whole value of the selector is that an
unproven answer is a refusal rather than a benefit of the doubt: an ordinary run
that produced commits, or whose checkout could not be read, must never silently
be excused from publishing a branch.

The positive direction matters just as much here as the negative one, and more
than it does for the planning lane: an ordinary evidence run's whole deliverable
is the issue comment, so ``POST_COMMENT`` surviving the settlement is the
outcome the lane exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_types import CompletionSettlement
from issue_orchestrator.control.ordinary_zero_code import (
    ZeroCodeOrdinarySettlement,
    settle_ordinary_publication_plan,
    settle_zero_code_ordinary_completion,
)
from issue_orchestrator.control.review_publish_pipeline import PublishPipelinePlan
from issue_orchestrator.domain.models import (
    PUBLICATION_ACTIONS,
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.ports.working_copy import BranchCommitsResult

HEAD_SHA = "c" * 40
BASE_REF = "origin/main"
WORKTREE = Path("/worktrees/issue-orchestrator-41")

# Exactly what ``coding-done completed`` hands every ordinary completion, in
# the order it hands them — the tuple #336 measured dying at ``create_pr``.
COMPLETED_INTENTS = (
    RequestedAction.PUSH_BRANCH,
    RequestedAction.CREATE_PR,
    RequestedAction.POST_COMMENT,
)

# An enumeration that SUCCEEDED and found nothing, kept distinguishable from
# the ``None`` that means the enumeration itself failed.
CLEAN: list[str] = []


class FakeCandidateReader:
    """The three orchestrator-side reads, each independently failable."""

    def __init__(
        self,
        *,
        head: str | None = HEAD_SHA,
        dirt: list[str] | None = CLEAN,
        commits: BranchCommitsResult | None = None,
    ) -> None:
        self._head = head
        self._dirt = list(dirt) if dirt is not None else None
        self._commits = commits or BranchCommitsResult(success=True, count=0)
        self.dirt_modes: list[str] = []
        self.counted_against: list[str] = []

    def get_head_sha(self, worktree: Path) -> str | None:
        return self._head

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        self.dirt_modes.append(mode)
        return self._dirt

    def commits_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchCommitsResult:
        self.counted_against.append(base_ref)
        return self._commits


def settle(
    reader: FakeCandidateReader,
    *,
    outcome: CompletionOutcome = CompletionOutcome.COMPLETED,
    requested_actions: tuple[RequestedAction, ...] = COMPLETED_INTENTS,
):
    return settle_zero_code_ordinary_completion(
        outcome=outcome,
        requested_actions=requested_actions,
        worktree=WORKTREE,
        base_ref=BASE_REF,
        worktree_reader=reader,
    )


class TestTheProvenZeroCodeRun:
    """The one shape that earns the lane."""

    def test_the_reviewed_result_still_publishes_to_the_issue(self) -> None:
        """The whole point: the branch write goes, the comment stays.

        This is the exact defect #336 measured — ``post_comment`` sequenced
        third behind a ``create_pr`` a zero-commit branch can never complete.
        """
        settlement = settle(FakeCandidateReader())

        assert settlement.zero_code is True
        assert settlement.requested_actions == (RequestedAction.POST_COMMENT,)

    def test_both_publication_intents_go_together(self) -> None:
        """Dropping only PUSH_BRANCH would leave the PR demand standing."""
        settlement = settle(FakeCandidateReader())

        assert RequestedAction.PUSH_BRANCH not in settlement.requested_actions
        assert RequestedAction.CREATE_PR not in settlement.requested_actions

    def test_exactly_what_the_domain_calls_publication_is_dropped(self) -> None:
        """The publication vocabulary has one owner; this asks it too.

        Offered EVERY action there is, so the set removed is pinned to
        :data:`PUBLICATION_ACTIONS` itself rather than to a literal restated
        here: an action the domain later adopts into the publication family
        leaves this lane in the same edit.
        """
        every_action = tuple(RequestedAction)

        settlement = settle(FakeCandidateReader(), requested_actions=every_action)

        assert set(every_action) - set(settlement.requested_actions) == set(
            PUBLICATION_ACTIONS
        )
        # Order among the survivors is the caller's, untouched.
        assert settlement.requested_actions == tuple(
            action for action in every_action if action not in PUBLICATION_ACTIONS
        )

    def test_what_survives_no_longer_reaches_the_remote(self) -> None:
        """The consumers of the same vocabulary agree, on the record."""
        settlement = settle(FakeCandidateReader())
        record = CompletionRecord(
            session_id="issue-41",
            timestamp="2026-08-28T00:00:00Z",
            outcome=CompletionOutcome.COMPLETED,
            summary="measurement result",
            requested_actions=list(settlement.requested_actions),
        )

        assert record.reaches_the_remote is False
        assert record.offers_a_change_for_review is False

    def test_the_dirt_question_is_asked_of_tracked_content(self) -> None:
        """The dirt vocabulary has one owner; this asks it, never re-answers it."""
        reader = FakeCandidateReader()

        settle(reader)

        assert reader.dirt_modes == ["tracked"]

    def test_the_commit_count_is_taken_against_the_caller_s_base(self) -> None:
        """The base a PR would target is the completion path's answer, not ours."""
        reader = FakeCandidateReader()

        settle(reader)

        assert reader.counted_against == [BASE_REF]


class TestTheHardNegatives:
    """Every direction in which the lane must refuse."""

    def test_a_branch_with_commits_keeps_the_publication_path(self) -> None:
        """The failure-direction case: defeat the proof, lose the lane."""
        settlement = settle(
            FakeCandidateReader(commits=BranchCommitsResult(success=True, count=3))
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert "3 commit(s)" in settlement.detail

    def test_blocking_tracked_dirt_keeps_the_publication_path(self) -> None:
        settlement = settle(
            FakeCandidateReader(dirt=["src/issue_orchestrator/control/planner.py"])
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert "planner.py" in settlement.detail

    def test_an_unreadable_head_fails_closed(self) -> None:
        settlement = settle(FakeCandidateReader(head=None))

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS

    def test_an_unenumerable_worktree_fails_closed(self) -> None:
        """A failed enumeration is not an empty one."""
        settlement = settle(FakeCandidateReader(dirt=None))

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS

    def test_an_uncountable_branch_fails_closed(self) -> None:
        """A git failure is not a branch with nothing in it."""
        settlement = settle(
            FakeCandidateReader(
                commits=BranchCommitsResult(success=False, error="unknown revision")
            )
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert "unknown revision" in settlement.detail

    def test_the_checkout_is_not_even_read_for_a_run_that_asks_no_branch(self) -> None:
        """Nothing to drop is not the same fact as a proven-clean checkout."""
        reader = FakeCandidateReader()

        settlement = settle(
            reader, requested_actions=(RequestedAction.POST_COMMENT,)
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == (RequestedAction.POST_COMMENT,)
        assert reader.dirt_modes == []
        assert reader.counted_against == []

    @pytest.mark.parametrize(
        "outcome",
        [
            CompletionOutcome.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN,
            CompletionOutcome.REVIEW_APPROVED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED,
        ],
    )
    def test_only_a_landed_run_can_settle_here(
        self, outcome: CompletionOutcome
    ) -> None:
        """A report of failure is not a proof that nothing needed publishing."""
        settlement = settle(FakeCandidateReader(), outcome=outcome)

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert outcome.value in settlement.detail


class TestThePlanTheCallerCarriesForward:
    """The command form: what the completion path is left holding."""

    def _plan(self) -> PublishPipelinePlan:
        return PublishPipelinePlan(
            ordered_actions=COMPLETED_INTENTS,
            run_review_exchange_before_publish=True,
        )

    def _settle_plan(self, reader: FakeCandidateReader, *, governed_elsewhere: bool):
        return settle_ordinary_publication_plan(
            plan=self._plan(),
            outcome=CompletionOutcome.COMPLETED,
            worktree=WORKTREE,
            base_ref=BASE_REF,
            worktree_reader=reader,
            governed_elsewhere=governed_elsewhere,
            issue_number=41,
        )

    def test_a_proven_run_carries_only_the_comment_forward(self) -> None:
        settled = self._settle_plan(FakeCandidateReader(), governed_elsewhere=False)

        assert settled.plan.ordered_actions == (RequestedAction.POST_COMMENT,)

    def test_the_rest_of_the_plan_is_left_alone(self) -> None:
        """Only the actions are settled; the pipeline's own decision is not."""
        settled = self._settle_plan(FakeCandidateReader(), governed_elsewhere=False)

        assert settled.plan.run_review_exchange_before_publish is True

    def test_a_run_another_owner_governs_is_handed_back_untouched(self) -> None:
        """Tech_lead publication intent is settled at the pre-action seam."""
        reader = FakeCandidateReader()

        settled = self._settle_plan(reader, governed_elsewhere=True)

        assert settled.plan == self._plan()
        assert reader.dirt_modes == []
        assert reader.counted_against == []

    def test_a_run_another_owner_governs_proves_nothing_here(self) -> None:
        """Its own owner already answered both; a second answer is drift.

        A tech_lead planning run IS settled out of the code lane — by the
        pre-action seam, whose answer this module must not overwrite. Claiming
        the proof here would be the same rule enforced twice, from evidence
        this module never gathered (it read nothing at all above).
        """
        settled = self._settle_plan(FakeCandidateReader(), governed_elsewhere=True)

        assert settled.settlement == CompletionSettlement.unsettled()

    def test_a_code_bearing_run_keeps_the_whole_plan(self) -> None:
        settled = self._settle_plan(
            FakeCandidateReader(commits=BranchCommitsResult(success=True, count=1)),
            governed_elsewhere=False,
        )

        assert settled.plan == self._plan()


class TestWhatTheSettlementTellsThePhasesDownstream:
    """The proof leaves this module, or two later phases decide from less.

    An earlier round of #337 shaped the plan and logged the proof. The
    code-validation gate then ran over a commit the run did not produce, and the
    completion planner released the run's claim label with nothing to take its
    place. Both are decided from the settlement, so both are pinned here.
    """

    def _settle(self, reader: FakeCandidateReader) -> ZeroCodeOrdinarySettlement:
        return settle_zero_code_ordinary_completion(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=COMPLETED_INTENTS,
            worktree=WORKTREE,
            base_ref=BASE_REF,
            worktree_reader=reader,
        )

    def test_a_proven_run_offers_no_code_candidate_to_validate(self) -> None:
        """The #328 contract, carried by its second producer rather than dropped."""
        settlement = self._settle(FakeCandidateReader())

        assert settlement.code_candidate.offers_code_candidate is False
        assert settlement.code_candidate.detail == settlement.detail

    def test_a_proven_run_delivers_its_whole_result_as_the_comment(self) -> None:
        settlement = self._settle(FakeCandidateReader())

        assert settlement.result_only.delivered is True
        assert settlement.result_only.detail == settlement.detail

    def test_the_settlement_property_carries_both_halves_together(self) -> None:
        settlement = self._settle(FakeCandidateReader())

        assert settlement.settlement == CompletionSettlement(
            code_candidate=settlement.code_candidate,
            result_only=settlement.result_only,
        )

    @pytest.mark.parametrize(
        ("reader", "why"),
        [
            (
                FakeCandidateReader(commits=BranchCommitsResult(success=True, count=1)),
                "the branch adds a commit",
            ),
            (
                FakeCandidateReader(
                    commits=BranchCommitsResult(success=False, error="bad revision")
                ),
                "the count could not be read",
            ),
            (FakeCandidateReader(head=None), "HEAD could not be read"),
            (FakeCandidateReader(dirt=None), "the dirt could not be enumerated"),
            (FakeCandidateReader(dirt=["src/app.py"]), "tracked content is modified"),
        ],
    )
    def test_a_refusal_proves_nothing_and_says_so(
        self, reader: FakeCandidateReader, why: str
    ) -> None:
        """Fail-safe in the direction that matters: a refusal keeps today's path.

        A refusal is not evidence that a candidate exists — it is the absence of
        evidence that one does not. Both readers must therefore get the ordinary
        answer: the gate still runs, and the PR-carried lifecycle still applies.
        """
        settlement = self._settle(reader)

        assert settlement.zero_code is False, why
        assert settlement.code_candidate.offers_code_candidate is True
        assert settlement.result_only.delivered is False
