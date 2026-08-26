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
    CONTINUATION_EXIT_SOURCE,
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
from issue_orchestrator.events import EventName
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


class CountingPullRequests(StubPullRequests):
    """A PR port that counts reads, so a repeated search is a test failure."""

    def __init__(self, prs: dict[int, PRInfo] | None = None) -> None:
        super().__init__(prs)
        self.reads = 0

    def get_prs_for_issue(
        self, issue_number: int, state: str = "open"
    ) -> list[PRInfo]:
        self.reads += 1
        return super().get_prs_for_issue(issue_number, state)


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


def _attempt(
    *,
    with_descriptor: bool = True,
    record: str | None = RECORD_PATH,
    sha: str = SHA,
) -> Attempt:
    issue = _issue()
    attempt = Attempt(
        key=AttemptKey(issue.key, sha),
        validation_record_path=record,
        completed_evaluations=(
            ValidationVerdictReceipt(
                suite=ValidationGateKind.PUBLISH.suite,
                head_sha=sha,
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
        key=ControlOperationKey(issue.key, attempt.key.head_sha, CONTINUATION_KIND),
        issue=issue,
        attempt=attempt,
        phase=ContinuationPhase.EXHAUSTED,
    )


def _handoff_fact(feedback: str = "why the publication failed") -> DiscoveredRework:
    """The kind of fact this producer files: it carries correction context."""
    return DiscoveredRework(
        issue_number=ISSUE_NUMBER,
        pr_number=294,
        branch_name=f"{ISSUE_NUMBER}-continuation-lineage",
        agent_type="agent:backend",
        source=CONTINUATION_EXIT_SOURCE,
        feedback=feedback,
    )


def _sweep_fact() -> DiscoveredRework:
    """The kind the ``needs-rework`` sweep files: the label, and nothing else."""
    return DiscoveredRework(
        issue_number=ISSUE_NUMBER,
        pr_number=294,
        branch_name="",
        agent_type="agent:backend",
        source="",
    )


class RecordingEvents:
    """The sink, kept so a test can assert what the UI would have seen."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event: object) -> None:
        self.published.append(
            (event.event_type.value, dict(event.data))  # type: ignore[attr-defined]
        )

    def reasons(self) -> list[str]:
        return [str(data.get("reason")) for _, data in self.published]


def _handoff(
    state: OrchestratorState,
    pull_requests: StubPullRequests,
    events: RecordingEvents | None = None,
) -> ContinuationReworkHandoff:
    config = Config()
    return ContinuationReworkHandoff(
        state=state,
        pull_requests=pull_requests,  # type: ignore[arg-type]
        budget=ReworkCycleBudget(
            LabelManager(config), max_rework_cycles=config.max_rework_cycles
        ),
        events=events if events is not None else RecordingEvents(),  # type: ignore[arg-type]
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
        assert state.record_discovered_rework(_handoff_fact())

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "already_queued"


class TestNoRefusalCostsAGitHubRead:
    """F1: every refusal the exit's own facts can settle is settled for free.

    The board issue arrives with the exit, so its blocking labels and its agent
    label cost nothing to consult. Reaching the PR port to answer from them
    would be one search-API call per reconciliation, forever, for a candidate
    that is refused every time.
    """

    def test_a_blocked_issue_refuses_before_the_pr_is_read(self) -> None:
        handoff = _handoff(OrchestratorState(), UnreachablePullRequests())

        result = handoff.admit(
            [_exit(_issue("agent:backend", "needs-human"), _attempt())]
        )

        assert result.reworks == ()
        assert result.outcomes[0].reason == "issue_blocked"

    def test_a_missing_agent_label_refuses_before_the_pr_is_read(self) -> None:
        handoff = _handoff(OrchestratorState(), UnreachablePullRequests())

        result = handoff.admit([_exit(_issue("some-other-label"), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "no_agent_label"

    def test_a_settled_absence_of_a_pr_is_not_searched_for_again(self) -> None:
        pull_requests = CountingPullRequests()
        handoff = _handoff(OrchestratorState(), pull_requests)
        exit_ = _exit(_issue(), _attempt())

        for _ in range(4):
            result = handoff.admit([exit_])
            assert result.outcomes[0].reason == "no_open_pr"

        assert pull_requests.reads == 1

    def test_a_newer_candidate_is_searched_for_afresh(self) -> None:
        pull_requests = CountingPullRequests()
        handoff = _handoff(OrchestratorState(), pull_requests)

        handoff.admit([_exit(_issue(), _attempt())])
        handoff.admit([_exit(_issue(), _attempt(sha="d" * 40))])

        assert pull_requests.reads == 2

    def test_an_exit_that_stops_being_derived_is_forgotten(self) -> None:
        pull_requests = CountingPullRequests()
        handoff = _handoff(OrchestratorState(), pull_requests)
        exit_ = _exit(_issue(), _attempt())

        handoff.admit([exit_])
        handoff.admit([])  # the durable facts changed; nothing exits this pass
        handoff.admit([exit_])

        assert pull_requests.reads == 2


class TestARefusalThatStrandsACandidateIsPublished:
    """N1: the two refusals nothing downstream retries reach the UI as events.

    A candidate refused for ``no_open_pr`` or ``no_agent_label`` sits until a
    human looks at it. Per this repo's events-vs-logs rule a UI may not read the
    log line that says so, so these are published.
    """

    def test_a_stranded_candidate_publishes_its_reason(self) -> None:
        events = RecordingEvents()
        handoff = _handoff(OrchestratorState(), StubPullRequests(), events)

        handoff.admit([_exit(_issue(), _attempt())])

        assert events.published == [
            (
                EventName.REWORK_SKIPPED.value,
                {
                    "reason": "no_open_pr",
                    "issue_number": ISSUE_NUMBER,
                    "source": CONTINUATION_EXIT_SOURCE,
                },
            )
        ]

    def test_a_candidate_with_no_agent_label_publishes_too(self) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            OrchestratorState(), StubPullRequests({ISSUE_NUMBER: _pr()}), events
        )

        handoff.admit([_exit(_issue("some-other-label"), _attempt())])

        assert events.reasons() == ["no_agent_label"]

    def test_an_admitted_exit_publishes_no_refusal(self) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            OrchestratorState(), StubPullRequests({ISSUE_NUMBER: _pr()}), events
        )

        handoff.admit([_exit(_issue(), _attempt())])

        assert events.published == []


class TestTheFactBufferOwnsItsOwnAdmission:
    """The write is the refusal that cannot be skipped.

    The budget's ``already_held`` refuses earlier and more cheaply, but it is a
    read: two producers deciding from the same snapshot would both pass it. The
    collection's own rule is what makes a second file impossible.
    """

    def test_one_rework_per_issue_per_tick(self) -> None:
        state = OrchestratorState()
        first = _handoff_fact()

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


class TestTheCorrectionContextSurvivesEitherOrdering:
    """A1: which producer ran first must not decide which fact survives.

    The steady-state refresh sweeps ``needs-rework`` PRs before it hydrates the
    queue; startup hydrates before it sweeps. So both orderings happen in
    production, and the fact carrying the publication failure's evidence has to
    win in both.
    """

    def test_a_context_free_fact_is_superseded_by_one_that_has_context(
        self,
    ) -> None:
        state = OrchestratorState()
        assert state.record_discovered_rework(_sweep_fact()) is True

        assert state.record_discovered_rework(_handoff_fact()) is True

        assert state.discovered_reworks == [_handoff_fact()]

    def test_a_fact_with_context_is_never_replaced_by_one_without(self) -> None:
        state = OrchestratorState()
        assert state.record_discovered_rework(_handoff_fact()) is True

        assert state.record_discovered_rework(_sweep_fact()) is False

        assert state.discovered_reworks == [_handoff_fact()]

    def test_the_sweeps_fact_does_not_block_the_handoff_from_filing(self) -> None:
        state = OrchestratorState()
        state.record_discovered_rework(_sweep_fact())
        handoff = _handoff(state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)
        assert len(state.discovered_reworks) == 1
        assert state.discovered_reworks[0].source == CONTINUATION_EXIT_SOURCE

    def test_the_handoffs_own_fact_still_blocks_a_second_pass(self) -> None:
        state = OrchestratorState()
        handoff = _handoff(state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        handoff.admit([_exit(_issue(), _attempt())])
        second = handoff.admit([_exit(_issue(), _attempt())])

        assert second.reworks == ()
        assert second.outcomes[0].reason == "already_queued"
        assert len(state.discovered_reworks) == 1

    def test_an_escalation_is_a_claim_no_later_fact_may_supersede(self) -> None:
        state = OrchestratorState()
        state.record_discovered_escalation(
            DiscoveredEscalation(issue_number=ISSUE_NUMBER, pr_number=294, rework_cycle=6)
        )
        handoff = _handoff(state, UnreachablePullRequests())

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "already_queued"


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
