"""The one owner of the rework-cycle budget (#297).

The arithmetic and the ordering of the refusals used to live inside
``PRScanner``. The continuation handoff needs the same answers, and a second
copy would be a second budget — so both now decide through this pure policy,
and it is tested here rather than through either caller.

Nothing here reads GitHub, a store or a clock, so every direction is stated
directly over the facts the callers hold: the PR's labels, the issue's labels,
and what the engine is already doing about the issue.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.rework_cycle_policy import (
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
    next_rework_cycle,
)
from issue_orchestrator.infra.config import Config

ISSUE_NUMBER = 297


@pytest.fixture
def labels() -> LabelManager:
    return LabelManager(Config())


@pytest.fixture
def budget(labels: LabelManager) -> ReworkCycleBudget:
    return ReworkCycleBudget(labels, max_rework_cycles=3)


def _admit(
    budget: ReworkCycleBudget,
    *,
    pr_labels: list[str] | None = None,
    issue_labels: list[str] | None = None,
    queued: set[int] | None = None,
    active: set[int] | None = None,
):
    return budget.admit(
        issue_number=ISSUE_NUMBER,
        pr_labels=pr_labels if pr_labels is not None else [],
        issue_labels=issue_labels if issue_labels is not None else [],
        queued_issue_numbers=queued if queued is not None else set(),
        active_issue_numbers=active if active is not None else set(),
    )


class TestTheCycleNumber:
    """The durable ``rework-cycle-N`` label is the counter; nothing else is."""

    def test_an_unlabelled_pr_has_spent_nothing(
        self, labels: LabelManager
    ) -> None:
        assert next_rework_cycle([], labels) == 1

    def test_the_next_cycle_follows_the_highest_label(
        self, labels: LabelManager
    ) -> None:
        assert next_rework_cycle(["rework-cycle-2"], labels) == 3

    def test_the_budget_counts_the_same_way_the_function_does(
        self, budget: ReworkCycleBudget, labels: LabelManager
    ) -> None:
        for pr_labels in ([], ["rework-cycle-1"], ["rework-cycle-5"]):
            assert budget.next_cycle(pr_labels) == next_rework_cycle(
                pr_labels, labels
            )


class TestTheOrderOfRefusals:
    """Order is the policy, so each refusal is proved to outrank the next."""

    def test_a_clean_pr_takes_the_first_cycle(
        self, budget: ReworkCycleBudget
    ) -> None:
        admission = _admit(budget)

        assert admission.verdict is ReworkAdmissionVerdict.QUEUE
        assert admission.rework_cycle == 1

    def test_a_queued_issue_is_refused_before_anything_else(
        self, budget: ReworkCycleBudget
    ) -> None:
        admission = _admit(
            budget,
            pr_labels=["rework-cycle-9"],
            issue_labels=["needs-human"],
            queued={ISSUE_NUMBER},
        )

        assert admission.verdict is ReworkAdmissionVerdict.SKIP
        assert admission.reason == "already_queued"

    def test_an_active_session_is_refused_before_the_pr_is_read(
        self, budget: ReworkCycleBudget
    ) -> None:
        admission = _admit(budget, active={ISSUE_NUMBER})

        assert admission.reason == "active_session"

    def test_a_blocking_pr_label_outranks_the_budget(
        self, budget: ReworkCycleBudget
    ) -> None:
        admission = _admit(budget, pr_labels=["needs-human", "rework-cycle-9"])

        assert admission.verdict is ReworkAdmissionVerdict.SKIP
        assert admission.reason == "blocking_label"
        assert "needs-human" in admission.blocking_labels
        # The number is a fact about the PR, reported even in refusal.
        assert admission.rework_cycle == 10

    def test_a_blocking_issue_label_refuses_a_reworkable_pr(
        self, budget: ReworkCycleBudget
    ) -> None:
        """A publish failure blocks the issue while ``needs-rework`` stands on
        the PR; the issue's label is what must win."""
        admission = _admit(budget, issue_labels=["needs-human"])

        assert admission.reason == "issue_blocked"

    def test_the_ceiling_escalates_rather_than_queueing(
        self, budget: ReworkCycleBudget
    ) -> None:
        admission = _admit(budget, pr_labels=["rework-cycle-3"])

        assert admission.verdict is ReworkAdmissionVerdict.ESCALATE
        assert admission.escalates is True
        assert admission.rework_cycle == 4
        assert admission.reason == "max_rework_exceeded"

    def test_the_last_cycle_under_the_ceiling_is_still_granted(
        self, budget: ReworkCycleBudget
    ) -> None:
        admission = _admit(budget, pr_labels=["rework-cycle-2"])

        assert admission.queues is True
        assert admission.rework_cycle == 3


class TestTheCheapHalfAgreesWithTheWholeDecision:
    """``already_held`` is a SPLIT of ``admit``, never a second answer."""

    def test_it_refuses_exactly_what_admit_refuses(
        self, budget: ReworkCycleBudget
    ) -> None:
        for queued, active in (
            ({ISSUE_NUMBER}, set[int]()),
            (set[int](), {ISSUE_NUMBER}),
        ):
            held = budget.already_held(
                ISSUE_NUMBER,
                queued_issue_numbers=queued,
                active_issue_numbers=active,
            )
            whole = _admit(budget, queued=queued, active=active)

            assert held is not None
            assert held == whole

    def test_it_stays_silent_when_nothing_holds_the_issue(
        self, budget: ReworkCycleBudget
    ) -> None:
        assert (
            budget.already_held(
                ISSUE_NUMBER,
                queued_issue_numbers=set(),
                active_issue_numbers=set(),
            )
            is None
        )


class TestARefusalCostsOnlyWhatTheCallerAlreadyHolds:
    """Whichever label set a caller has for free settles what it can settle.

    Both callers hold one side without paying for it — the sweep found the PR by
    label, the handoff arrives holding a board issue — and every refusal that
    side can reach must be reachable before the other side is read.
    """

    def test_a_blocking_pr_label_alone_refuses(
        self, budget: ReworkCycleBudget
    ) -> None:
        held = budget.already_held(
            ISSUE_NUMBER,
            queued_issue_numbers=set(),
            active_issue_numbers=set(),
            pr_labels=["needs-rework", "needs-human"],
        )

        assert held is not None
        assert held.reason == "blocking_label"
        assert held.verdict is ReworkAdmissionVerdict.SKIP
        assert held.blocking_labels == ("needs-human",)

    def test_a_blocking_issue_label_alone_refuses(
        self, budget: ReworkCycleBudget
    ) -> None:
        held = budget.already_held(
            ISSUE_NUMBER,
            queued_issue_numbers=set(),
            active_issue_numbers=set(),
            issue_labels=["agent:backend", "needs-human"],
        )

        assert held is not None
        assert held.reason == "issue_blocked"

    def test_an_unread_label_set_is_not_an_empty_one(
        self, budget: ReworkCycleBudget
    ) -> None:
        """``None`` must skip that side's refusal, not answer it as "clean"."""
        assert (
            budget.already_held(
                ISSUE_NUMBER,
                queued_issue_numbers=set(),
                active_issue_numbers=set(),
                pr_labels=None,
                issue_labels=None,
            )
            is None
        )

    def test_an_unread_pr_reports_an_unknown_cycle_not_the_first(
        self, budget: ReworkCycleBudget
    ) -> None:
        held = budget.already_held(
            ISSUE_NUMBER,
            queued_issue_numbers=set(),
            active_issue_numbers=set(),
            issue_labels=["needs-human"],
        )

        assert held is not None
        assert held.rework_cycle == 0

    def test_a_caller_holding_the_pr_still_reports_the_cycle_it_refused(
        self, budget: ReworkCycleBudget
    ) -> None:
        held = budget.already_held(
            ISSUE_NUMBER,
            queued_issue_numbers=set(),
            active_issue_numbers=set(),
            pr_labels=["rework-cycle-2", "needs-human"],
        )

        assert held is not None
        assert held.rework_cycle == 3

    @pytest.mark.parametrize(
        ("pr_labels", "issue_labels"),
        [
            (["needs-human"], []),
            ([], ["needs-human"]),
            (["rework-cycle-2"], []),
            ([], []),
        ],
    )
    def test_the_full_picture_still_answers_exactly_as_admit_does(
        self,
        budget: ReworkCycleBudget,
        pr_labels: list[str],
        issue_labels: list[str],
    ) -> None:
        held = budget.already_held(
            ISSUE_NUMBER,
            queued_issue_numbers=set(),
            active_issue_numbers=set(),
            pr_labels=pr_labels,
            issue_labels=issue_labels,
        )
        whole = _admit(budget, pr_labels=pr_labels, issue_labels=issue_labels)

        if held is None:
            assert whole.verdict is not ReworkAdmissionVerdict.SKIP
        else:
            assert held == whole
