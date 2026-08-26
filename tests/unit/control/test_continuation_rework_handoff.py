"""The handoff owner's explicit outcomes and its evidence copy (#297).

The end-to-end directions live in ``test_control_continuation.py``, where the
real durable stores drive ``reconcile``. What is proved here is the part a
caller of the owner sees directly: every exit produces a stated outcome
including the refusals, and the correction context is COPIED from durable
records rather than re-derived — a handoff that reported only its admissions
would be indistinguishable from the pre-#297 engine that reported nothing.

The evidence half is proved against the REAL #94 store on a real filesystem,
never a double. Acceptance 3 requires that a rework launched after the failed
candidate's worktree is gone still receive the failing output, and a double
standing in for the store would prove only that this module can be handed a
value — which is exactly what a session-local ``record_path`` also does, right
up to the moment somebody tries to read it.

The last class is #304's, and it is about the question this owner must ask
BEFORE any of the above: whether the engine is allowed to act on the issue the
exit names at all. The end-to-end proof of that lives in
``test_control_continuation.py`` too, for the same reason — only there does a
real board-wide reconciliation put a foreign issue in front of the handoff.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from issue_orchestrator.adapters.github.errors import GitHubTransportError
from issue_orchestrator.control.continuation_live_truth import (
    CONTINUATION_KIND,
    ContinuationReworkExit,
)
from issue_orchestrator.control.continuation_rework_feedback import (
    build_continuation_rework_feedback,
)
from issue_orchestrator.control.continuation_rework_handoff import (
    CONTINUATION_EXIT_SOURCE,
    ContinuationReworkHandoff,
)
from issue_orchestrator.control.gate_failure_diagnostics import (
    FAILURE_LOG_TAIL_BYTES,
    STDERR_FILE_NAME,
    STDOUT_FILE_NAME,
    DurableGateFailure,
    GateFailureDiagnostics,
    GateFailureOutput,
)
from issue_orchestrator.control.issue_scope import EngineIssueScope
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.rework_cycle_policy import (
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
)
from issue_orchestrator.control.validation import VALIDATION_SCHEMA_VERSION
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
from issue_orchestrator.ports.session_output import ValidationRecord

REPO = "owner/repo"
ISSUE_NUMBER = 297
SHA = "c" * 40
OTHER_SHA = "d" * 40
PUBLISH_COMMAND = "make validate-pr-raw"
PROFILE = "default"
PUBLISH_SUITE = ValidationGateKind.PUBLISH.suite
QUICK_SUITE = ValidationGateKind.QUICK.suite

#: The session-local pointer the attempt carries. It lives inside the CODER's
#: worktree, which is what makes it worthless once the worktree is reaped.
RUN_RECORD_RELATIVE = ".issue-orchestrator/sessions/run-7/validation-record.json"

#: The failing output a correction agent has to be handed. Two streams, because
#: a failure explained only by its stdout is one whose traceback was elsewhere.
FAILING_STDOUT = "FAILED tests/unit/test_widget.py::test_it - AssertionError: boom"
FAILING_STDERR = "make: *** [validate-pr-raw] Error 1"


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


class RateLimitedPullRequests(CountingPullRequests):
    """A PR port that refuses to answer, exactly as a live outage does.

    The real transport error, not a stand-in: ``get_prs_for_issue`` goes through
    ``/search/issues`` on a 30 req/min budget, and ``HttpGitHubClient`` raises
    ``GitHubTransportError`` for the timeouts and blips that produce it. What
    the handoff has to do with that is unrelated to which exception class it is
    — but a double that raised something the production catch could not see
    would prove nothing about the production path.
    """

    def __init__(self, prs: dict[int, PRInfo] | None = None) -> None:
        super().__init__(prs)
        #: Flip to let the read succeed, which is what recovery looks like.
        self.reachable = False

    def get_prs_for_issue(
        self, issue_number: int, state: str = "open"
    ) -> list[PRInfo]:
        if self.reachable:
            return super().get_prs_for_issue(issue_number, state)
        self.reads += 1
        raise GitHubTransportError(
            f"GitHub search unavailable for issue {issue_number}"
        )


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
    record: str | None = RUN_RECORD_RELATIVE,
    sha: str = SHA,
    publication_passed: bool = False,
) -> Attempt:
    issue = _issue()
    attempt = Attempt(
        key=AttemptKey(issue.key, sha),
        validation_record_path=record,
        completed_evaluations=(
            ValidationVerdictReceipt(
                suite=PUBLISH_SUITE,
                head_sha=sha,
                verdict=(
                    ValidationVerdict.PASSED
                    if publication_passed
                    else ValidationVerdict.FAILED
                ),
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
            suite=PUBLISH_SUITE,
            command=PUBLISH_COMMAND,
            profile=PROFILE,
        )
    )


def _exit(
    issue: Issue,
    attempt: Attempt,
    phase: ContinuationPhase = ContinuationPhase.EXHAUSTED,
) -> ContinuationReworkExit:
    return ContinuationReworkExit(
        key=ControlOperationKey(issue.key, attempt.key.head_sha, CONTINUATION_KIND),
        issue=issue,
        attempt=attempt,
        phase=phase,
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


# ---------------------------------------------------------------------------
# The durable #94 store, exercised for real
# ---------------------------------------------------------------------------


def _validation_record(
    *, sha: str = SHA, suite: str = PUBLISH_SUITE, run_dir: Path | None = None
) -> ValidationRecord:
    """A failed gate run, as the gate itself would have recorded it.

    ``run_dir`` is where the run wrote its own copy — inside the coder worktree,
    which is exactly what the durable store exists because of.
    """
    stdout_path = (
        str(run_dir / STDOUT_FILE_NAME) if run_dir is not None else RUN_RECORD_RELATIVE
    )
    stderr_path = (
        str(run_dir / STDERR_FILE_NAME) if run_dir is not None else RUN_RECORD_RELATIVE
    )
    return ValidationRecord(
        schema_version=VALIDATION_SCHEMA_VERSION,
        suite=suite,
        head_sha=sha,
        passed=False,
        exit_code=1,
        command=PUBLISH_COMMAND,
        started_at="2026-08-26T00:00:00+00:00",
        ended_at="2026-08-26T00:11:00+00:00",
        timed_out=False,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        profile=PROFILE,
    )


def _file_failure(
    repo_root: Path,
    *,
    sha: str = SHA,
    suite: str = PUBLISH_SUITE,
    stdout: str = FAILING_STDOUT,
    stderr: str = FAILING_STDERR,
    run_dir: Path | None = None,
) -> Path:
    """File a failed gate's output through the real #94 writer."""
    written = (
        GateFailureDiagnostics(repo_root)
        .for_candidate(_issue().key)
        .record_failure(
            GateFailureOutput(
                record=_validation_record(sha=sha, suite=suite, run_dir=run_dir),
                stdout=stdout,
                stderr=stderr,
            )
        )
    )
    assert written is not None
    return written


def _resolved_failure(repo_root: Path, *, sha: str = SHA) -> DurableGateFailure:
    """What the handoff resolves, read back through the store's own reader."""
    failure = (
        GateFailureDiagnostics(repo_root)
        .for_candidate(_issue().key)
        .latest_failure(head_sha=sha, suite=PUBLISH_SUITE)
    )
    assert failure is not None
    return failure


@pytest.fixture
def unexplained_root(tmp_path: Path) -> Path:
    """A primary checkout in which no failure output was ever kept."""
    root = tmp_path / "primary-checkout"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(unexplained_root: Path) -> Path:
    """A primary checkout holding the failed candidate's durable explanation."""
    _file_failure(unexplained_root)
    return unexplained_root


def _handoff(
    repo_root: Path,
    state: OrchestratorState,
    pull_requests: StubPullRequests,
    events: RecordingEvents | None = None,
    *,
    scoped_to: int | None = None,
) -> ContinuationReworkHandoff:
    """The owner under test.

    ``scoped_to`` is the operator's ``--issue N`` narrowing, set on the real
    ``Config`` and read back through the engine's own scope owner. A test that
    injected a hand-written predicate instead would prove the handoff can be
    told "no", not that the engine's configured scope is what tells it (#304).
    """
    config = Config()
    config.filtering.issue = scoped_to
    return ContinuationReworkHandoff(
        state=state,
        scope=EngineIssueScope(config),
        pull_requests=pull_requests,  # type: ignore[arg-type]
        budget=ReworkCycleBudget(
            LabelManager(config), max_rework_cycles=config.max_rework_cycles
        ),
        diagnostics=GateFailureDiagnostics(repo_root),
        events=events if events is not None else RecordingEvents(),  # type: ignore[arg-type]
    )


class TestEveryExitProducesAStatedOutcome:
    def test_an_admitted_exit_reports_the_pr_and_the_cycle(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert len(result.outcomes) == 1
        outcome = result.outcomes[0]
        assert outcome.verdict is ReworkAdmissionVerdict.QUEUE
        assert outcome.pr_number == 294
        assert outcome.rework_cycle == 1
        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)

    def test_a_candidate_with_no_open_pr_says_so(self, repo_root: Path) -> None:
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, StubPullRequests())

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "no_open_pr"

    def test_a_candidate_with_no_agent_label_says_so(self, repo_root: Path) -> None:
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue("some-other-label"), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "no_agent_label"

    def test_an_escalated_exit_reports_the_cycle_it_could_not_take(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        config = Config()
        handoff = _handoff(
            repo_root,
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

    def test_no_exits_asks_nothing_of_anybody(self, repo_root: Path) -> None:
        handoff = _handoff(
            repo_root, OrchestratorState(), UnreachablePullRequests()
        )

        result = handoff.admit([])

        assert result.outcomes == ()
        assert result.reworks == ()
        assert result.escalations == ()

    def test_a_held_issue_refuses_before_github_is_reached(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, UnreachablePullRequests())
        assert state.record_discovered_rework(_handoff_fact())

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "already_queued"


class TestNoRefusalCostsAGitHubRead:
    """F1: every refusal the exit's own facts can settle is settled for free.

    The board issue arrives with the exit, so its blocking labels and its agent
    label cost nothing to consult. Reaching the PR port to answer from either
    would be one search-API call per reconciliation, forever, for a candidate
    that is refused every time.

    ``missing_failure_evidence`` is deliberately NOT in this class. Its input is
    just as free — #94's store is on the engine's own filesystem — but the
    ceiling outranks it (see :class:`TestTheCeilingOutranksTheEvidenceGate`), so
    it is asked after the PR read. What it pays for is a positive PR answer,
    which ``AdapterCache`` caches, rather than the uncached search the
    ``no_open_pr`` memo below exists to avoid.
    """

    def test_a_blocked_issue_refuses_before_the_pr_is_read(
        self, repo_root: Path
    ) -> None:
        handoff = _handoff(
            repo_root, OrchestratorState(), UnreachablePullRequests()
        )

        result = handoff.admit(
            [_exit(_issue("agent:backend", "needs-human"), _attempt())]
        )

        assert result.reworks == ()
        assert result.outcomes[0].reason == "issue_blocked"

    def test_a_missing_agent_label_refuses_before_the_pr_is_read(
        self, repo_root: Path
    ) -> None:
        handoff = _handoff(
            repo_root, OrchestratorState(), UnreachablePullRequests()
        )

        result = handoff.admit([_exit(_issue("some-other-label"), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "no_agent_label"

    def test_a_settled_absence_of_a_pr_is_not_searched_for_again(
        self, repo_root: Path
    ) -> None:
        pull_requests = CountingPullRequests()
        handoff = _handoff(repo_root, OrchestratorState(), pull_requests)
        exit_ = _exit(_issue(), _attempt())

        for _ in range(4):
            result = handoff.admit([exit_])
            assert result.outcomes[0].reason == "no_open_pr"

        assert pull_requests.reads == 1

    def test_a_newer_candidate_is_searched_for_afresh(self, repo_root: Path) -> None:
        pull_requests = CountingPullRequests()
        handoff = _handoff(repo_root, OrchestratorState(), pull_requests)
        _file_failure(repo_root, sha=OTHER_SHA)

        handoff.admit([_exit(_issue(), _attempt())])
        handoff.admit([_exit(_issue(), _attempt(sha=OTHER_SHA))])

        assert pull_requests.reads == 2

    def test_an_exit_that_stops_being_derived_is_forgotten(
        self, repo_root: Path
    ) -> None:
        pull_requests = CountingPullRequests()
        handoff = _handoff(repo_root, OrchestratorState(), pull_requests)
        exit_ = _exit(_issue(), _attempt())

        handoff.admit([exit_])
        handoff.admit([])  # the durable facts changed; nothing exits this pass
        handoff.admit([exit_])

        assert pull_requests.reads == 2


class TestAReadThatFailedIsNotAFact:
    """A recoverable GitHub error must not settle anything, ever.

    The memo above is what keeps a re-derived exit off the search API, and it is
    also the sharpest edge in this module: it is an instance attribute of a
    handoff the composition root builds ONCE per engine, and the exit that feeds
    it keeps being derived for as long as the durable facts stand. So an entry
    written from a rate-limited search would never expire — the candidate would
    take the short-circuit on every subsequent reconciliation, silently, with no
    read and nothing published, until the process restarted. That is
    ``BOUNDED_PR_BACKED_CONTINUATION_GAP`` all over again, reached from a blip
    rather than from a design gap, and reached past the very fix #297 is.

    The rule that closes it is the repo's own: a read that failed is not a fact.
    ``OpenPrLookup`` is what carries the difference, so the handoff never has to
    infer it from a bare ``None``.
    """

    def test_an_unreachable_github_is_read_again_next_pass(
        self, repo_root: Path
    ) -> None:
        pull_requests = RateLimitedPullRequests()
        handoff = _handoff(repo_root, OrchestratorState(), pull_requests)
        exit_ = _exit(_issue(), _attempt())

        first = handoff.admit([exit_])
        second = handoff.admit([exit_])

        assert first.outcomes[0].reason == "pr_read_failed"
        assert second.outcomes[0].reason == "pr_read_failed"
        assert pull_requests.reads == 2

    def test_the_candidate_is_admitted_as_soon_as_the_read_succeeds(
        self, repo_root: Path
    ) -> None:
        """The whole point: the outage costs a pass, not the candidate."""
        state = OrchestratorState()
        pull_requests = RateLimitedPullRequests({ISSUE_NUMBER: _pr()})
        handoff = _handoff(repo_root, state, pull_requests)
        exit_ = _exit(_issue(), _attempt())

        refused = handoff.admit([exit_])
        assert refused.reworks == ()
        assert state.discovered_reworks == []

        pull_requests.reachable = True
        admitted = handoff.admit([exit_])

        assert admitted.outcomes[0].verdict is ReworkAdmissionVerdict.QUEUE
        assert admitted.admitted_issue_numbers == (ISSUE_NUMBER,)
        assert admitted.reworks[0].rework_cycle == 1

    def test_a_failed_read_never_becomes_a_settled_absence(
        self, repo_root: Path
    ) -> None:
        """The regression itself: the outage must not be filed as "no open PR".

        A pass whose read failed and then a pass whose read answered "no open
        PR" must both reach the port. If the first had settled the memo, the
        second would never read at all — and the reason it reported would be a
        conclusion nothing ever checked.
        """
        pull_requests = RateLimitedPullRequests()
        handoff = _handoff(repo_root, OrchestratorState(), pull_requests)
        exit_ = _exit(_issue(), _attempt())

        handoff.admit([exit_])
        pull_requests.reachable = True
        answered = handoff.admit([exit_])

        assert answered.outcomes[0].reason == "no_open_pr"
        assert pull_requests.reads == 2

    def test_an_outage_is_published_once_rather_than_every_pass(
        self, repo_root: Path
    ) -> None:
        """A budget that stays exhausted keeps a candidate from moving, so it is
        visible — but it is one candidate stuck, not one event per tick."""
        events = RecordingEvents()
        handoff = _handoff(
            repo_root, OrchestratorState(), RateLimitedPullRequests(), events
        )
        exit_ = _exit(_issue(), _attempt())

        for _ in range(4):
            handoff.admit([exit_])

        assert events.reasons() == ["pr_read_failed"]


class TestARefusalThatStrandsACandidateIsPublished:
    """N1: the refusals nothing downstream retries reach the UI as events.

    A candidate refused for ``no_open_pr``, ``no_agent_label`` or
    ``missing_failure_evidence`` sits until a human looks at it. Per this repo's
    events-vs-logs rule a UI may not read the log line that says so, so these
    are published.
    """

    def test_a_stranded_candidate_publishes_its_reason(
        self, repo_root: Path
    ) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            repo_root, OrchestratorState(), StubPullRequests(), events
        )

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

    def test_a_candidate_with_no_agent_label_publishes_too(
        self, repo_root: Path
    ) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            repo_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            events,
        )

        handoff.admit([_exit(_issue("some-other-label"), _attempt())])

        assert events.reasons() == ["no_agent_label"]

    def test_an_unexplainable_failure_publishes_too(
        self, unexplained_root: Path
    ) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            events,
        )

        handoff.admit([_exit(_issue(), _attempt())])

        assert events.published == [
            (
                EventName.REWORK_SKIPPED.value,
                {
                    "reason": "missing_failure_evidence",
                    "issue_number": ISSUE_NUMBER,
                    "source": CONTINUATION_EXIT_SOURCE,
                },
            )
        ]

    def test_an_admitted_exit_publishes_no_refusal(self, repo_root: Path) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            repo_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            events,
        )

        handoff.admit([_exit(_issue(), _attempt())])

        assert events.published == []

    def test_a_permanent_strand_is_announced_once_not_once_per_tick(
        self, unexplained_root: Path
    ) -> None:
        """The exit is re-derived forever; the news happens once.

        ``missing_failure_evidence`` is permanent by construction — a bundle
        that is not there now was never written — and the exit that produces it
        keeps being derived until a human acts. An event per reconciliation
        would tell a consumer something changed on every tick for the rest of
        the candidate's life, when nothing has.
        """
        events = RecordingEvents()
        handoff = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            events,
        )
        exit_ = _exit(_issue(), _attempt())

        for _ in range(4):
            result = handoff.admit([exit_])
            # The decision is still re-made every pass — only the announcement
            # is remembered, so a bundle that appears later is still picked up.
            assert result.outcomes[0].reason == "missing_failure_evidence"

        assert events.reasons() == ["missing_failure_evidence"]

    def test_a_candidate_that_leaves_the_exit_and_returns_is_news_again(
        self, unexplained_root: Path
    ) -> None:
        events = RecordingEvents()
        handoff = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            events,
        )
        exit_ = _exit(_issue(), _attempt())

        handoff.admit([exit_])
        handoff.admit([])  # the durable facts changed; nothing exits this pass
        handoff.admit([exit_])

        assert events.reasons() == [
            "missing_failure_evidence",
            "missing_failure_evidence",
        ]

    def test_a_later_bundle_still_closes_a_strand_that_was_announced(
        self, unexplained_root: Path
    ) -> None:
        """Announcing once must not settle the decision.

        The announcement memo is about the event stream, not about the
        candidate: the evidence read still happens on every pass, so a bundle
        that is restored — or one written by a gate that ran after the strand —
        is picked up on the very next reconciliation.
        """
        state = OrchestratorState()
        handoff = _handoff(
            unexplained_root, state, StubPullRequests({ISSUE_NUMBER: _pr()})
        )
        exit_ = _exit(_issue(), _attempt())
        assert handoff.admit([exit_]).reworks == ()

        _file_failure(unexplained_root)

        assert handoff.admit([exit_]).admitted_issue_numbers == (ISSUE_NUMBER,)


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

    def test_the_sweeps_fact_does_not_block_the_handoff_from_filing(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        state.record_discovered_rework(_sweep_fact())
        handoff = _handoff(repo_root, state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)
        assert len(state.discovered_reworks) == 1
        assert state.discovered_reworks[0].source == CONTINUATION_EXIT_SOURCE

    def test_the_handoffs_own_fact_still_blocks_a_second_pass(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        handoff.admit([_exit(_issue(), _attempt())])
        second = handoff.admit([_exit(_issue(), _attempt())])

        assert second.reworks == ()
        assert second.outcomes[0].reason == "already_queued"
        assert len(state.discovered_reworks) == 1

    def test_an_escalation_is_a_claim_no_later_fact_may_supersede(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        state.record_discovered_escalation(
            DiscoveredEscalation(issue_number=ISSUE_NUMBER, pr_number=294, rework_cycle=6)
        )
        handoff = _handoff(repo_root, state, UnreachablePullRequests())

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "already_queued"


class TestThePublicationFailureIsHandedOverWithItsOutput:
    """Acceptance 3: the next rework gets the actual failure, with no human relay.

    The pre-correction handoff formatted ``Attempt.validation_record_path`` and
    said it might have been reaped. That tells a corrector THAT publication
    failed; the thing it must be told is WHAT failed, and after cleanup that
    lives in exactly one place — #94's durable bundle in the primary checkout.
    """

    def test_the_failing_output_survives_the_candidates_worktree(
        self, unexplained_root: Path, tmp_path: Path
    ) -> None:
        """The post-cleanup boundary, walked in the order production walks it.

        The gate runs inside a worktree and files its output durably at the same
        moment. Then ordinary cleanup takes the worktree — with the run
        directory the attempt's ``validation_record_path`` points at — and only
        after that does the handoff run.
        """
        worktree = tmp_path / "issue-297-worktree"
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-7"
        run_dir.mkdir(parents=True)
        (run_dir / STDOUT_FILE_NAME).write_text(FAILING_STDOUT, encoding="utf-8")
        (run_dir / STDERR_FILE_NAME).write_text(FAILING_STDERR, encoding="utf-8")
        record_path = run_dir / "validation-record.json"
        record_path.write_text("{}", encoding="utf-8")
        bundle = _file_failure(unexplained_root, run_dir=run_dir)

        # Ordinary cleanup. Everything session-local about the failure is gone.
        shutil.rmtree(worktree)
        assert not record_path.exists()
        assert not run_dir.exists()

        state = OrchestratorState()
        handoff = _handoff(
            unexplained_root, state, StubPullRequests({ISSUE_NUMBER: _pr()})
        )
        result = handoff.admit(
            [_exit(_issue(), _attempt(record=str(record_path)))]
        )

        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)
        feedback = result.reworks[0].feedback
        assert feedback is not None
        # The actual failing test and the actual failing command, not a pointer
        # to somewhere a reader would have to go and find them.
        assert FAILING_STDOUT in feedback
        assert FAILING_STDERR in feedback
        # And where the whole of it still is, in the primary checkout.
        assert str(bundle) in feedback
        assert bundle.is_relative_to(unexplained_root)
        # The dead session-local pointer is not offered as evidence any more.
        assert str(record_path) not in feedback

    def test_the_failed_sha_and_the_publish_verdict_still_travel(
        self, repo_root: Path
    ) -> None:
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue(), _attempt())])

        feedback = result.reworks[0].feedback
        assert feedback is not None
        assert SHA in feedback
        assert PUBLISH_COMMAND in feedback
        assert ValidationVerdict.FAILED.value in feedback

    def test_a_failure_nobody_kept_is_not_handed_over_at_all(
        self, unexplained_root: Path
    ) -> None:
        """Fail closed: no evidence, no rework, and the gap is named."""
        state = OrchestratorState()
        handoff = _handoff(
            unexplained_root, state, StubPullRequests({ISSUE_NUMBER: _pr()})
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.escalations == ()
        assert state.discovered_reworks == []
        assert result.outcomes[0].verdict is ReworkAdmissionVerdict.SKIP
        assert result.outcomes[0].reason == "missing_failure_evidence"

    def test_another_candidates_explanation_does_not_stand_in_for_this_one(
        self, unexplained_root: Path
    ) -> None:
        """A′'s bundle must not be read as A's — the #94 binding, from the read side."""
        _file_failure(unexplained_root, sha=OTHER_SHA, stdout="A-prime failed there")

        result = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
        ).admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "missing_failure_evidence"

    def test_another_contracts_explanation_does_not_stand_in_either(
        self, unexplained_root: Path
    ) -> None:
        """The quick gate's failure is not the publish gate's failure.

        Both can be filed for one candidate, and the suite in the name is what
        keeps them apart. A publish refusal explained by the quick gate's output
        would send a corrector after the wrong contract.
        """
        _file_failure(
            unexplained_root, suite=QUICK_SUITE, stdout="the quick gate failed"
        )

        result = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
        ).admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "missing_failure_evidence"

    def test_a_bundle_with_no_output_explains_nothing(
        self, unexplained_root: Path
    ) -> None:
        """It repeats the receipt and adds nothing, which is not evidence."""
        _file_failure(unexplained_root, stdout="", stderr="   \n")

        result = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
        ).admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.outcomes[0].reason == "missing_failure_evidence"

    def test_the_newest_explanation_is_the_one_handed_over(
        self, repo_root: Path
    ) -> None:
        """A retried publish files one bundle per attempt; the last one is current."""
        _file_failure(repo_root, stdout="the second reason it failed")

        result = _handoff(
            repo_root, OrchestratorState(), StubPullRequests({ISSUE_NUMBER: _pr()})
        ).admit([_exit(_issue(), _attempt())])

        feedback = result.reworks[0].feedback
        assert feedback is not None
        assert "the second reason it failed" in feedback
        assert FAILING_STDOUT not in feedback

    def test_an_exit_whose_publication_never_failed_needs_no_bundle(
        self, unexplained_root: Path
    ) -> None:
        """``EXIT_TO_REWORK`` after a PASS owes no publish-gate explanation.

        The obligation is read off the durable record — a publication receipt
        that REFUSED this candidate — not off "the phase exits to rework". A
        reviewer asking for changes on a commit that passed publication has no
        gate failure to explain, and stranding it would break the exit #149's
        own predicate declares.
        """
        result = _handoff(
            unexplained_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
        ).admit(
            [
                _exit(
                    _issue(),
                    _attempt(publication_passed=True),
                    ContinuationPhase.EXIT_TO_REWORK,
                )
            ]
        )

        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)


class TestTheCeilingOutranksTheEvidenceGate:
    """Acceptance 4: at exhaustion, today's escalation path fires. No exceptions.

    The evidence gate guards the *spending* of a rework cycle, so it may only be
    asked once the existing cycle owner has granted one. A candidate that is
    simultaneously at the ceiling and missing its durable explanation is at the
    ceiling first: #297 requires that exhaustion take today's escalation path
    with no new budget, and an evidence refusal reached earlier would swap the
    escalation a human is waiting on for a strand that produces nothing.

    The inverse property is the one the round-1 correction bought, and it is
    still here: below the ceiling, a failure nobody kept is refused before any
    rework is filed and before any principal is spawned.
    """

    @staticmethod
    def _pr_at_cycle(cycle: int) -> dict[int, PRInfo]:
        """A PR whose durable ``rework-cycle-N`` label says N cycles are spent."""
        return {ISSUE_NUMBER: _pr(labels=[f"rework-cycle-{cycle}"])}

    def test_exhaustion_escalates_even_when_the_evidence_is_gone(
        self, unexplained_root: Path
    ) -> None:
        state = OrchestratorState()
        events = RecordingEvents()
        ceiling = Config().max_rework_cycles
        handoff = _handoff(
            unexplained_root, state, StubPullRequests(self._pr_at_cycle(ceiling)), events
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.outcomes[0].verdict is ReworkAdmissionVerdict.ESCALATE
        assert result.outcomes[0].reason == "max_rework_exceeded"
        assert result.outcomes[0].rework_cycle == ceiling + 1
        # Today's escalation path, filed through the collection's own owner.
        assert result.escalations == (
            DiscoveredEscalation(
                issue_number=ISSUE_NUMBER, pr_number=294, rework_cycle=ceiling + 1
            ),
        )
        assert state.discovered_escalations == list(result.escalations)
        # And not diverted into the strand that produces nothing for a human.
        assert events.reasons() == []
        assert result.reworks == ()

    def test_the_ceiling_is_the_only_thing_that_outranks_it(
        self, unexplained_root: Path
    ) -> None:
        """One cycle below the ceiling, the missing explanation still refuses."""
        state = OrchestratorState()
        ceiling = Config().max_rework_cycles
        handoff = _handoff(
            unexplained_root,
            state,
            StubPullRequests(self._pr_at_cycle(ceiling - 1)),
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.outcomes[0].verdict is ReworkAdmissionVerdict.SKIP
        assert result.outcomes[0].reason == "missing_failure_evidence"
        assert result.reworks == ()
        assert result.escalations == ()
        assert state.discovered_reworks == []
        assert state.discovered_escalations == []
        # It refused the cycle the owner had granted, and says which one.
        assert result.outcomes[0].pr_number == 294
        assert result.outcomes[0].rework_cycle == ceiling

    def test_the_refused_cycle_is_not_consumed(self, unexplained_root: Path) -> None:
        """Fail-closed must not cost the candidate a cycle it never spent.

        Nothing is filed and no principal is spawned, so the launcher never
        writes ``rework-cycle-N`` — and the very next pass, once the evidence
        gap is closed, is offered the same cycle the refusal declined.
        """
        state = OrchestratorState()
        handoff = _handoff(
            unexplained_root, state, StubPullRequests(self._pr_at_cycle(2))
        )
        refused = handoff.admit([_exit(_issue(), _attempt())])
        assert refused.outcomes[0].rework_cycle == 3

        _file_failure(unexplained_root)
        admitted = handoff.admit([_exit(_issue(), _attempt())])

        assert admitted.outcomes[0].verdict is ReworkAdmissionVerdict.QUEUE
        assert admitted.outcomes[0].rework_cycle == 3
        assert admitted.admitted_issue_numbers == (ISSUE_NUMBER,)

    def test_a_ceiling_candidate_escalates_once_per_tick(
        self, unexplained_root: Path
    ) -> None:
        """The escalation an unexplainable exit takes is the ordinary one.

        Same owner, same once-per-issue-per-tick rule: a re-derived exit does
        not file a second escalation, and it does not fall through to the
        evidence strand on the second pass either.
        """
        state = OrchestratorState()
        ceiling = Config().max_rework_cycles
        handoff = _handoff(
            unexplained_root, state, StubPullRequests(self._pr_at_cycle(ceiling))
        )

        handoff.admit([_exit(_issue(), _attempt())])
        second = handoff.admit([_exit(_issue(), _attempt())])

        assert len(state.discovered_escalations) == 1
        assert second.reworks == ()
        assert second.outcomes[0].reason == "already_queued"


class TestTheEvidenceIsCopiedNotDerived:
    def test_every_correction_fact_appears_verbatim(self, repo_root: Path) -> None:
        pr = _pr()
        attempt = _attempt()
        failure = _resolved_failure(repo_root)

        feedback = build_continuation_rework_feedback(
            pr=pr,
            attempt=attempt,
            phase_reason=ContinuationPhase.EXHAUSTED.value,
            failure=failure,
        )

        assert SHA in feedback
        assert PUBLISH_COMMAND in feedback
        assert ValidationVerdict.FAILED.value in feedback
        assert str(failure.directory) in feedback
        assert FAILING_STDOUT in feedback
        assert FAILING_STDERR in feedback
        assert pr.url in feedback
        assert pr.branch in feedback
        assert "what the agent claimed to build" in feedback
        assert "the publish gate refused it" in feedback
        assert ContinuationPhase.EXHAUSTED.value in feedback

    def test_a_long_log_is_tailed_and_says_so(self, unexplained_root: Path) -> None:
        """The prompt gets the end of the log, and the path to the rest.

        Truncating silently would hand a corrector a fragment it could not tell
        was a fragment; the tail is where a test runner names what failed, and
        the durable path is the way past the bound.
        """
        noise = "\n".join(f"line {index}" for index in range(20_000))
        _file_failure(
            unexplained_root, stdout=f"{noise}\n{FAILING_STDOUT}"
        )
        failure = _resolved_failure(unexplained_root)

        feedback = build_continuation_rework_feedback(
            pr=_pr(),
            attempt=_attempt(),
            phase_reason="exhausted",
            failure=failure,
        )

        assert failure.stdout.truncated is True
        assert FAILING_STDOUT in feedback
        assert "line 0\n" not in feedback
        assert f"last {FAILURE_LOG_TAIL_BYTES} bytes" in feedback
        assert str(failure.directory) in feedback

    def test_a_candidate_with_no_recorded_verdict_says_so(self) -> None:
        issue = _issue()
        bare = Attempt(key=AttemptKey(issue.key, SHA))

        feedback = build_continuation_rework_feedback(
            pr=_pr(), attempt=bare, phase_reason="exhausted", failure=None
        )

        assert "no verdict was recorded" in feedback

    def test_an_unrecorded_intent_is_omitted_not_invented(
        self, repo_root: Path
    ) -> None:
        feedback = build_continuation_rework_feedback(
            pr=_pr(),
            attempt=_attempt(with_descriptor=False),
            phase_reason="exhausted",
            failure=_resolved_failure(repo_root),
        )

        assert "what the agent claimed to build" not in feedback
        # The failure itself is still there; only the agent's own account is not.
        assert FAILING_STDOUT in feedback

    def test_the_corrector_is_told_not_to_trust_the_failed_commit(
        self, repo_root: Path
    ) -> None:
        feedback = build_continuation_rework_feedback(
            pr=_pr(),
            attempt=_attempt(),
            phase_reason="exhausted",
            failure=_resolved_failure(repo_root),
        )

        assert "Do not treat the failed commit above as validated." in feedback


class TestReconciliationVisibilityIsNotWorkAdmissionAuthority:
    """#304: an exit derived over the whole board is not a licence to work it.

    ``ControlContinuation`` must reconcile continuation truth board-wide —
    ownership release names every lease the derived live set does not, so an
    engine that looked at less would report other issues' running operations as
    finished. #297 attached this work-admitting producer to the end of that
    board-wide sequence and gave it no scope predicate, and #303 measured the
    consequence: an engine started with ``--issue 301`` filed, queued and
    launched ordinary rework for held issue #293, created its worktree and
    rebased its branch.

    Every direction here is about the OUT-OF-SCOPE exit reaching this owner and
    leaving nothing behind. The two facts this producer can file are asserted
    separately, because they are reached by different routes: a rework below the
    ceiling, an escalation at it.
    """

    #: The issue the operator narrowed this engine to. Not ``ISSUE_NUMBER``, so
    #: the exits below belong to an issue the engine was never started for.
    SCOPED_TO = 301

    def test_an_out_of_scope_exit_files_no_rework(self, repo_root: Path) -> None:
        state = OrchestratorState()
        handoff = _handoff(
            repo_root,
            state,
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            scoped_to=self.SCOPED_TO,
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.reworks == ()
        assert result.admitted_issue_numbers == ()
        assert state.discovered_reworks == []

    def test_an_out_of_scope_exit_at_the_ceiling_files_no_escalation(
        self, repo_root: Path
    ) -> None:
        """The other producible fact, reached only past the cycle owner.

        An escalation is filed when the budget refuses a cycle, which is a
        different branch from the admission above and would survive a fix that
        only guarded the first one.
        """
        state = OrchestratorState()
        max_cycles = Config().max_rework_cycles
        handoff = _handoff(
            repo_root,
            state,
            StubPullRequests({ISSUE_NUMBER: _pr(labels=[f"rework-cycle-{max_cycles}"])}),
            scoped_to=self.SCOPED_TO,
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.escalations == ()
        assert state.discovered_escalations == []

    def test_the_refusal_is_stated_rather_than_silently_dropped(
        self, repo_root: Path
    ) -> None:
        """Reconcile-only is a state the caller has to be able to see.

        A handoff that filtered these out of its result would be
        indistinguishable from one whose scope owner had accidentally been given
        the whole board — which is the shape of the defect, not of the fix.
        """
        handoff = _handoff(
            repo_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            scoped_to=self.SCOPED_TO,
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert len(result.outcomes) == 1
        outcome = result.outcomes[0]
        assert outcome.issue_number == ISSUE_NUMBER
        assert outcome.verdict is ReworkAdmissionVerdict.SKIP
        assert outcome.reason == "outside_engine_scope"

    def test_the_refusal_precedes_the_github_read(self, repo_root: Path) -> None:
        """Authority is asked first, so a foreign exit costs no API call.

        The exit is re-derived on every reconciliation for as long as the
        durable facts stand. A scope refusal that paid a ``/search/issues`` read
        each time would spend a narrowed engine's whole rate budget on issues it
        is not allowed to touch.
        """
        handoff = _handoff(
            repo_root,
            OrchestratorState(),
            UnreachablePullRequests(),
            scoped_to=self.SCOPED_TO,
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.outcomes[0].reason == "outside_engine_scope"

    def test_the_refusal_is_not_announced_to_the_ui(self, repo_root: Path) -> None:
        """Refused, not stranded — and the difference is who needs telling.

        The three strands leave a candidate sitting until a human looks, so they
        publish. This candidate is not stuck; it belongs to another engine's
        scope. Publishing ``rework.skipped`` for it from an engine narrowed to a
        different issue would put exactly the cross-issue traffic #304 removes
        back into the stream a consumer reads.
        """
        events = RecordingEvents()
        handoff = _handoff(
            repo_root,
            OrchestratorState(),
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            events,
            scoped_to=self.SCOPED_TO,
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.outcomes[0].reason == "outside_engine_scope"
        assert events.published == []

    def test_the_narrowed_engines_own_issue_is_untouched(
        self, repo_root: Path
    ) -> None:
        """Acceptance 3: the target issue behaves exactly as #297 intended."""
        state = OrchestratorState()
        handoff = _handoff(
            repo_root,
            state,
            StubPullRequests({ISSUE_NUMBER: _pr()}),
            scoped_to=ISSUE_NUMBER,
        )

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.outcomes[0].verdict is ReworkAdmissionVerdict.QUEUE
        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)
        assert state.discovered_reworks[0].source == CONTINUATION_EXIT_SOURCE

    def test_an_unnarrowed_engine_admits_every_exit_as_before(
        self, repo_root: Path
    ) -> None:
        """Acceptance 4: without a single-issue narrowing nothing changes."""
        state = OrchestratorState()
        handoff = _handoff(repo_root, state, StubPullRequests({ISSUE_NUMBER: _pr()}))

        result = handoff.admit([_exit(_issue(), _attempt())])

        assert result.admitted_issue_numbers == (ISSUE_NUMBER,)
        assert len(state.discovered_reworks) == 1
