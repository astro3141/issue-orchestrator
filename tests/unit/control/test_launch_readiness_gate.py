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

They also pin what a deferral MEANS for the request that asked for it
(#193). The refusal used to carry no disposition, so ``LaunchResult``'s
``PERMANENT_FAILURE`` default applied and the settlement dropped the
pending item and retired its durable claim — an unrecoverable outcome
for a deferral whose whole premise is that the next tick works. The
second class below drives the real routing owner and asserts retention,
a spent-but-bounded budget, and a later launch once the endpoint is
answered.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.ports.planning_command_guard import (
    UNGUARDED_PLANNING_COMMAND_GUARD,
)
from issue_orchestrator.control.session_launcher import SessionLauncher
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullBoardSnapshotProvider
from tests.unit.publication_evidence_helpers import verdict_with_no_evidence

DEFER_REASON = "Agent callback endpoint not published yet"


def _launcher(endpoint) -> SessionLauncher:
    return SessionLauncher(
        config=Config(),
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
        planning_command_guard=UNGUARDED_PLANNING_COMMAND_GUARD,
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


# ---------------------------------------------------------------------------
# What a deferral MEANS for the request that asked for it (#193)
# ---------------------------------------------------------------------------


def _tech_lead_config(tmp_path):
    from issue_orchestrator.infra.config import AgentConfig

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    config = Config(repo="test/repo", repo_root=tmp_path, max_concurrent_sessions=4)
    config.agents = {
        label: AgentConfig(prompt_path=prompt, provider="claude-code")
        for label in ("agent:backend", "agent:tech-lead")
    }
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead.max_concurrent = 1
    return config


class _RealLauncher:
    """A real ``SessionLauncher`` whose callback endpoint the test owns.

    Everything below the gate is a port-level mock, but the launcher, the
    routing owner, the pending queue and the durable claim store are the
    production ones — the settlement is precisely what is under test, and a
    stubbed launcher would let the wrong disposition pass unnoticed.
    """

    def __init__(self, tmp_path, endpoint) -> None:
        from issue_orchestrator.domain.state_machines.issue_machine import (
            IssueStateMachine,
        )
        from issue_orchestrator.domain.state_machines.review_machine import (
            ReviewStateMachine,
        )
        from issue_orchestrator.domain.state_machines.session_machine import (
            SessionStateMachine,
        )
        from issue_orchestrator.execution.pending_work_claim_store import (
            SqlitePendingWorkClaimStore,
        )
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )
        from issue_orchestrator.ports import NullManifestDownloader
        from tests.unit.test_session_launcher import (
            MockCommandRunner,
            MockEventSink,
            MockRepositoryHost,
            MockWorkingCopy,
            MockWorktreeManager,
        )

        self.config = _tech_lead_config(tmp_path)
        self.created: list[str] = []
        self.claims = SqlitePendingWorkClaimStore.for_repo(tmp_path)
        self.launcher = SessionLauncher(
            config=self.config,
            events=MockEventSink(),
            repository_host=MockRepositoryHost(),
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=MockWorktreeManager(tmp_path),
            working_copy=MockWorkingCopy(),
            command_runner=MockCommandRunner(),
            session_output=FileSystemSessionOutput(),
            manifest_downloader=NullManifestDownloader(),
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(tmp_path),
            session_exists_fn=lambda name: False,
            create_session_fn=(
                lambda name, cmd, wd, title: self.created.append(name) or True
            ),
            get_issue_machine=lambda issue: IssueStateMachine(issue),
            get_session_machine=lambda name, n, timeout: SessionStateMachine(
                name, n, timeout_minutes=timeout
            ),
            get_review_machine=lambda pr, issue: ReviewStateMachine(pr, issue),
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=endpoint,
            publication_verdict=verdict_with_no_evidence(),
            planning_command_guard=UNGUARDED_PLANNING_COMMAND_GUARD,
        )


def _queued_investigation():
    """One queued failure investigation — the only record of itself."""
    from issue_orchestrator.control.pending_session_queues import PendingSessionQueues
    from issue_orchestrator.domain.models import DiscoveredFailure, OrchestratorState

    state = OrchestratorState()
    PendingSessionQueues(state).queue_failure_investigation(
        7,
        "Investigate: session failed",
        failure=DiscoveredFailure(
            issue_number=7, issue_title="Subject", failure_reason="timed_out",
            blocking_label="blocked-failed",
        ),
    )
    return state


def _route_tech_lead(state, harness):
    from issue_orchestrator.control import session_routing

    restorer = MagicMock()
    restorer.restore_session.return_value = None
    return session_routing.orchestrator_launch_tech_lead_session(
        state.pending_tech_lead_reviews[0],
        state,
        harness.config,
        harness.launcher,
        restorer,
        harness.claims,
    )


class TestADeferralKeepsTheWorkItDeferred:
    """A deferral that destroys the request is not a deferral (#193).

    With no disposition the refusal inherited ``PERMANENT_FAILURE``, so the
    settlement took its destructive branch: the queue item was dropped and the
    durable claim retired, which for a failure investigation is the only record
    that exists. The next tick — the one the docstring promises will launch —
    then had nothing left to launch.
    """

    def test_the_queued_investigation_survives_the_deferral(
        self, unresolved_endpoint, tmp_path
    ) -> None:
        harness = _RealLauncher(tmp_path, unresolved_endpoint)
        state = _queued_investigation()

        session = _route_tech_lead(state, harness)

        assert session is None
        assert harness.created == []  # nothing spawned into a callback-less env
        assert len(state.pending_tech_lead_reviews) == 1

    def test_the_queue_owner_handles_the_retryable_refusal_as_designed(
        self, unresolved_endpoint, tmp_path
    ) -> None:
        """Retained with its budget intact, and that is the DESIGNED answer.

        The gate refuses in Phase 1, before the launch holds its durable claim,
        so there is no deferred row for a spend to be written against. The
        settlement's ledger-honesty rule (#6999 F1 round 2) therefore declines
        to project a spend nothing durable would remember, and settles the
        request as UNRECORDED — the same answer the other pre-claim retryable
        refusal (``required_input_unavailable``) already gets.

        Ten deferrals must therefore cost the investigation nothing: the
        budget exists for attempts that actually failed against a recorded
        request, and an endpoint race is not one.
        """
        from issue_orchestrator.control.pending_session_queues import (
            PendingSessionQueues,
            TechLeadRetentionOutcome,
        )

        harness = _RealLauncher(tmp_path, unresolved_endpoint)
        state = _queued_investigation()

        for _ in range(10):
            _route_tech_lead(state, harness)

        assert len(state.pending_tech_lead_reviews) == 1
        assert state.pending_tech_lead_reviews[0].retryable_launch_failures == 0
        # Intact rather than merely unread: the first REAL launch failure is
        # still the first one counted, and it is retained.
        queues = PendingSessionQueues(state)
        spend = queues.plan_tech_lead_retry(7)
        assert spend.outcome is TechLeadRetentionOutcome.RETAINED

    def test_a_later_tick_launches_once_the_endpoint_is_answered(
        self, unresolved_endpoint, tmp_path
    ) -> None:
        """The whole promise of the deferral, end to end.

        The endpoint is answered exactly as a one-shot ``tech_lead`` command
        answers it — ``declare_unavailable``, no server bound — and the same
        queued investigation then launches a real terminal. No second
        long-lived engine, and no Control API, is involved.
        """
        harness = _RealLauncher(tmp_path, unresolved_endpoint)
        state = _queued_investigation()
        _route_tech_lead(state, harness)

        unresolved_endpoint.declare_unavailable()
        session = _route_tech_lead(state, harness)

        assert session is not None
        assert harness.created, "no terminal was spawned once the endpoint resolved"
        assert state.pending_tech_lead_reviews == []

    def test_the_refusal_names_its_reason(
        self, unresolved_endpoint, tmp_path, caplog
    ) -> None:
        """Which precondition refused must reach an operator.

        The observed failure printed "Session starting" and then "launch
        declined" with nothing between them: the reason string lived only on a
        ``LaunchResult`` that the destructive settlement discarded.
        """
        harness = _RealLauncher(tmp_path, unresolved_endpoint)
        state = _queued_investigation()

        with caplog.at_level("WARNING"):
            _route_tech_lead(state, harness)

        assert any(DEFER_REASON in record.getMessage() for record in caplog.records), (
            f"deferral reason never logged; got {[r.getMessage() for r in caplog.records]}"
        )
