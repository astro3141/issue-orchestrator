"""What a tech_lead completion may still ask for, before any action runs (#257).

The completion processor's pre-action seam asks this owner exactly once, for
every outcome. These tests pin the owner's own answers: which completions are
held to the admission contract, which policies shape the survivors, and where
each one refuses.

The end-to-end direction — that a shaped tuple is what the generic action
executor actually receives, so no push happens and no label is applied — lives
in ``tests/unit/test_completion_processor.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_TECH_LEAD_DECISION,
)
from issue_orchestrator.control.subject_recovery_authority import (
    SubjectRecoveryAuthority,
)
from issue_orchestrator.control.tech_lead_completion import (
    settle_tech_lead_completion,
)
from issue_orchestrator.domain.models import (
    PUBLICATION_ACTIONS,
    SUBJECT_RECOVERY_ACTIONS,
    CompletionOutcome,
    RequestedAction,
)
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)

LAUNCH_SHA = "e" * 40
RUN_ID = "20260824T153405000000Z"
SESSION_NAME = "issue-23"

# What ``coding-done blocked`` writes, planning runs included.
BLOCKED_INTENTS = (
    RequestedAction.PUSH_BRANCH,
    RequestedAction.ADD_BLOCKED_LABEL,
    RequestedAction.POST_COMMENT,
)


class FakeWorktreeReader:
    """The two orchestrator-side reads the zero-code proof needs."""

    def __init__(self, *, head: str | None = LAUNCH_SHA) -> None:
        self._head = head

    def get_head_sha(self, worktree: Path) -> str | None:
        return self._head

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        return []


class ArmedRun:
    """One armed tech_lead run: the worktree copy and the authority row."""

    def __init__(
        self,
        tmp_path: Path,
        flavor: TechLeadSessionFlavor,
        *,
        launch_base_sha: str = LAUNCH_SHA,
    ) -> None:
        self.config = Config()
        self.config.repo_root = tmp_path
        self.config.tech_lead_review_agent = "agent:tech-lead"
        self.store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
        self.run_dir = tmp_path / "runs" / RUN_ID
        self.assignment_path = (
            self.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        )
        focused = flavor.is_issue_focused
        TechLeadAssignment(
            flavor=flavor,
            focus_issue_number=23 if focused else None,
            focus_reason="Prepare: open and unblocked" if focused else "",
        ).write(self.assignment_path)
        self.store.record(
            run_id=RUN_ID,
            session_name=SESSION_NAME,
            authority=TechLeadLaunchAuthority(
                flavor=flavor,
                anchor_issue_number=23,
                focus_issue_number=23 if focused else None,
                launch_base_sha=launch_base_sha,
            ),
        )

    def settle(
        self,
        *,
        outcome: CompletionOutcome = CompletionOutcome.BLOCKED,
        requested_actions: tuple[RequestedAction, ...] = BLOCKED_INTENTS,
        reader: FakeWorktreeReader | None = None,
    ):
        return settle_tech_lead_completion(
            self.config,
            tech_lead_authority=self.store,
            run_dir=self.run_dir,
            run_id=RUN_ID,
            session_name=SESSION_NAME,
            outcome=outcome,
            requested_actions=requested_actions,
            worktree=Path("/scratch/tech-lead-planning-23"),
            worktree_reader=reader or FakeWorktreeReader(),
        )


def arm(
    tmp_path: Path,
    flavor: TechLeadSessionFlavor,
    *,
    launch_base_sha: str = LAUNCH_SHA,
) -> ArmedRun:
    return ArmedRun(tmp_path, flavor, launch_base_sha=launch_base_sha)


class TestTheRecoveryAnswerHasOneOwner:
    """No second ``planning_investigation`` match decides recovery here (#182)."""

    @pytest.mark.parametrize("flavor", list(TechLeadSessionFlavor))
    def test_the_capability_table_decides_which_requests_survive(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor
    ) -> None:
        """Re-derived from the owner, never from a list of flavors restated here.

        A role that later GAINS a recovery kind keeps its ``add_blocked_label``
        request in the same edit that gives it back the planned label — which is
        the whole point of asking :class:`SubjectRecoveryAuthority` rather than
        matching the flavor at this seam.
        """
        run = arm(tmp_path, flavor)
        may_recover = SubjectRecoveryAuthority.for_flavor(
            flavor
        ).may_leave_recovery_label

        lane = run.settle()

        assert (
            RequestedAction.ADD_BLOCKED_LABEL in lane.requested_actions
        ) is may_recover

    def test_exactly_the_recovery_vocabulary_is_dropped(self, tmp_path: Path) -> None:
        """Offered every action there is, the bounded role loses precisely those.

        The launch base is cleared so the zero-code lane refuses, isolating the
        recovery policy: what is missing is :data:`SUBJECT_RECOVERY_ACTIONS`
        itself rather than a literal restated here, so an action the domain
        later adopts into that family leaves this seam in the same edit.
        """
        run = arm(
            tmp_path,
            TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            launch_base_sha="",
        )
        every_action = tuple(RequestedAction)

        lane = run.settle(requested_actions=every_action)

        assert lane.zero_code is False
        assert set(every_action) - set(lane.requested_actions) == set(
            SUBJECT_RECOVERY_ACTIONS
        )
        # Order among the survivors is the caller's, untouched.
        assert lane.requested_actions == tuple(
            action
            for action in every_action
            if action not in SUBJECT_RECOVERY_ACTIONS
        )

    def test_a_proven_zero_code_block_loses_both_families(
        self, tmp_path: Path
    ) -> None:
        """The two policies compose; neither is the other's precondition."""
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        lane = run.settle(requested_actions=tuple(RequestedAction))

        assert lane.zero_code is True
        assert set(tuple(RequestedAction)) - set(lane.requested_actions) == set(
            PUBLICATION_ACTIONS | SUBJECT_RECOVERY_ACTIONS
        )

    def test_the_trace_names_the_recovery_requests_it_dropped(
        self, tmp_path: Path
    ) -> None:
        """``detail`` is the operator's trace, so a drop must be visible in it.

        It used to explain only the publication lane, so a blocked planning run
        whose ``add_blocked_label`` had just been refused logged nothing about
        the one thing #257 is for (round 1 review N1).
        """
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        lane = run.settle(
            requested_actions=(
                RequestedAction.PUSH_BRANCH,
                RequestedAction.ADD_BLOCKED_LABEL,
            )
        )

        assert "add_blocked_label" in lane.detail
        assert "holds no recovery authority" in lane.detail

    def test_the_trace_is_unchanged_for_a_run_that_kept_its_requests(
        self, tmp_path: Path
    ) -> None:
        """Nothing refused, nothing appended — the publication lane reads as before."""
        run = arm(tmp_path, TechLeadSessionFlavor.FAILURE_INVESTIGATION)

        lane = run.settle(
            requested_actions=(
                RequestedAction.PUSH_BRANCH,
                RequestedAction.ADD_BLOCKED_LABEL,
            )
        )

        assert "recovery requests dropped" not in lane.detail


class TestWhatABlockedRunIsAndIsNotAskedFor:
    """A run that did not land is governed, but never asked to have landed."""

    def test_no_decision_artifact_is_required(self, tmp_path: Path) -> None:
        """Nothing was written to the run dir but the assignment copy.

        Requiring the completed-decision pair to settle side-effect policy would
        either reject every honest block or invite a fabricated artifact.
        """
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        lane = run.settle()

        assert lane.rejection is None
        assert lane.zero_code is True
        assert lane.requested_actions == (RequestedAction.POST_COMMENT,)

    def test_a_completed_run_still_owes_its_decision_pair(
        self, tmp_path: Path
    ) -> None:
        """The COMPLETED admission contract is untouched (#6761 F3)."""
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        lane = run.settle(outcome=CompletionOutcome.COMPLETED)

        assert lane.rejection is not None
        assert lane.rejection.startswith(ERROR_PREFIX_TECH_LEAD_DECISION)

    def test_a_rejected_completion_carries_its_actions_untouched(
        self, tmp_path: Path
    ) -> None:
        """A refusal is not a settlement: the caller must take zero action."""
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        lane = run.settle(outcome=CompletionOutcome.COMPLETED)

        assert lane.requested_actions == BLOCKED_INTENTS
        assert lane.zero_code is False

    def test_the_flavor_comes_from_the_orchestrators_own_row(
        self, tmp_path: Path
    ) -> None:
        """A deleted worktree copy cannot buy back the authority the role lacks.

        Tamper evidence is fatal for a COMPLETED run — that gate is unchanged —
        but it must not un-classify a BLOCKED one: the flavor is read from the
        orchestrator-owned record the agent never touches, the same way
        ``resolve_subject_recovery_authority`` reads it for the planned half.
        """
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)
        run.assignment_path.unlink()

        lane = run.settle()

        assert lane.rejection is None
        assert RequestedAction.ADD_BLOCKED_LABEL not in lane.requested_actions

    def test_an_unrecorded_run_is_governed_by_neither_policy(
        self, tmp_path: Path
    ) -> None:
        """Unproven role, conservative direction: today's generic behaviour."""
        run = arm(tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION)
        run.store.discard(run_id=RUN_ID, session_name=SESSION_NAME)

        lane = run.settle()

        assert lane.rejection is None
        assert lane.zero_code is False
        assert lane.requested_actions == BLOCKED_INTENTS
        assert "launch-authority" in lane.detail
