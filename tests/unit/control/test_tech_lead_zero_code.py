"""The trusted zero-code selector for a planning completion (#202).

Six facts decide the lane, and every one of them is required. These tests fix
each refusal individually, because the whole value of the selector is that an
unproven answer is a refusal rather than a benefit of the doubt: a planning run
with orchestrator-observed code or commit changes must never silently receive
zero-code treatment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.tech_lead_zero_code import (
    settle_zero_code_planning_completion,
)
from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)

LAUNCH_SHA = "a" * 40
MOVED_SHA = "b" * 40

# What ``coding-done completed`` hands EVERY completion, planning runs included.
COMPLETED_INTENTS = (
    RequestedAction.PUSH_BRANCH,
    RequestedAction.CREATE_PR,
    RequestedAction.ADD_NEEDS_HUMAN_LABEL,
)

# An enumeration that SUCCEEDED and found nothing, kept distinguishable from
# the ``None`` that means the enumeration itself failed.
CLEAN: list[str] = []


class FakeWorktreeReader:
    """The two orchestrator-side reads, each independently failable.

    ``None`` means the read itself failed — the case the selector must never
    collapse into "nothing changed".
    """

    def __init__(
        self,
        *,
        head: str | None = LAUNCH_SHA,
        dirt: list[str] | None = CLEAN,
    ) -> None:
        self._head = head
        self._dirt = list(dirt) if dirt is not None else None
        self.dirt_modes: list[str] = []

    def get_head_sha(self, worktree: Path) -> str | None:
        return self._head

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        self.dirt_modes.append(mode)
        return self._dirt


def planning_authority(*, launch_base_sha: str = LAUNCH_SHA) -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
        anchor_issue_number=23,
        focus_issue_number=23,
        launch_base_sha=launch_base_sha,
    )


def settle(authority: TechLeadLaunchAuthority, reader: FakeWorktreeReader):
    return settle_zero_code_planning_completion(
        authority=authority,
        requested_actions=COMPLETED_INTENTS,
        worktree=Path("/scratch/tech-lead-planning-23"),
        worktree_reader=reader,
    )


class TestTheProvenZeroCodeRun:
    """The one shape that earns the lane."""

    def test_publication_intent_is_dropped_and_nothing_else_is(self) -> None:
        settlement = settle(planning_authority(), FakeWorktreeReader())

        assert settlement.zero_code is True
        assert settlement.requested_actions == (RequestedAction.ADD_NEEDS_HUMAN_LABEL,)

    def test_both_publication_intents_go_together(self) -> None:
        """Shape A — dropping only PUSH_BRANCH — is rejected.

        A record still carrying ``CREATE_PR`` reports
        ``offers_a_change_for_review``, which is what keeps the publication
        gate and the review exchange running for a completion offering nothing.
        """
        settlement = settle(planning_authority(), FakeWorktreeReader())

        assert RequestedAction.PUSH_BRANCH not in settlement.requested_actions
        assert RequestedAction.CREATE_PR not in settlement.requested_actions

    def test_the_dirt_question_is_asked_of_tracked_content(self) -> None:
        """The dirt vocabulary has one owner; this asks it, it does not re-answer it."""
        reader = FakeWorktreeReader()

        settle(planning_authority(), reader)

        assert reader.dirt_modes == ["tracked"]


class TestTheHardNegatives:
    """Every direction in which the lane must refuse."""

    def test_head_moved_after_launch_keeps_the_publication_path(self) -> None:
        settlement = settle(
            planning_authority(), FakeWorktreeReader(head=MOVED_SHA)
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert MOVED_SHA in settlement.detail

    def test_blocking_tracked_dirt_keeps_the_publication_path(self) -> None:
        settlement = settle(
            planning_authority(),
            FakeWorktreeReader(dirt=["src/issue_orchestrator/control/planner.py"]),
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert "planner.py" in settlement.detail

    def test_an_unreadable_head_fails_closed(self) -> None:
        settlement = settle(planning_authority(), FakeWorktreeReader(head=None))

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS

    def test_an_unenumerable_worktree_fails_closed(self) -> None:
        """A failed enumeration is not an empty one."""
        settlement = settle(planning_authority(), FakeWorktreeReader(dirt=None))

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS

    @pytest.mark.parametrize(
        "flavor",
        [
            TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            TechLeadSessionFlavor.BATCH_REVIEW,
            TechLeadSessionFlavor.HEALTH_REVIEW,
        ],
    )
    def test_every_other_flavor_keeps_the_publication_path(
        self, flavor: TechLeadSessionFlavor
    ) -> None:
        """Even a flavor whose checkout is provably unchanged (#202)."""
        authority = TechLeadLaunchAuthority(
            flavor=flavor,
            anchor_issue_number=23,
            focus_issue_number=23 if flavor.is_issue_focused else None,
            launch_base_sha=LAUNCH_SHA,
        )

        settlement = settle(authority, FakeWorktreeReader())

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS
        assert flavor.value in settlement.detail

    def test_a_legacy_row_without_a_launch_base_is_never_exempt(self) -> None:
        """A record written before the field existed is ineligible, not guessed."""
        settlement = settle(
            planning_authority(launch_base_sha=""), FakeWorktreeReader()
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == COMPLETED_INTENTS

    def test_a_legacy_row_is_refused_before_any_worktree_read(self) -> None:
        """Fail-closed early: nothing about the checkout can rescue the row."""
        reader = FakeWorktreeReader()

        settle(planning_authority(launch_base_sha=""), reader)

        assert reader.dirt_modes == []
