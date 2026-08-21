"""Every launch flavor defers while the callback endpoint is unresolved.

An agent spawned before the endpoint is published gets a completion
environment with no way to reach the orchestrator: every callback it
makes fails, and the round runner reads the resulting silence as an
unresponsive agent — which is how completed, validated work ended up
stranded (#6913, #6924).

The rule first lived only in ``_check_launch_preconditions``, which
review, retrospective-review and rework launches never reach, so those
three flavors could still launch into the unresolved window (F7-R3).
These tests pin the behaviour per path — that the launcher actually
defers — not merely that the endpoint object reports unready.

They also pin what the deferral MEANS for the queue that asked for the
launch. The refusal returned no disposition, so ``LaunchResult``'s
``PERMANENT_FAILURE`` default applied and the settlement read a
transient, self-closing window as "the launcher gave up": the queued
item was dropped and its durable claim retired, which for a tech-lead
investigation is the only record there is (#193).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.session_launcher import SessionLauncher
from issue_orchestrator.control.session_routing import (
    orchestrator_launch_tech_lead_session,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.config import AgentConfig, Config
from issue_orchestrator.ports import NullBoardSnapshotProvider
from tests.unit.publication_evidence_helpers import verdict_with_no_evidence

DEFER_REASON = "Agent callback endpoint not published yet"
TECH_LEAD_AGENT = "agent:tech-lead"


def _launcher(endpoint, config: Config | None = None) -> SessionLauncher:
    return SessionLauncher(
        config=config or Config(),
        events=MagicMock(),
        repository_host=MagicMock(),
        action_applier=MagicMock(),
        session_manager=MagicMock(),
        worktree_manager=MagicMock(),
        working_copy=MagicMock(),
        command_runner=MagicMock(),
        session_output=MagicMock(),
        manifest_downloader=MagicMock(),
        tech_lead_authority=MagicMock(),
        session_exists_fn=lambda name: False,
        create_session_fn=lambda *args: True,
        get_issue_machine=lambda issue: MagicMock(state="AVAILABLE"),
        get_session_machine=lambda *args: MagicMock(),
        get_review_machine=lambda *args: MagicMock(),
        board_snapshot_provider=NullBoardSnapshotProvider(),
        agent_callback_endpoint=endpoint,
        publication_verdict=verdict_with_no_evidence(),
    )


@pytest.fixture
def unresolved_endpoint() -> RuntimeAgentCallbackEndpoint:
    """Neither published nor declared — the window the gate guards."""
    return RuntimeAgentCallbackEndpoint()


def _pending(flavor: str):
    """Concrete domain values, not mocks.

    Mock-shaped input lets a launcher fail for unrelated reasons and
    still satisfy an assertion about which error string is absent, which
    is what made the earlier version of the "proceeds" test hollow (N5).
    """
    if flavor == "review":
        return PendingReview(
            issue_key=FakeIssueKey(name="4058"),
            pr_number=99,
            pr_url="https://github.com/owner/repo/pull/99",
            branch_name="4058-fix",
            _issue_number=4058,
        )
    if flavor == "retrospective_review":
        return PendingRetrospectiveReview(
            issue_key=FakeIssueKey(name="4058"),
            issue_number=4058,
            issue_title="Already finished",
            agent_label="agent:backend",
            trigger_label="lack-of-review-redo",
        )
    return PendingRework(
        issue_key=FakeIssueKey(name="4058"),
        agent_type="agent:backend",
        issue_number=4058,
        pr_number=99,
    )


@pytest.mark.parametrize(
    "flavor",
    ["review", "retrospective_review", "rework"],
)
def test_flavor_defers_while_endpoint_unresolved(
    unresolved_endpoint: RuntimeAgentCallbackEndpoint, flavor: str
) -> None:
    """Each flavor that bypassed the precondition helper now defers."""
    launcher = _launcher(unresolved_endpoint)
    call = {
        "review": launcher.launch_review_session,
        "retrospective_review": launcher.launch_retrospective_review_session,
        "rework": launcher.launch_rework_session,
    }[flavor]

    result = call(_pending(flavor), [])

    assert result.session is None, f"{flavor} launched with no callback endpoint"
    assert result.success is False
    assert DEFER_REASON in (result.reason or ""), result.reason


@pytest.mark.parametrize(
    "flavor",
    ["review", "retrospective_review", "rework"],
)
def test_flavor_proceeds_past_the_gate_once_resolved(
    unresolved_endpoint: RuntimeAgentCallbackEndpoint, flavor: str
) -> None:
    """The gate must not be a permanent block.

    Asserting only that one error string is absent would pass even if
    the launcher blew up on mock-shaped data, so this pins the *next*
    boundary each flavor reaches: with no reviewer configured, review
    and retrospective-review must refuse for that reason, and rework
    must get past the gate to its own agent-config check.
    """
    unresolved_endpoint.declare_unavailable()
    launcher = _launcher(unresolved_endpoint)
    call = {
        "review": launcher.launch_review_session,
        "retrospective_review": launcher.launch_retrospective_review_session,
        "rework": launcher.launch_rework_session,
    }[flavor]

    result = call(_pending(flavor), [])

    assert DEFER_REASON not in (result.reason or ""), (
        f"{flavor} still blocked on readiness after the endpoint resolved"
    )
    assert result.success is False
    expected = {
        "review": "No code review agent configured",
        "retrospective_review": "No code review agent configured",
        "rework": "No agent config",
    }[flavor]
    assert expected in (result.reason or ""), (
        f"{flavor} did not reach its next boundary; got {result.reason!r}"
    )


class TestTheDeferralRetainsTheQueuedWork:
    """The queue owner must treat this refusal as retryable, not as a drop.

    Driven through the production routing function that owns the pending
    tech-lead queue, so the assertion is about what the SETTLEMENT did —
    the disposition is not readable from outside, but the queue and the
    durable claim it settles are.
    """

    def _config(self, tmp_path: Path) -> Config:
        prompt = tmp_path / "tech-lead.md"
        prompt.write_text("Investigate")
        config = Config(repo="owner/repo", repo_root=tmp_path)
        config.tech_lead_review_agent = TECH_LEAD_AGENT
        config.agents = {
            TECH_LEAD_AGENT: AgentConfig(
                prompt_path=prompt, provider="claude-code", model="sonnet"
            )
        }
        return config

    def _queued_investigation(self) -> PendingTechLeadReview:
        return PendingTechLeadReview(
            issue_number=23,
            title="Investigate: session failed",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                23, "Stuck issue", "timed_out", blocking_label="blocked-failed"
            ),
        )

    def _route(
        self,
        unresolved_endpoint: RuntimeAgentCallbackEndpoint,
        tmp_path: Path,
    ) -> tuple[OrchestratorState, object]:
        state = OrchestratorState()
        state.pending_tech_lead_reviews.append(self._queued_investigation())
        config = self._config(tmp_path)
        launcher = _launcher(unresolved_endpoint, config)
        session = orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0],
            state,
            config,
            launcher,
            MagicMock(restore_session=MagicMock(return_value=None)),
            SqlitePendingWorkClaimStore.for_repo(tmp_path),
        )
        return state, session

    def test_the_queued_investigation_survives_the_deferral(
        self, unresolved_endpoint: RuntimeAgentCallbackEndpoint, tmp_path: Path
    ) -> None:
        """Dropping it would destroy the only record of the investigation."""
        state, session = self._route(unresolved_endpoint, tmp_path)

        assert session is None
        assert len(state.pending_tech_lead_reviews) == 1, (
            "the deferral dropped work over a window that had not closed yet"
        )
        assert state.active_sessions == []

    def test_the_retained_item_keeps_its_full_retry_budget(
        self, unresolved_endpoint: RuntimeAgentCallbackEndpoint, tmp_path: Path
    ) -> None:
        """The gate refuses before the durable claim is ever held.

        So the queue owner's bounded budget has no deferred row to spend
        against, and the settlement declines to spend one in memory
        (#6999 F1 round 2). The item waits with its budget intact — the
        endpoint resolving on a later tick must not cost it an attempt.
        """
        state, _ = self._route(unresolved_endpoint, tmp_path)

        assert state.pending_tech_lead_reviews[0].retryable_launch_failures == 0

    def test_the_refusal_reason_is_logged_rather_than_swallowed(
        self,
        unresolved_endpoint: RuntimeAgentCallbackEndpoint,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An operator must be able to see WHY nothing launched.

        The pilot run showed queue bookkeeping and no reason at all, so
        the refusal's own string had to be read out of the source (#193).
        """
        with caplog.at_level(logging.WARNING):
            self._route(unresolved_endpoint, tmp_path)

        assert DEFER_REASON in caplog.text, caplog.text

    def test_a_resolved_endpoint_lets_the_issue_path_past_the_gate(
        self, unresolved_endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """Retention must not mean a queue that can never drain.

        Pins the *next* boundary the issue-session path reaches once the
        endpoint has answered — an issue with no agent-type label — so a
        gate that silently kept refusing could not pass this.
        """
        unresolved_endpoint.declare_unavailable()
        launcher = _launcher(unresolved_endpoint)

        result = launcher.launch_issue_session(Issue(23, "Unlabelled", []), [])

        assert DEFER_REASON not in (result.reason or ""), result.reason
        assert "has no agent type label" in (result.reason or ""), result.reason
