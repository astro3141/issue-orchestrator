"""The handoff owner's explicit outcomes and its evidence copy (#297).

The end-to-end directions live in ``test_control_continuation.py``, where the
real durable stores drive ``reconcile``. What is proved here is the part a
caller of the owner sees directly: every exit produces a stated outcome
including the refusals, and the correction context is COPIED from durable
records rather than re-derived — a handoff that reported only its admissions
would be indistinguishable from the pre-#297 engine that reported nothing.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.continuation_live_truth import (
    CONTINUATION_KIND,
    ContinuationReworkExit,
)
from issue_orchestrator.control.continuation_rework_handoff import (
    ContinuationReworkHandoff,
    build_continuation_rework_feedback,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.rework_cycle_policy import (
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
)
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.continuation_descriptor import ContinuationDescriptor
from issue_orchestrator.domain.continuation_phase import ContinuationPhase
from issue_orchestrator.domain.control_operation import ControlOperationKey
from issue_orchestrator.domain.models import (
    DiscoveredEscalation,
    DiscoveredRework,
    Issue,
    OrchestratorState,
    RequestedAction,
)
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo

REPO = "owner/repo"
ISSUE_NUMBER = 297
SHA = "c" * 40
PUBLISH_COMMAND = "make validate-pr-raw"
PROFILE = "default"
RECORD_PATH = ".issue-orchestrator/sessions/run-7/validation-record.json"


class StubPullRequests:
    """The PR port, answering with whatever the direction under test needs."""

    def __init__(self, prs: dict[int, PRInfo] | None = None) -> None:
        self.prs = prs if prs is not None else {}

    def create_pr(self, *args: object, **kwargs: object) -> PRInfo:
        raise AssertionError("the handoff must not create pull requests")

    def get_prs_for_issue(
        self, issue_number: int, state: str = "open"
    ) -> list[PRInfo]:
        pr = self.prs.get(issue_number)
        return [pr] if pr is not None else []

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        raise AssertionError("the handoff must not scan branches")


class UnreachablePullRequests(StubPullRequests):
    """A PR port whose being reached at all is the failure."""

    def get_prs_for_issue(
        self, issue_number: int, state: str = "open"
    ) -> list[PRInfo]:
        raise AssertionError("the handoff must refuse before reading GitHub")


def _issue(*labels: str) -> Issue:
    return Issue(
        number=ISSUE_NUMBER,
        title="the candidate under correction",
        labels=list(labels) or ["agent:backend"],
        repo=REPO,
    )


def _pr(*, labels: list[str] | None = None) -> PRInfo:
    return PRInfo(
        number=294,
        title=f"#{ISSUE_NUMBER}: the candidate under correction",
        url=f"https://example.test/{REPO}/pull/294",
        branch=f"{ISSUE_NUMBER}-continuation-lineage",
        body="",
        state="open",
        labels=labels if labels is not None else [],
    )


def _attempt(*, with_descriptor: bool = True, record: str | None = RECORD_PATH) -> Attempt:
    issue = _issue()
    attempt = Attempt(
        key=AttemptKey(issue.key, SHA),
        validation_record_path=record,
        completed_evaluations=(
            ValidationVerdictReceipt(
                suite=ValidationGateKind.PUBLISH.suite,
                head_sha=SHA,
                verdict=ValidationVerdict.FAILED,
                command=PUBLISH_COMMAND,
                profile=PROFILE,
            ),
        ),
        revalidation_budget_used=1,
    )
    if not with_descriptor:
        return attempt
    return attempt.with_continuation_descriptor(
        ContinuationDescriptor(
            requested_actions=(RequestedAction.CREATE_PR,),
            implementation="what the agent claimed to build",
            problems="the publish gate refused it",
            suite=ValidationGateKind.PUBLISH.suite,
            command=PUBLISH_COMMAND,
            profile=PROFILE,
        )
    )


def _exit(issue: Issue, attempt: Attempt) -> ContinuationReworkExit:
    return ContinuationReworkExit(
        key=ControlOperationKey(issue.key, SHA, CONTINUATION_KIND),
        issue=issue,
        attempt=attempt,
        phase=ContinuationPhase.EXHAUSTED,
    )


def _handoff(
    state: OrchestratorState, pull_requests: StubPullRequests
) -> ContinuationReworkHandoff:
    config = Config()
    return ContinuationReworkHandoff(
        state=state,
        pull_requests=pull_requests,  # type: ignore[arg-type]
        budget=ReworkCycleBudget(
            LabelManager(config), max_rework_cycles=config.max_rework_cycles
        ),
    )


class TestEveryExitProducesAStatedOutcome:
    def test_an_admitted_exit_reports_the_pr_and_the_cycle(self) -> None:
        state = OrchestratorState()
        handoff = _handoff(state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert len(result.outcomes) == 1
        outcome = result.outcomes[0]
        assert outcome.verdict is ReworkAdmissionVerdict.QUEUE
        assert outcome.pr_number == 294
        assert outcome.rework_cycle == 1
        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)

    def test_a_candidate_with_no_open_pr_says_so(self) -> None:
        state = OrchestratorState()
        handoff = _handoff(state, StubPullRequests())

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "no_open_pr"

    def test_a_candidate_with_no_agent_label_says_so(self) -> None:
        state = OrchestratorState()
        handoff = _handoff(state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue("some-other-label"), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "no_agent_label"

    def test_an_escalated_exit_reports_the_cycle_it_could_not_take(self) -> None:
        state = OrchestratorState()
        config = Config()
        handoff = _handoff(
            state,
            StubPullRequests(
                {ISSUE_NUMBER: _pr(labels=[f"rework-cycle-{config.max_rework_cycles}"])}
            ),
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert len(result.escalations) == 1
        assert result.outcomes[0].verdict is ReworkAdmissionVerdict.ESCALATE
        assert result.outcomes[0].rework_cycle == config.max_rework_cycles + 1

    def test_no_exits_asks_nothing_of_anybody(self) -> None:
        handoff = _handoff(OrchestratorState(), UnreachablePullRequests())

        result = handoff.admit([])

        assert result.outcomes == ()
        assert result.reworks == ()
        assert result.escalations == ()

    def test_a_held_issue_refuses_before_github_is_reached(self) -> None:
        state = OrchestratorState()
        handoff = _handoff(state, UnreachablePullRequests())
        assert state.record_discovered_rework(
            DiscoveredRework(
                issue_number=ISSUE_NUMBER,
                pr_number=294,
                branch_name=f"{ISSUE_NUMBER}-continuation-lineage",
                agent_type="agent:backend",
            )
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "already_queued"


class TestTheFactBufferOwnsItsOwnAdmission:
    """The write is the refusal that cannot be skipped.

    The budget's ``already_held`` refuses earlier and more cheaply, but it is a
    read: two producers deciding from the same snapshot would both pass it. The
    collection's own rule is what makes a second file impossible.
    """

    def test_one_rework_per_issue_per_tick(self) -> None:
        state = OrchestratorState()
        first = DiscoveredRework(
            issue_number=ISSUE_NUMBER,
            pr_number=294,
            branch_name=f"{ISSUE_NUMBER}-continuation-lineage",
            agent_type="agent:backend",
        )

        assert state.record_discovered_rework(first) is True
        assert state.record_discovered_rework(first) is False
        assert state.discovered_reworks == [first]

    def test_one_escalation_per_issue_per_tick(self) -> None:
        state = OrchestratorState()
        first = DiscoveredEscalation(
            issue_number=ISSUE_NUMBER, pr_number=294, rework_cycle=6
        )

        assert state.record_discovered_escalation(first) is True
        assert state.record_discovered_escalation(first) is False
        assert state.discovered_escalations == [first]

    def test_a_different_issue_is_not_blocked_by_another(self) -> None:
        state = OrchestratorState()
        state.record_discovered_rework(
            DiscoveredRework(
                issue_number=ISSUE_NUMBER,
                pr_number=294,
                branch_name="a",
                agent_type="agent:backend",
            )
        )

        assert state.record_discovered_rework(
            DiscoveredRework(
                issue_number=ISSUE_NUMBER + 1,
                pr_number=295,
                branch_name="b",
                agent_type="agent:backend",
            )
        )
        assert len(state.discovered_reworks) == 2


class TestTheEvidenceIsCopiedNotDerived:
    def test_every_correction_fact_appears_verbatim(self) -> None:
        pr = _pr()
        attempt = _attempt()

        feedback = build_continuation_rework_feedback(
            pr=pr, attempt=attempt, phase_reason=ContinuationPhase.EXHAUSTED.value
        )

        assert SHA in feedback
        assert PUBLISH_COMMAND in feedback
        assert ValidationVerdict.FAILED.value in feedback
        assert RECORD_PATH in feedback
        assert pr.url in feedback
        assert pr.branch in feedback
        assert "what the agent claimed to build" in feedback
        assert "the publish gate refused it" in feedback
        assert ContinuationPhase.EXHAUSTED.value in feedback

    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [
            (
                _attempt(record=None),
                "no validation record path was recorded",
            ),
            (
                _attempt(with_descriptor=False),
                "what the agent claimed to build",
            ),
        ],
    )
    def test_absent_evidence_is_never_invented(
        self, attempt: Attempt, expected: str
    ) -> None:
        feedback = build_continuation_rework_feedback(
            pr=_pr(), attempt=attempt, phase_reason="exhausted"
        )

        if expected.startswith("no "):
            assert expected in feedback
        else:
            assert expected not in feedback

    def test_a_candidate_with_no_recorded_verdict_says_so(self) -> None:
        issue = _issue()
        bare = Attempt(key=AttemptKey(issue.key, SHA))

        feedback = build_continuation_rework_feedback(
            pr=_pr(), attempt=bare, phase_reason="exhausted"
        )

        assert "no verdict was recorded" in feedback

    def test_the_corrector_is_told_not_to_trust_the_failed_commit(self) -> None:
        feedback = build_continuation_rework_feedback(
            pr=_pr(), attempt=_attempt(), phase_reason="exhausted"
        )

        assert "Do not treat the failed commit above as validated." in feedback
