"""A Tech Lead completion cannot settle on validation it does not have (#385).

The command moved off the model session; the GATE did not. These tests hold the
pre-action seam to that: a COMPLETED tech_lead run reaches the trusted owner,
and every way that owner can fail to say "passed on this exact candidate" —
missing, failed, timed out, unavailable, raised, bound elsewhere, unreadable
head — refuses the completion outright.

The refusal shape matters as much as the refusal: it carries the tech_lead
error prefix, which is what routes a rejected completion to the tech_lead
terminal-effects owner (FAILED history, rejection surfaced on the anchor issue)
instead of to the generic publish-failure lane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.control.completion_pre_action import (
    settle_tech_lead_pre_action,
)
from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION,
)
from issue_orchestrator.control.tech_lead_completion import (
    settle_tech_lead_completion,
)
from issue_orchestrator.control.tech_lead_completion_errors import (
    has_tech_lead_refusal,
)
from issue_orchestrator.control.tech_lead_completion_validation import (
    UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR,
)
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_completion_validation import (
    TechLeadCompletionValidationStatus,
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
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.tech_lead_completion_validation_helpers import (
    StubTechLeadCompletionValidator,
    passing_completion_validator,
)

LAUNCH_SHA = "e" * 40
RUN_ID = "20260830T101112000000Z"
SESSION = "issue-23"
WORKTREE = Path("/scratch/tech-lead-planning-23")

COMPLETED_INTENTS = (
    RequestedAction.PUSH_BRANCH,
    RequestedAction.CREATE_PR,
    RequestedAction.POST_COMMENT,
)


class FakeWorktreeReader:
    def __init__(self, *, head: str | None = LAUNCH_SHA) -> None:
        self._head = head

    def get_head_sha(self, worktree: Path) -> str | None:
        return self._head

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        return []


@pytest.fixture
def armed(tmp_path: Path):
    """A COMPLETED batch-review run that clears the admission contract.

    Everything the admission gate wants is in place — trusted launch authority,
    matching worktree assignment, valid decision pair — so the only thing left
    for these tests to move is the completion validation.
    """
    config = Config()
    config.repo_root = tmp_path
    config.tech_lead_review_agent = "agent:tech-lead"
    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    run_dir = tmp_path / "runs" / RUN_ID
    data_dir = run_dir / "tech-lead-data"
    TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW).write(
        data_dir / TECH_LEAD_ASSIGNMENT_FILENAME
    )
    (data_dir / TECH_LEAD_DECISION_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Clean audit.",
                "findings": [],
                "proposed_actions": [],
            }
        )
    )
    (data_dir / TECH_LEAD_REPORT_FILENAME).write_text("# Report\n\nNothing found.\n")
    store.record(
        run_id=RUN_ID,
        session_name=SESSION,
        authority=TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=23,
            launch_base_sha=LAUNCH_SHA,
        ),
    )
    return config, store, run_dir


def _settle(armed, *, validator, reader: FakeWorktreeReader | None = None):
    config, store, run_dir = armed
    return settle_tech_lead_completion(
        config,
        tech_lead_authority=store,
        completion_validator=validator,
        run_dir=run_dir,
        run_id=RUN_ID,
        session_name=SESSION,
        outcome=CompletionOutcome.COMPLETED,
        requested_actions=COMPLETED_INTENTS,
        worktree=WORKTREE,
        worktree_reader=reader or FakeWorktreeReader(),
    )


class TestATrustedPassLetsTheCompletionSettle:
    def test_a_passing_verdict_is_not_a_rejection(self, armed) -> None:
        lane = _settle(armed, validator=passing_completion_validator())

        assert lane.rejection is None

    def test_the_owner_is_asked_about_the_head_the_orchestrator_read(
        self, armed
    ) -> None:
        validator = passing_completion_validator()

        _settle(armed, validator=validator)

        assert validator.calls == [
            {
                "run_id": RUN_ID,
                "session_name": SESSION,
                "worktree": WORKTREE,
                "candidate_head_sha": LAUNCH_SHA,
            }
        ]


class TestEveryFailClosedDirection:
    @pytest.mark.parametrize(
        "status",
        [
            TechLeadCompletionValidationStatus.FAILED,
            TechLeadCompletionValidationStatus.TIMED_OUT,
            TechLeadCompletionValidationStatus.UNAVAILABLE,
        ],
    )
    def test_a_non_passing_verdict_refuses(
        self, armed, status: TechLeadCompletionValidationStatus
    ) -> None:
        lane = _settle(
            armed,
            validator=StubTechLeadCompletionValidator(status=status, detail="nope"),
        )

        assert lane.rejection is not None
        assert lane.rejection.startswith(
            ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION
        )
        assert f"validation_{status.value}" in lane.rejection

    def test_no_wired_owner_refuses_rather_than_fail_safing(self, armed) -> None:
        lane = _settle(armed, validator=UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR)

        assert lane.rejection is not None
        assert "no trusted Tech Lead completion-validation owner" in lane.rejection

    def test_an_owner_that_raises_refuses(self, armed) -> None:
        lane = _settle(
            armed,
            validator=StubTechLeadCompletionValidator(
                raises=RuntimeError("store exploded")
            ),
        )

        assert lane.rejection is not None
        assert "validation_unavailable" in lane.rejection
        assert "store exploded" in lane.rejection

    def test_a_mis_wired_owner_still_crashes(self, armed) -> None:
        """Fail-closed is not fail-silent: a composition bug is not a verdict."""
        with pytest.raises(TypeError):
            _settle(
                armed,
                validator=StubTechLeadCompletionValidator(
                    raises=TypeError("validate_completion() got an unexpected kwarg")
                ),
            )

    def test_evidence_bound_to_another_candidate_refuses(self, armed) -> None:
        lane = _settle(
            armed,
            validator=StubTechLeadCompletionValidator(bind_to="f" * 40),
        )

        assert lane.rejection is not None
        assert "candidate_drift" in lane.rejection

    def test_an_unreadable_head_refuses_before_the_owner_is_asked(
        self, armed
    ) -> None:
        validator = passing_completion_validator()

        lane = _settle(
            armed, validator=validator, reader=FakeWorktreeReader(head=None)
        )

        assert lane.rejection is not None
        assert "candidate_unreadable" in lane.rejection
        assert validator.calls == []

    def test_the_requested_actions_are_handed_back_unshaped_on_refusal(
        self, armed
    ) -> None:
        """A refused completion settles nothing, so nothing is dropped for it."""
        lane = _settle(
            armed,
            validator=StubTechLeadCompletionValidator(
                status=TechLeadCompletionValidationStatus.FAILED, detail="dirty"
            ),
        )

        assert lane.requested_actions == COMPLETED_INTENTS
        assert lane.zero_code is False


class TestTheRefusalRoutesToTheTechLeadOwner:
    def test_the_prefix_is_recognised_as_a_tech_lead_failure(self, armed) -> None:
        lane = _settle(
            armed,
            validator=StubTechLeadCompletionValidator(
                status=TechLeadCompletionValidationStatus.FAILED, detail="dirty"
            ),
        )

        assert lane.rejection is not None
        assert has_tech_lead_refusal([lane.rejection])


class TestNonTechLeadCompletionsAreUntouched:
    @pytest.mark.parametrize(
        "outcome",
        [CompletionOutcome.BLOCKED, CompletionOutcome.NEEDS_HUMAN],
    )
    def test_a_non_completed_tech_lead_outcome_is_not_gated(
        self, armed, outcome: CompletionOutcome
    ) -> None:
        """Only a terminal settlement rests on the validation."""
        config, store, run_dir = armed
        validator = StubTechLeadCompletionValidator(
            status=TechLeadCompletionValidationStatus.UNAVAILABLE, detail="absent"
        )

        lane = settle_tech_lead_completion(
            config,
            tech_lead_authority=store,
            completion_validator=validator,
            run_dir=run_dir,
            run_id=RUN_ID,
            session_name=SESSION,
            outcome=outcome,
            requested_actions=COMPLETED_INTENTS,
            worktree=WORKTREE,
            worktree_reader=FakeWorktreeReader(),
        )

        assert lane.rejection is None
        assert validator.calls == []

    def test_an_actor_completion_never_reaches_the_owner(
        self, armed, tmp_path: Path
    ) -> None:
        """F8: a coder's completion path is byte-for-byte what it was."""
        config, store, run_dir = armed
        validator = StubTechLeadCompletionValidator(
            status=TechLeadCompletionValidationStatus.UNAVAILABLE, detail="absent"
        )
        record = CompletionRecord(
            session_id=SESSION,
            timestamp="2026-08-30T10:11:12+00:00",
            outcome=CompletionOutcome.COMPLETED,
            summary="did the work",
            requested_actions=list(COMPLETED_INTENTS),
        )

        outcome = settle_tech_lead_pre_action(
            config,
            tech_lead_authority=store,
            completion_validator=validator,
            worktree=WORKTREE,
            record=record,
            agent_label="agent:backend",
            issue_number=23,
            run_assets=make_session_run_assets(
                tmp_path, session_name=SESSION, run_id=RUN_ID
            ),
            worktree_reader=FakeWorktreeReader(),
        )

        assert outcome.refusal is None
        assert validator.calls == []
