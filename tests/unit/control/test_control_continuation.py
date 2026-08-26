"""The production control continuation (#149).

Fourteen failure directions, one class per direction the issue states. Each
proves that the continuation refuses in the direction it must, not merely that
it works when everything is fine.

Everything durable here is the REAL orchestrator-owned storage over a temporary
directory: the sidecar attempt store the gate files receipts into, and the
sqlite lease ledger #146 owns. A "restart" is a fresh
:class:`OrchestratorState` plus fresh store handles over the same files, which
is what a new process gets — so an in-process-only decision passes none of the
restart directions.

The two doubles are the unreadable stores, mocked at their port boundaries
because a genuine sqlite or filesystem outage cannot be produced
deterministically, and a recording runner that stands in for the execution
plane. Nothing constructs a Session, a terminal or a queue request: the whole
premise of a control operation is that none of those exist.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from tests.unit.session_run_helpers import make_session_run_assets

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.continuation_descriptor_writer import (
    ContinuationDescriptorWriter,
)
from issue_orchestrator.control.continuation_in_flight import ContinuationsInFlight
from issue_orchestrator.control.continuation_runs import (
    ContinuationRun,
    ContinuationRuns,
)
from issue_orchestrator.control.continuation_live_truth import (
    CONTINUATION_KIND,
    ContinuationLiveTruth,
    ContinuationReconciliation,
)
from issue_orchestrator.control.continuation_rework_handoff import (
    CONTINUATION_EXIT_SOURCE,
    ContinuationReworkHandoff,
)
from issue_orchestrator.control.continuation_scheduling import ControlContinuation
from issue_orchestrator.control.control_operation_ownership import (
    ControlOperationOwnership,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.control.rework_cycle_policy import ReworkCycleBudget
from issue_orchestrator.domain.attempt import (
    CONTINUATION_RUN_ALLOWANCE,
    Attempt,
    AttemptKey,
)
from issue_orchestrator.domain.continuation_descriptor import ContinuationDescriptor
from issue_orchestrator.domain.continuation_phase import ContinuationPhase
from issue_orchestrator.domain.continuation_settlement import (
    ContinuationSettlement,
    ContinuationSettlementKind,
)
from issue_orchestrator.domain.control_operation import ControlOperationKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    CompletionOutcome,
    CompletionRecord,
    Issue,
    OrchestratorState,
    PendingRework,
    RequestedAction,
    Session,
    SessionHistoryEntry,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.review_verdict_binding import (
    BoundReviewVerdict,
    ReviewVerdictOutcome,
)
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.execution.control_operation_ownership_store import (
    SqliteControlOperationOwnershipStore,
)
from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.control_operation_ownership_store import (
    ControlOperationOwnershipRead,
    ControlOperationReadStatus,
    ControlOperationRelease,
    ControlOperationReleaseStatus,
    ControlOperationReservation,
    ControlOperationReservationStatus,
)
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.session_output import ValidationRecord

REPO = "owner/repo"
ISSUE_NUMBER = 149
SHA_A = "a" * 40
SHA_A_PRIME = "b" * 40
PR_PENDING = "pr-pending"
PUBLISH_COMMAND = "make validate-pr-raw"
PROFILE = "default"


# ----------------------------------------------------------------------
# Durable fixtures — the real stores, over a temporary repository root
# ----------------------------------------------------------------------


def _issue(*labels: str, number: int = ISSUE_NUMBER) -> Issue:
    return Issue(number=number, title=f"Issue {number}", labels=list(labels), repo=REPO)


def _issue_key(number: int = ISSUE_NUMBER) -> GitHubIssueKey:
    return _issue(number=number).key  # type: ignore[return-value]


def _attempt_key(head_sha: str = SHA_A, number: int = ISSUE_NUMBER) -> AttemptKey:
    return AttemptKey(_issue_key(number), head_sha)


def _operation_key(
    head_sha: str = SHA_A, number: int = ISSUE_NUMBER
) -> ControlOperationKey:
    return ControlOperationKey(_issue_key(number), head_sha, CONTINUATION_KIND)


def _receipt(
    head_sha: str = SHA_A, *, verdict: ValidationVerdict = ValidationVerdict.FAILED
) -> ValidationVerdictReceipt:
    return ValidationVerdictReceipt(
        suite=ValidationGateKind.PUBLISH.suite,
        head_sha=head_sha,
        verdict=verdict,
        command=PUBLISH_COMMAND,
        profile=PROFILE,
    )


def _descriptor(*actions: RequestedAction) -> ContinuationDescriptor:
    return ContinuationDescriptor(
        requested_actions=tuple(actions),
        implementation="what the agent claimed to build",
        problems="None",
        suite=ValidationGateKind.PUBLISH.suite,
        command=PUBLISH_COMMAND,
        profile=PROFILE,
    )


def _gate_record(
    head_sha: str = SHA_A, *, passed: bool = False
) -> ValidationRecord:
    return ValidationRecord(
        schema_version=1,
        suite=ValidationGateKind.PUBLISH.suite,
        head_sha=head_sha,
        passed=passed,
        exit_code=0 if passed else 1,
        command=PUBLISH_COMMAND,
        started_at="2026-08-19T00:00:00Z",
        ended_at="2026-08-19T00:01:00Z",
        profile=PROFILE,
    )


def _completion(*actions: RequestedAction) -> CompletionRecord:
    return CompletionRecord(
        session_id="issue-149",
        timestamp="2026-08-19T00:00:00Z",
        outcome=CompletionOutcome.COMPLETED,
        summary="done",
        requested_actions=list(actions),
        implementation="what the agent claimed to build",
        problems="None",
    )


# ----------------------------------------------------------------------
# Doubles at the seams a unit test may not drive for real
# ----------------------------------------------------------------------


@dataclass
class RecordingRunner:
    """Stands in for the execution plane, recording what it was asked to do.

    The runner's own composition (worktrees, the completion owner, #139) is
    proved in its own tests; here the question is *which* operations reach it
    and *when*, which is what the ordering and handoff directions are about.
    """

    advanced: list[tuple[ControlOperationKey, ContinuationPhase]] = field(
        default_factory=list
    )
    on_advance: Callable[[ContinuationReconciliation], None] | None = None

    def advance(self, reconciliation: ContinuationReconciliation) -> None:
        for operation in reconciliation.owned:
            self.advanced.append((operation.key, operation.phase))
        if self.on_advance is not None:
            self.on_advance(reconciliation)

    @property
    def advanced_keys(self) -> list[ControlOperationKey]:
        return [key for key, _ in self.advanced]


class _NoWorktrees:
    """A worktree manager for a suite that never opens a run.

    The recording runner stands in for the execution plane here, so no run is
    ever opened and nothing can be closed. Raising rather than passing keeps it
    that way: a suite that started materialising checkouts would say so.
    """

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        raise AssertionError("this suite must not open or close continuation runs")


class _UnreadableOwnershipStore:
    """A lease store that cannot answer, as a sqlite outage cannot say."""

    def reserve_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationReservation:
        return ControlOperationReservation(
            status=ControlOperationReservationStatus.UNAVAILABLE,
            holder="",
            detail="store unreachable",
        )

    def release_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationRelease:
        return ControlOperationRelease(
            status=ControlOperationReleaseStatus.UNAVAILABLE,
            holder="",
            detail="store unreachable",
        )

    def list_control_operation_ownership(self) -> ControlOperationOwnershipRead:
        return ControlOperationOwnershipRead(
            status=ControlOperationReadStatus.UNAVAILABLE,
            detail="store unreachable",
        )


class UnreadableAttempts:
    """An attempt store whose sidecars are damaged rather than absent."""

    def for_key(self, key: AttemptKey) -> Attempt | None:
        raise ValueError(f"Attempt sidecar is unreadable: {key.head_sha}")

    def update(self, key: AttemptKey, mutate: object) -> Attempt:
        raise ValueError("unreadable")

    def for_issue(self, issue_key: object) -> tuple[Attempt, ...]:
        raise ValueError(f"Attempt sidecars are unreadable: {issue_key}")

    def supersede_issue(self, issue_key: object) -> int:
        raise ValueError("unreadable")


@dataclass
class RecordingPullRequests:
    """The board's open pull requests, as GitHub would answer for them.

    A double at the PR port rather than at the handoff, so the production
    open-PR owner (``get_open_pr_for_issue``) really runs — including its
    issue/branch matching, which is what "same lineage" is actually about.
    Every read is counted, because a handoff that re-reads GitHub on every
    reconciliation for a candidate nothing can act on is a defect of its own.
    """

    prs: dict[int, PRInfo] = field(default_factory=dict)
    reads: list[int] = field(default_factory=list)

    def create_pr(self, *args: object, **kwargs: object) -> PRInfo:
        raise AssertionError("the continuation must not create pull requests")

    def get_prs_for_issue(
        self, issue_number: int, state: str = "open"
    ) -> list[PRInfo]:
        self.reads.append(issue_number)
        pr = self.prs.get(issue_number)
        return [pr] if pr is not None else []

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        raise AssertionError("the continuation must not scan branches")


@dataclass
class Engine:
    """One orchestrator engine's continuation stack over real durable stores."""

    root: Path
    state: OrchestratorState
    attempts: SidecarAttemptStore
    ownership: ControlOperationOwnership
    runner: RecordingRunner
    continuation: ControlContinuation
    config: Config
    #: The open pull requests the handoff's open-PR owner will find. Shared
    #: across a restart, because GitHub is what a new process re-reads.
    pull_requests: RecordingPullRequests
    #: What this engine is executing, as the real runner claims into it. A
    #: restart gets a fresh one, which is exactly what a new process gets.
    in_flight: ContinuationsInFlight
    #: The runs this engine is carrying, likewise. Also process-local, so a
    #: restart is an engine that holds none.
    runs: ContinuationRuns

    def queue_cache(self) -> QueueCache:
        return QueueCache(self.config, self.state)

    def file(self, attempt: Attempt) -> Attempt:
        return self.attempts.update(attempt.key, lambda _current: attempt)


def _engine(
    root: Path,
    *,
    attempts: object | None = None,
    pull_requests: RecordingPullRequests | None = None,
) -> Engine:
    """A fresh engine over ``root``. A second one is a restart."""
    state = OrchestratorState()
    config = Config()
    config.repo = REPO
    store = SidecarAttemptStore(root) if attempts is None else attempts
    ownership = ControlOperationOwnership(
        state, SqliteControlOperationOwnershipStore(root / STORE_FILENAME)
    )
    runner = RecordingRunner()
    in_flight = ContinuationsInFlight()
    runs = ContinuationRuns(_NoWorktrees())  # type: ignore[arg-type]
    prs = pull_requests if pull_requests is not None else RecordingPullRequests()
    continuation = ControlContinuation(
        ownership,
        ContinuationLiveTruth(
            store,  # type: ignore[arg-type]
            pr_pending_label=PR_PENDING,
            in_flight=in_flight,
            runs=runs,
        ),
        runner,  # type: ignore[arg-type]
        ContinuationReworkHandoff(
            state=state,
            pull_requests=prs,  # type: ignore[arg-type]
            budget=ReworkCycleBudget(
                LabelManager(config), max_rework_cycles=config.max_rework_cycles
            ),
            events=NullEventSink(),
        ),
    )
    return Engine(
        root=root,
        state=state,
        attempts=store,  # type: ignore[arg-type]
        ownership=ownership,
        runner=runner,
        continuation=continuation,
        config=config,
        pull_requests=prs,
        in_flight=in_flight,
        runs=runs,
    )


def _continuation(
    ownership: ControlOperationOwnership,
    attempts: object,
    runner: RecordingRunner,
    *,
    state: OrchestratorState,
    in_flight: ContinuationsInFlight | None = None,
    runs: ContinuationRuns | None = None,
    pull_requests: RecordingPullRequests,
) -> ControlContinuation:
    """A continuation over caller-chosen stores, for the outage directions."""
    config = Config()
    config.repo = REPO
    return ControlContinuation(
        ownership,
        ContinuationLiveTruth(
            attempts,  # type: ignore[arg-type]
            pr_pending_label=PR_PENDING,
            in_flight=in_flight if in_flight is not None else ContinuationsInFlight(),
            runs=runs if runs is not None else ContinuationRuns(_NoWorktrees()),  # type: ignore[arg-type]
        ),
        runner,  # type: ignore[arg-type]
        ContinuationReworkHandoff(
            state=state,
            pull_requests=pull_requests,  # type: ignore[arg-type]
            budget=ReworkCycleBudget(
                LabelManager(config), max_rework_cycles=config.max_rework_cycles
            ),
            events=NullEventSink(),
        ),
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return _engine(tmp_path)


def _failed_candidate(
    engine: Engine,
    *actions: RequestedAction,
    head_sha: str = SHA_A,
) -> Attempt:
    """A candidate whose publication failed and whose intent was recorded."""
    return engine.attempts.update(
        _attempt_key(head_sha),
        lambda attempt: attempt.with_completed_evaluation(
            _receipt(head_sha)
        ).with_continuation_descriptor(_descriptor(*actions)),
    )


def _exhausted(
    engine: Engine,
    *actions: RequestedAction,
    head_sha: str = SHA_A,
) -> Attempt:
    """The #297 shape: publication non-PASS, and the same-SHA allowance spent.

    Exactly what #293 left behind — two durable non-PASS publication
    evaluations against one commit, with ``revalidation_budget_used=1``.
    """
    _failed_candidate(engine, *actions, head_sha=head_sha)
    return engine.attempts.update(
        _attempt_key(head_sha),
        lambda attempt: attempt.with_revalidation_reserved().with_completed_evaluation(
            _receipt(head_sha)
        ),
    )


def _with_validation_record(engine: Engine, path: str) -> Attempt:
    """The candidate after the gate recorded where its evidence landed."""
    return engine.attempts.update(
        _attempt_key(),
        lambda attempt: replace(attempt, validation_record_path=path),
    )


def _active_session(engine: Engine, issue: Issue) -> Session:
    """A running session for ``issue``. Only its issue number is under test."""
    worktree = engine.root / f"worktree-{issue.number}"
    worktree.mkdir(parents=True, exist_ok=True)
    return Session(
        key=SessionKey(issue=issue.key, task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=engine.root / "prompt.md"),
        terminal_id=f"issue-{issue.number}",
        worktree_path=worktree,
        branch_name=f"{issue.number}-continuation-lineage",
        run_assets=make_session_run_assets(
            worktree, session_name=f"issue-{issue.number}"
        ),
    )


def _open_pr(
    *,
    number: int = 294,
    labels: list[str] | None = None,
    branch: str = f"{ISSUE_NUMBER}-continuation-lineage",
) -> PRInfo:
    """The open PR the candidate is backed by, as GitHub reports it."""
    return PRInfo(
        number=number,
        title=f"#{ISSUE_NUMBER}: the candidate under correction",
        url=f"https://example.test/{REPO}/pull/{number}",
        branch=branch,
        body="",
        state="open",
        labels=labels if labels is not None else [],
    )


def _passed(engine: Engine, head_sha: str = SHA_A) -> Attempt:
    """The same candidate after #139 spent its allowance and the gate passed."""
    return engine.attempts.update(
        _attempt_key(head_sha),
        lambda attempt: attempt.with_revalidation_reserved().with_completed_evaluation(
            _receipt(head_sha, verdict=ValidationVerdict.PASSED)
        ),
    )


def _settled(
    engine: Engine,
    head_sha: str = SHA_A,
    *,
    pr_url: str | None = "https://example.test/owner/repo/pull/7",
) -> Attempt:
    """The candidate after its own run recorded what it produced."""
    settlement = (
        ContinuationSettlement(
            kind=ContinuationSettlementKind.PULL_REQUEST_OPENED,
            settled_at="2026-08-19T02:00:00Z",
            pr_url=pr_url,
        )
        if pr_url is not None
        else ContinuationSettlement(
            kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
            settled_at="2026-08-19T02:00:00Z",
        )
    )
    return engine.attempts.update(
        _attempt_key(head_sha),
        lambda attempt: attempt.with_continuation_settlement(settlement),
    )


def _runs_spent(
    engine: Engine,
    count: int = CONTINUATION_RUN_ALLOWANCE,
    head_sha: str = SHA_A,
) -> Attempt:
    """The candidate after its continuation has opened ``count`` runs."""
    attempt = engine.attempts.update(_attempt_key(head_sha), lambda a: a)
    for _index in range(count):
        attempt = engine.attempts.update(
            _attempt_key(head_sha),
            lambda a: a.with_continuation_run_reserved(),
        )
    return attempt


def _stub_run(engine: Engine) -> ContinuationRun:
    """A run this engine is carrying. Only its presence is under test here."""
    worktree = engine.root / "continuation-worktree"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return ContinuationRun(
        worktree=worktree,
        agent_label="agent:backend",
        assets=SessionRunAssets.from_paths(
            session_name=f"continuation-{ISSUE_NUMBER}",
            run_id="run-1",
            worktree_path=worktree,
            run_dir=run_dir,
            terminal_recording_path=run_dir / "terminal.cast",
            manifest_path=run_dir / "manifest.json",
            started_at="2026-08-19T00:00:00Z",
        ),
        completion_path=".issue-orchestrator/sessions/run-1/completion.json",
    )


def _reviewed(
    engine: Engine, outcome: ReviewVerdictOutcome, head_sha: str = SHA_A
) -> Attempt:
    return engine.attempts.update(
        _attempt_key(head_sha),
        lambda attempt: attempt.with_continuation_review_verdict(
            BoundReviewVerdict(
                verdict=outcome,
                reviewed_sha=head_sha,
                decided_at="2026-08-19T01:00:00Z",
                completed_rounds=1,
            )
        ),
    )


# ======================================================================
# 1. No synthesized intent
# ======================================================================


class TestNoSynthesizedIntent:
    """No descriptor ⇒ no continuation, whatever else is true."""

    def test_a_failed_candidate_without_a_descriptor_is_never_live(
        self, engine: Engine
    ) -> None:
        engine.attempts.update(
            _attempt_key(),
            lambda attempt: attempt.with_completed_evaluation(_receipt()),
        )

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert result.operations == ()
        assert result.exclusions.entries == ()

    def test_a_passing_candidate_without_a_descriptor_is_never_live(
        self, engine: Engine
    ) -> None:
        engine.attempts.update(
            _attempt_key(),
            lambda attempt: attempt.with_completed_evaluation(
                _receipt(verdict=ValidationVerdict.PASSED)
            ),
        )

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert result.operations == ()

    def test_labels_cannot_substitute_for_recorded_intent(
        self, engine: Engine
    ) -> None:
        """A board that looks exactly like a PR-bound issue still yields nothing."""
        engine.attempts.update(
            _attempt_key(),
            lambda attempt: attempt.with_completed_evaluation(_receipt()),
        )

        result = engine.continuation.reconcile(
            [_issue("agent:backend", "validation-failed", "needs-code-review")]
        )

        assert result.operations == ()

    def test_the_writer_records_nothing_when_the_gate_passed(
        self, engine: Engine
    ) -> None:
        """A passing candidate is the ordinary path's, not the continuation's."""
        writer = ContinuationDescriptorWriter(engine.attempts)

        written = writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(passed=True),
        )

        assert written is None
        assert engine.attempts.for_key(_attempt_key()) is None


# ======================================================================
# 2. PASS stays owned
# ======================================================================


class TestPassStaysOwned:
    """Recording PASS(A) must not release K before review settles."""

    def test_pass_keeps_the_operation_live(self, engine: Engine) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        board = [_issue("agent:backend")]
        engine.continuation.reconcile(board)

        _passed(engine)
        result = engine.continuation.reconcile(board)

        assert [op.phase for op in result.operations] == [
            ContinuationPhase.PASS_PENDING_REVIEW
        ]
        assert result.exclusions.owns(_operation_key())

    def test_pass_keeps_the_issue_out_of_the_queue(self, engine: Engine) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        issue = _issue("agent:backend")

        engine.continuation.reconcile([issue])

        assert (
            engine.queue_cache().evaluate_issue(issue)
            is QueueMutationStatus.REJECTED_EXCLUDED
        )


# ======================================================================
# 3. Intent honoured
# ======================================================================


class TestIntentHonoured:
    """CREATE_PR absent ⇒ no PR, and ownership settles after APPROVED(A)."""

    def test_approval_without_create_pr_settles_immediately(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.PUSH_BRANCH)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.APPROVED)

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert result.operations == ()
        assert result.exclusions.entries == ()

    def test_approval_with_create_pr_waits_for_the_pull_request(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.APPROVED)

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert [op.phase for op in result.operations] == [
            ContinuationPhase.APPROVED_PENDING_PR
        ]

    def test_a_no_pr_intent_never_strands_ownership(self, engine: Engine) -> None:
        """The settlement must not wait for a pr-pending that cannot arrive."""
        _failed_candidate(engine, RequestedAction.PUSH_BRANCH)
        _passed(engine)
        board = [_issue("agent:backend")]
        engine.continuation.reconcile(board)
        assert engine.ownership.exclusions.owns(_operation_key())

        _reviewed(engine, ReviewVerdictOutcome.APPROVED)
        engine.continuation.reconcile(board)

        assert engine.ownership.exclusions.entries == ()


# ======================================================================
# 4. Exact SHA
# ======================================================================


class TestExactSha:
    """A' never inherits A's descriptor, ownership or review."""

    def test_a_prime_does_not_inherit_the_descriptor(self, engine: Engine) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        engine.attempts.update(
            _attempt_key(SHA_A_PRIME),
            lambda attempt: attempt.with_completed_evaluation(_receipt(SHA_A_PRIME)),
        )

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert [op.key.head_sha for op in result.operations] == [SHA_A]

    def test_a_prime_does_not_inherit_the_review_verdict(self) -> None:
        with pytest.raises(ValueError, match="own commit"):
            Attempt(
                key=_attempt_key(SHA_A_PRIME),
                continuation_review_verdict=BoundReviewVerdict(
                    verdict=ReviewVerdictOutcome.APPROVED,
                    reviewed_sha=SHA_A,
                    decided_at="2026-08-19T01:00:00Z",
                    completed_rounds=1,
                ),
            )

    def test_the_operation_key_is_bound_to_the_exact_commit(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert result.operations[0].key == _operation_key(SHA_A)
        assert result.operations[0].key != _operation_key(SHA_A_PRIME)

    def test_filing_a_newer_candidates_intent_supersedes_the_older(
        self, engine: Engine
    ) -> None:
        """One issue offers one candidate, so two live operations cannot form."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        writer = ContinuationDescriptorWriter(engine.attempts)

        # The gate files its verdict receipt and the writer copies the intent,
        # both at the same seam and both for the newer candidate.
        engine.attempts.update(
            _attempt_key(SHA_A_PRIME),
            lambda attempt: attempt.with_completed_evaluation(_receipt(SHA_A_PRIME)),
        )
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A_PRIME),
        )

        result = engine.continuation.reconcile([_issue("agent:backend")])
        assert [op.key.head_sha for op in result.operations] == [SHA_A_PRIME]
        older = engine.attempts.for_key(_attempt_key(SHA_A))
        assert older is not None
        assert older.continuation_descriptor is None
        # Superseding intent must not touch the evidence #139 exists to keep.
        assert older.completed_evaluations == (_receipt(SHA_A),)


# ======================================================================
# 5. No coder work turn between PASS(A) and the first review
# ======================================================================


class TestNoCoderWorkTurn:
    """Nothing may make the issue launchable between PASS(A) and review."""

    def test_the_issue_is_excluded_across_the_whole_pass_to_review_window(
        self, engine: Engine
    ) -> None:
        """Every hydration from the recorded intent to the settled review
        refuses the issue, so no coder session can be planned in between."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        issue = _issue("agent:backend")
        cache = engine.queue_cache()

        for advance in (lambda: None, lambda: _passed(engine)):
            advance()
            engine.continuation.hydrate_queue(cache, [issue])
            assert engine.state.cached_queue_issues == []

        # Only the settled review reopens the lane, and only for the intent
        # that has nothing left to publish.
        _reviewed(engine, ReviewVerdictOutcome.CHANGES_REQUESTED)
        engine.continuation.hydrate_queue(cache, [issue])
        assert [i.number for i in engine.state.cached_queue_issues] == [ISSUE_NUMBER]

    def test_an_approved_pr_intent_still_holds_the_lane(
        self, engine: Engine
    ) -> None:
        """APPROVED(A) with CREATE_PR is not a handoff: the PR is still owed,
        and a coder turn here would move the candidate out from under it."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.APPROVED)
        issue = _issue("agent:backend")

        queue = engine.continuation.hydrate_queue(engine.queue_cache(), [issue])

        assert queue == []


class TestTheOwedPullRequestIsOwedExactlyOnce:
    """An intent held forever is not a held lane; it is a loop.

    ``pr-pending`` is written when a Session completes with a PR, when a scan
    finds one carrying the code-review label, or when the publish-retry route
    finalizes itself. The continuation goes through none of those, so without a
    settlement recorded by its own run ``APPROVED_PENDING_PR`` is re-derived
    live on every reconciliation and a full reviewer exchange runs again.
    """

    def test_an_approved_pr_intent_is_advanced_again_until_it_settles(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.APPROVED)
        issue = _issue("agent:backend")
        cache = engine.queue_cache()

        engine.continuation.hydrate_queue(cache, [issue])
        engine.continuation.hydrate_queue(cache, [issue])

        assert engine.runner.advanced == [
            (_operation_key(), ContinuationPhase.APPROVED_PENDING_PR),
            (_operation_key(), ContinuationPhase.APPROVED_PENDING_PR),
        ]

    def test_the_settlement_the_run_recorded_ends_the_operation(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.APPROVED)
        _settled(engine)
        issue = _issue("agent:backend")

        queue = engine.continuation.hydrate_queue(engine.queue_cache(), [issue])

        assert engine.runner.advanced == []
        assert engine.ownership.exclusions.entries == ()
        assert [i.number for i in queue] == [ISSUE_NUMBER]

    def test_a_settled_operation_stays_settled_across_a_restart(
        self, tmp_path: Path
    ) -> None:
        """The settlement is durable, so a restart cannot resurrect the loop."""
        first = _engine(tmp_path)
        _failed_candidate(first, RequestedAction.CREATE_PR)
        _passed(first)
        _reviewed(first, ReviewVerdictOutcome.APPROVED)
        _settled(first)

        restarted = _engine(tmp_path)
        restarted.continuation.hydrate_queue(
            restarted.queue_cache(), [_issue("agent:backend")]
        )

        assert restarted.runner.advanced == []
        assert restarted.ownership.exclusions.entries == ()

    def test_a_settlement_releases_a_lease_written_before_the_crash(
        self, tmp_path: Path
    ) -> None:
        first = _engine(tmp_path)
        _failed_candidate(first, RequestedAction.CREATE_PR)
        _passed(first)
        _reviewed(first, ReviewVerdictOutcome.APPROVED)
        first.continuation.hydrate_queue(first.queue_cache(), [_issue("agent:backend")])
        assert first.ownership.exclusions.owns(_operation_key())
        _settled(first)

        restarted = _engine(tmp_path)
        queue = restarted.continuation.hydrate_queue(
            restarted.queue_cache(), [_issue("agent:backend")]
        )

        assert restarted.ownership.exclusions.entries == ()
        assert [i.number for i in queue] == [ISSUE_NUMBER]


# ======================================================================
# 6. Ordering — reconciliation precedes every eligibility evaluation
# ======================================================================


class TestOrdering:
    """No eligibility verdict for a live key predates its exclusion."""

    def test_hydration_publishes_exclusions_before_evaluating(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        issue = _issue("agent:backend")

        queue = engine.continuation.hydrate_queue(engine.queue_cache(), [issue])

        assert queue == []
        assert engine.state.control_operation_exclusions.owns(_operation_key())

    def test_hydrating_a_single_issue_reconciles_over_the_whole_board(
        self, engine: Engine
    ) -> None:
        """A partial board must not free another issue's running operation."""
        other = _issue("agent:backend", number=150)
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        engine.attempts.update(
            AttemptKey(other.key, SHA_A_PRIME),
            lambda attempt: attempt.with_completed_evaluation(
                _receipt(SHA_A_PRIME)
            ).with_continuation_descriptor(_descriptor(RequestedAction.CREATE_PR)),
        )
        board = [_issue("agent:backend"), other]

        engine.continuation.hydrate_issues(
            engine.queue_cache(), [board[0]], board=board
        )

        owned = {key.issue_stable_id for key in engine.ownership.exclusions.owned}
        assert owned == {str(ISSUE_NUMBER), "150"}

    def test_reconciliation_happens_even_with_nothing_to_upsert(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        board = [_issue("agent:backend")]

        engine.continuation.hydrate_issues(engine.queue_cache(), [], board=board)

        assert engine.ownership.exclusions.owns(_operation_key())

    def test_execution_never_precedes_publication(self, engine: Engine) -> None:
        """When the runner is called, the exclusion is already in force."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        seen: list[bool] = []
        engine.runner.on_advance = lambda _r: seen.append(
            engine.state.control_operation_exclusions.owns(_operation_key())
        )

        engine.continuation.reconcile([_issue("agent:backend")])

        assert seen == [True]


# ======================================================================
# 7. Stale snapshot
# ======================================================================


class TestStaleSnapshot:
    """A snapshot derived before a newer claim cannot release it."""

    def test_a_claim_taken_before_derivation_is_never_released(
        self, engine: Engine
    ) -> None:
        """Derivation runs under the owner's lock, so a claim is either
        visible to it or strictly after the reconciliation it would race."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        board = [_issue("agent:backend")]
        claimed = engine.ownership.claim(_operation_key())
        assert claimed.owned

        engine.continuation.reconcile(board)

        assert engine.ownership.exclusions.owns(_operation_key())

    def test_derivation_reads_durable_truth_written_before_the_claim(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """The falsifying order — snapshot, then K becomes live, then a stale
        reconcile — cannot be expressed: the live set is derived inside the
        same lock the claim takes, so it always sees the durable write."""
        board = [_issue("agent:backend")]
        derived: list[tuple[str, ...]] = []

        def derive() -> tuple[ControlOperationKey, ...]:
            # Standing in for the caller that becomes live between snapshot and
            # reconcile: the durable write lands, then the set is derived.
            _failed_candidate(engine, RequestedAction.CREATE_PR)
            reading = ContinuationLiveTruth(
                engine.attempts,
                pr_pending_label=PR_PENDING,
                in_flight=engine.in_flight,
                runs=engine.runs,
            ).read(board)
            derived.append(tuple(key.head_sha for key in reading.keys))
            return reading.keys

        engine.ownership.claim(_operation_key())
        engine.ownership.reconcile_derived(derive)

        assert derived == [(SHA_A,)]
        assert engine.ownership.exclusions.owns(_operation_key())


# ======================================================================
# 8. Store outage
# ======================================================================


class TestStoreOutage:
    """Unreadable state yields UNAVAILABLE or a held projection, never free."""

    def test_an_unreadable_attempt_store_keeps_the_standing_projection(
        self, tmp_path: Path
    ) -> None:
        readable = _engine(tmp_path)
        _failed_candidate(readable, RequestedAction.CREATE_PR)
        board = [_issue("agent:backend")]
        readable.continuation.reconcile(board)
        assert readable.ownership.exclusions.owns(_operation_key())

        blind_prs = RecordingPullRequests(prs={ISSUE_NUMBER: _open_pr()})
        blind = _continuation(
            readable.ownership,
            UnreadableAttempts(),
            readable.runner,
            state=readable.state,
            pull_requests=blind_prs,
        )
        result = blind.reconcile(board)

        assert result.readable is False
        assert result.exclusions.owns(_operation_key())
        # Ignorance admits nothing either: an unreadable record cannot say the
        # continuation exited, so the handoff never even asks GitHub.
        assert blind_prs.reads == []
        assert readable.state.discovered_reworks == []

    def test_an_unreadable_attempt_store_advances_nothing(
        self, tmp_path: Path
    ) -> None:
        readable = _engine(tmp_path)
        _failed_candidate(readable, RequestedAction.CREATE_PR)
        runner = RecordingRunner()
        blind = _continuation(
            readable.ownership,
            UnreadableAttempts(),
            runner,
            state=readable.state,
            pull_requests=RecordingPullRequests(),
        )

        blind.reconcile([_issue("agent:backend")])

        assert runner.advanced == []

    def test_an_unreadable_ownership_store_reads_unavailable_not_free(
        self, tmp_path: Path
    ) -> None:
        """The other half of the outage: durable truth is readable and says the
        operation is live, but the lease store cannot say who holds it."""
        engine = _engine(tmp_path)
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        blind_ownership = ControlOperationOwnership(
            engine.state, _UnreadableOwnershipStore()
        )
        continuation = _continuation(
            blind_ownership,
            engine.attempts,
            engine.runner,
            state=engine.state,
            in_flight=engine.in_flight,
            runs=engine.runs,
            pull_requests=engine.pull_requests,
        )
        issue = _issue("agent:backend")

        result = continuation.reconcile([issue])

        assert result.exclusions.unavailable == (_operation_key(),)
        assert result.owned == ()
        assert (
            engine.queue_cache().evaluate_issue(issue)
            is QueueMutationStatus.REJECTED_EXCLUDED
        )

    def test_an_unreadable_attempt_store_never_frees_the_queue(
        self, tmp_path: Path
    ) -> None:
        readable = _engine(tmp_path)
        _failed_candidate(readable, RequestedAction.CREATE_PR)
        issue = _issue("agent:backend")
        readable.continuation.reconcile([issue])

        blind = _continuation(
            readable.ownership,
            UnreadableAttempts(),
            readable.runner,
            state=readable.state,
            pull_requests=RecordingPullRequests(),
        )
        blind.hydrate_queue(readable.queue_cache(), [issue])

        assert readable.state.cached_queue_issues == []


# ======================================================================
# 9. Changes-requested handoff
# ======================================================================


class TestChangesRequestedHandoff:
    """CHANGES_REQUESTED(A) transfers before rework, and without waiting for A'."""

    def test_the_verdict_alone_releases_ownership(self, engine: Engine) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        board = [_issue("agent:backend")]
        engine.continuation.reconcile(board)
        assert engine.ownership.exclusions.owns(_operation_key())

        _reviewed(engine, ReviewVerdictOutcome.CHANGES_REQUESTED)
        result = engine.continuation.reconcile(board)

        assert result.operations == ()
        assert engine.ownership.exclusions.entries == ()

    def test_the_handoff_does_not_wait_for_a_prime(self, engine: Engine) -> None:
        """The branch never moves in this test. An implementation that waited
        for A' would hold the issue forever, and rework could never create A'
        because ownership excludes it."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.CHANGES_REQUESTED)
        issue = _issue("agent:backend")

        queue = engine.continuation.hydrate_queue(engine.queue_cache(), [issue])

        assert [i.number for i in queue] == [ISSUE_NUMBER]

    def test_ordinary_rework_becomes_eligible_only_after_the_release(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        issue = _issue("agent:backend")
        cache = engine.queue_cache()

        engine.continuation.hydrate_queue(cache, [issue])
        assert engine.state.cached_queue_issues == []

        _reviewed(engine, ReviewVerdictOutcome.CHANGES_REQUESTED)
        engine.continuation.hydrate_queue(cache, [issue])
        assert [i.number for i in engine.state.cached_queue_issues] == [ISSUE_NUMBER]


# ======================================================================
# 10. No dual-live handoff
# ======================================================================


class TestNoDualLiveHandoff:
    """Continuation and ordinary rework cannot both be launchable."""

    def test_the_operation_leaves_live_truth_in_the_same_pass_the_queue_opens(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.CHANGES_REQUESTED)
        issue = _issue("agent:backend")

        result = engine.continuation.reconcile([issue])
        queue = engine.queue_cache()
        queue.replace_from_refresh([issue])

        assert result.owned == ()
        assert engine.runner.advanced == []
        assert [i.number for i in engine.state.cached_queue_issues] == [ISSUE_NUMBER]

    def test_a_live_operation_is_never_advanced_and_queued_together(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        issue = _issue("agent:backend")

        queue = engine.continuation.hydrate_queue(engine.queue_cache(), [issue])

        assert engine.runner.advanced_keys == [_operation_key()]
        assert queue == []


# ======================================================================
# 11. Non-PASS clean return
# ======================================================================


class TestNonPassCleanReturn:
    """Exhaustion releases ownership with the evidence history intact."""

    def test_a_spent_allowance_with_a_non_pass_latest_is_exhausted(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        engine.attempts.update(
            _attempt_key(),
            lambda attempt: attempt.with_revalidation_reserved().with_completed_evaluation(
                _receipt()
            ),
        )
        issue = _issue("agent:backend")

        queue = engine.continuation.hydrate_queue(engine.queue_cache(), [issue])

        assert [i.number for i in queue] == [ISSUE_NUMBER]
        assert engine.ownership.exclusions.entries == ()

    def test_exhaustion_keeps_every_evaluation_on_record(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        engine.attempts.update(
            _attempt_key(),
            lambda attempt: attempt.with_revalidation_reserved().with_completed_evaluation(
                _receipt()
            ),
        )

        engine.continuation.reconcile([_issue("agent:backend")])

        attempt = engine.attempts.for_key(_attempt_key())
        assert attempt is not None
        assert len(attempt.publication_evaluations) == 2
        assert attempt.continuation_descriptor is not None


class TestAnInFlightRevalidationIsNotExhaustion:
    """#139's allowance is a START budget, so the two look identical durably.

    From the instant ``revalidate`` reserves the allowance until the instant the
    gate files its verdict, the record reads ``allowance spent, latest
    publication evaluation still the failure`` — the exact facts a revalidation
    that ran and failed leaves behind. Deriving from them alone releases the
    lease of a running operation and re-admits ordinary work onto the issue.
    """

    def _reserved_but_unreported(self, engine: Engine) -> None:
        """The durable state during a gate run: spent, nothing new appended."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        engine.attempts.update(
            _attempt_key(), lambda attempt: attempt.with_revalidation_reserved()
        )

    def test_the_lane_stays_held_while_the_gate_runs(self, engine: Engine) -> None:
        self._reserved_but_unreported(engine)
        engine.in_flight.claim(_operation_key())

        queue = engine.continuation.hydrate_queue(
            engine.queue_cache(), [_issue("agent:backend")]
        )

        assert queue == []
        assert engine.ownership.exclusions.owns(_operation_key())

    def test_a_second_reconciliation_starts_no_second_run(
        self, engine: Engine
    ) -> None:
        self._reserved_but_unreported(engine)
        engine.in_flight.claim(_operation_key())
        cache = engine.queue_cache()

        engine.continuation.hydrate_queue(cache, [_issue("agent:backend")])
        engine.continuation.hydrate_queue(cache, [_issue("agent:backend")])

        assert engine.runner.advanced == [
            (_operation_key(), ContinuationPhase.EXECUTING),
            (_operation_key(), ContinuationPhase.EXECUTING),
        ]

    def test_the_same_facts_release_once_the_run_ends(self, engine: Engine) -> None:
        """A verdict the gate never recorded must still reach a terminal answer."""
        self._reserved_but_unreported(engine)
        engine.in_flight.claim(_operation_key())
        cache = engine.queue_cache()
        engine.continuation.hydrate_queue(cache, [_issue("agent:backend")])

        engine.in_flight.release(_operation_key())
        queue = engine.continuation.hydrate_queue(cache, [_issue("agent:backend")])

        assert [i.number for i in queue] == [ISSUE_NUMBER]
        assert engine.ownership.exclusions.entries == ()

    def test_a_crash_mid_run_exhausts_rather_than_pinning_the_issue(
        self, tmp_path: Path
    ) -> None:
        """The claim cannot outlive its engine, so #139 stays fail-closed."""
        first = _engine(tmp_path)
        self._reserved_but_unreported(first)
        first.in_flight.claim(_operation_key())
        first.continuation.hydrate_queue(
            first.queue_cache(), [_issue("agent:backend")]
        )
        assert first.ownership.exclusions.owns(_operation_key())

        restarted = _engine(tmp_path)
        queue = restarted.continuation.hydrate_queue(
            restarted.queue_cache(), [_issue("agent:backend")]
        )

        assert [i.number for i in queue] == [ISSUE_NUMBER]
        assert restarted.ownership.exclusions.entries == ()


class TestTheContinuationsRetryIsBounded:
    """The re-entry re-runs a reviewer/coder pair; it needs a bound (F4).

    A terminal run that discharged nothing changes no durable fact, so the same
    phase is derived again and another run opens. The exchange's own no-progress
    budget cannot catch it: that budget lives under the worktree every closed
    run removes, so each retry starts it afresh.
    """

    def test_a_candidate_out_of_runs_returns_to_ordinary_rework(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _runs_spent(engine)

        queue = engine.continuation.hydrate_queue(
            engine.queue_cache(), [_issue("agent:backend")]
        )

        assert [i.number for i in queue] == [ISSUE_NUMBER]
        assert engine.ownership.exclusions.entries == ()
        assert engine.runner.advanced == []

    def test_the_candidate_returns_with_its_evidence_intact(
        self, engine: Engine
    ) -> None:
        """The clean return ``EXHAUSTED`` gives the revalidation half."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _reviewed(engine, ReviewVerdictOutcome.APPROVED)
        _runs_spent(engine)

        engine.continuation.reconcile([_issue("agent:backend")])

        attempt = engine.attempts.for_key(_attempt_key())
        assert attempt is not None
        assert attempt.continuation_descriptor is not None
        assert attempt.continuation_review_verdict is not None
        assert len(attempt.publication_evaluations) == 2

    def test_one_run_left_still_holds_the_lane(self, engine: Engine) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _runs_spent(engine, CONTINUATION_RUN_ALLOWANCE - 1)

        queue = engine.continuation.hydrate_queue(
            engine.queue_cache(), [_issue("agent:backend")]
        )

        assert queue == []
        assert engine.runner.advanced == [
            (_operation_key(), ContinuationPhase.PASS_PENDING_REVIEW)
        ]

    def test_a_spent_allowance_whose_run_is_still_open_holds_the_lane(
        self, engine: Engine
    ) -> None:
        """The allowance is spent when a run OPENS, so mid-run it reads spent."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)
        _runs_spent(engine)
        engine.runs.opened(_operation_key(), _stub_run(engine))

        queue = engine.continuation.hydrate_queue(
            engine.queue_cache(), [_issue("agent:backend")]
        )

        assert queue == []
        assert engine.ownership.exclusions.owns(_operation_key())

    def test_a_crash_mid_run_returns_the_candidate_rather_than_pinning_it(
        self, tmp_path: Path
    ) -> None:
        """The open run died with the engine; the spent allowance did not."""
        first = _engine(tmp_path)
        _failed_candidate(first, RequestedAction.CREATE_PR)
        _passed(first)
        _runs_spent(first)
        first.runs.opened(_operation_key(), _stub_run(first))
        first.continuation.hydrate_queue(
            first.queue_cache(), [_issue("agent:backend")]
        )
        assert first.ownership.exclusions.owns(_operation_key())

        restarted = _engine(tmp_path)
        queue = restarted.continuation.hydrate_queue(
            restarted.queue_cache(), [_issue("agent:backend")]
        )

        assert [i.number for i in queue] == [ISSUE_NUMBER]
        assert restarted.ownership.exclusions.entries == ()


# ======================================================================
# 12. No second scheduler truth
# ======================================================================


class TestNoSecondSchedulerTruth:
    """A raw lease row never drives liveness or eligibility."""

    def test_a_surviving_lease_with_no_recorded_intent_excludes_nothing(
        self, tmp_path: Path
    ) -> None:
        before = _engine(tmp_path)
        _failed_candidate(before, RequestedAction.CREATE_PR)
        before.continuation.reconcile([_issue("agent:backend")])

        # The operation settles into a PR; the lease row outlives it.
        after = _engine(tmp_path)
        settled_board = [_issue("agent:backend", PR_PENDING)]
        queue = after.continuation.hydrate_queue(after.queue_cache(), settled_board)

        assert [i.number for i in queue] == [ISSUE_NUMBER]
        assert after.ownership.exclusions.entries == ()

    def test_the_queue_reads_only_the_published_projection(
        self, tmp_path: Path
    ) -> None:
        """A lease written by another engine excludes nothing until a
        reconciliation matches it to an operation the caller declares live."""
        engine = _engine(tmp_path)
        peer_store = SqliteControlOperationOwnershipStore(tmp_path / STORE_FILENAME)
        peer_store.reserve_control_operation(_operation_key(), holder="peer")
        issue = _issue("agent:backend")

        assert (
            engine.queue_cache().evaluate_issue(issue) is QueueMutationStatus.ACCEPTED
        )


# ======================================================================
# 13. #139 remains the sole revalidation admission owner
# ======================================================================


class TestSingleAdmissionOwner:
    """Nothing here re-decides what #139 decides."""

    def test_retry_pending_is_derived_without_deciding_admission(
        self, engine: Engine
    ) -> None:
        """The phase says a retry is PENDING. Whether one may start — contract
        match, allowance, reserve-before-execute — stays with #139."""
        _failed_candidate(engine, RequestedAction.CREATE_PR)

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert [op.phase for op in result.operations] == [
            ContinuationPhase.RETRY_PENDING
        ]

    def test_a_spent_allowance_produces_no_second_retry(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        engine.attempts.update(
            _attempt_key(),
            lambda attempt: attempt.with_revalidation_reserved().with_completed_evaluation(
                _receipt()
            ),
        )

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert result.operations == ()
        assert engine.runner.advanced == []

    def test_the_allowance_counter_is_never_written_by_the_continuation(
        self, engine: Engine
    ) -> None:
        _failed_candidate(engine, RequestedAction.CREATE_PR)

        engine.continuation.reconcile([_issue("agent:backend")])

        attempt = engine.attempts.for_key(_attempt_key())
        assert attempt is not None
        assert attempt.revalidation_budget_used == 0


# ======================================================================
# 14. Restart matrix
# ======================================================================


class TestRestartMatrix:
    """Every load-bearing crash window resolves from durable truth alone."""

    @pytest.mark.parametrize(
        ("arrange", "labels", "expected"),
        [
            pytest.param(
                lambda e: None,
                (),
                ContinuationPhase.RETRY_PENDING,
                id="ownership-claimed-revalidation-not-started",
            ),
            pytest.param(
                lambda e: e.attempts.update(
                    _attempt_key(),
                    lambda a: a.with_revalidation_reserved(),
                ),
                (),
                ContinuationPhase.EXHAUSTED,
                id="allowance-reserved-gate-incomplete",
            ),
            pytest.param(
                _passed,
                (),
                ContinuationPhase.PASS_PENDING_REVIEW,
                id="pass-durable-reviewer-not-started",
            ),
            pytest.param(
                lambda e: (
                    _passed(e),
                    _reviewed(e, ReviewVerdictOutcome.APPROVED),
                ),
                (),
                ContinuationPhase.APPROVED_PENDING_PR,
                id="approved-pr-not-created",
            ),
            pytest.param(
                lambda e: (
                    _passed(e),
                    _reviewed(e, ReviewVerdictOutcome.APPROVED),
                ),
                (PR_PENDING,),
                ContinuationPhase.SETTLED_PR,
                id="pr-created-release-incomplete",
            ),
            pytest.param(
                lambda e: (
                    _passed(e),
                    _reviewed(e, ReviewVerdictOutcome.CHANGES_REQUESTED),
                ),
                (),
                ContinuationPhase.EXIT_TO_REWORK,
                id="changes-requested-mid-handoff",
            ),
        ],
    )
    def test_a_restart_derives_the_phase_from_durable_truth(
        self,
        tmp_path: Path,
        arrange: Callable[[Engine], object],
        labels: tuple[str, ...],
        expected: ContinuationPhase,
    ) -> None:
        before = _engine(tmp_path)
        _failed_candidate(before, RequestedAction.CREATE_PR)
        arrange(before)
        before.continuation.reconcile([_issue("agent:backend")])

        after = _engine(tmp_path)
        board = [_issue("agent:backend", *labels)]
        result = after.continuation.reconcile(board)

        if expected.live:
            assert [op.phase for op in result.operations] == [expected]
            assert after.ownership.exclusions.owns(_operation_key())
        else:
            assert result.operations == ()
            assert after.ownership.exclusions.entries == ()

    def test_a_restart_adopts_the_lease_without_a_session_walk(
        self, tmp_path: Path
    ) -> None:
        before = _engine(tmp_path)
        _failed_candidate(before, RequestedAction.CREATE_PR)
        _passed(before)
        before.continuation.reconcile([_issue("agent:backend")])

        after = _engine(tmp_path)
        assert after.state.active_sessions == []
        assert after.state.session_history == []

        result = after.continuation.reconcile([_issue("agent:backend")])

        assert result.exclusions.owns(_operation_key())
        assert result.owned[0].phase is ContinuationPhase.PASS_PENDING_REVIEW

    def test_the_descriptor_survives_a_restart(self, tmp_path: Path) -> None:
        before = _engine(tmp_path)
        writer = ContinuationDescriptorWriter(before.attempts)
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        after = _engine(tmp_path)
        reloaded = after.attempts.for_key(_attempt_key())

        assert reloaded is not None
        descriptor = reloaded.continuation_descriptor
        assert descriptor is not None
        assert descriptor.requested_actions == (RequestedAction.CREATE_PR,)
        assert descriptor.implementation == "what the agent claimed to build"
        assert descriptor.matches_contract(
            suite=ValidationGateKind.PUBLISH.suite,
            command=PUBLISH_COMMAND,
            profile=PROFILE,
        )


# ======================================================================
# 16. The EXHAUSTED PR-backed handoff (#297)
# ======================================================================


class TestExhaustedPrBackedHandoffAdmitsRework:
    """``EXHAUSTED`` on an open PR admits ordinary rework, in this process.

    #296 measured the gap exactly: reconciliation released the lease and
    produced nothing, so a PR-backed candidate that failed canonical
    publication validation and spent its same-SHA allowance sat stranded while
    the ordinary rework lane sat idle beside it. Every direction below is one
    of the issue's acceptance clauses, and the first is the mutation proof:
    remove the producer and it is the one that fails.
    """

    def _board(self) -> list[Issue]:
        return [_issue("agent:backend")]

    def _exhausted_pr_backed(
        self, engine: Engine, *, pr: PRInfo | None = None
    ) -> PRInfo:
        open_pr = pr if pr is not None else _open_pr()
        engine.pull_requests.prs[ISSUE_NUMBER] = open_pr
        _exhausted(engine, RequestedAction.CREATE_PR)
        return open_pr

    # -- 1. Same-process handoff -----------------------------------------

    def test_an_exhausted_pr_backed_candidate_admits_exactly_one_rework(
        self, engine: Engine
    ) -> None:
        pr = self._exhausted_pr_backed(engine)

        result = engine.continuation.reconcile(self._board())

        assert result.owned == ()
        assert result.exclusions.entries == ()
        assert len(engine.state.discovered_reworks) == 1
        rework = engine.state.discovered_reworks[0]
        assert rework.issue_number == ISSUE_NUMBER
        assert rework.pr_number == pr.number
        assert rework.source == CONTINUATION_EXIT_SOURCE
        assert rework.agent_type == "agent:backend"

    def test_the_derived_phase_really_is_exhausted(self, engine: Engine) -> None:
        """The premise of the whole direction, stated rather than assumed."""
        self._exhausted_pr_backed(engine)
        attempt = engine.attempts.for_key(_attempt_key())

        assert attempt is not None
        assert attempt.revalidation_allowance_available is False
        assert attempt.publication_validation_passed is False
        assert len(attempt.publication_evaluations) == 2

    # -- 2. Same lineage -------------------------------------------------

    def test_the_correction_targets_the_open_prs_own_branch(
        self, engine: Engine
    ) -> None:
        pr = self._exhausted_pr_backed(engine)

        engine.continuation.reconcile(self._board())

        rework = engine.state.discovered_reworks[0]
        assert rework.branch_name == pr.branch
        assert rework.pr_number == pr.number
        # No fresh ordinary coding session: the handoff files a rework fact and
        # nothing else, and the queue owner is untouched by it.
        assert engine.state.active_sessions == []
        assert engine.state.session_history == []

    def test_a_pr_on_another_branch_is_not_this_candidates_lineage(
        self, engine: Engine
    ) -> None:
        """The open-PR owner's own scoping decides identity, not this module."""
        engine.pull_requests.prs[ISSUE_NUMBER] = _open_pr(
            number=999, branch="some-other-attempt"
        )
        _exhausted(engine, RequestedAction.CREATE_PR)

        engine.continuation.reconcile(self._board())

        # ``pr_matches_issue`` accepts the title reference, so this PR still
        # resolves — what matters is that the branch the rework targets is the
        # PR's own, never a branch this module invented.
        rework = engine.state.discovered_reworks[0]
        assert rework.branch_name == "some-other-attempt"
        assert rework.pr_number == 999

    # -- 3. Evidence handoff ---------------------------------------------

    def test_the_admitted_rework_carries_the_correction_evidence(
        self, engine: Engine
    ) -> None:
        pr = self._exhausted_pr_backed(engine)
        _with_validation_record(
            engine, ".issue-orchestrator/sessions/run-1/validation-record.json"
        )

        engine.continuation.reconcile(self._board())

        feedback = engine.state.discovered_reworks[0].feedback
        assert feedback is not None
        assert SHA_A in feedback
        assert PUBLISH_COMMAND in feedback
        assert ValidationVerdict.FAILED.value in feedback
        assert str(pr.number) in feedback
        assert pr.branch in feedback
        assert "validation-record.json" in feedback
        # The intent the agent recorded travels too, so the corrector knows
        # what the failed candidate was trying to be.
        assert "what the agent claimed to build" in feedback

    def test_missing_evidence_is_named_rather_than_omitted(
        self, engine: Engine
    ) -> None:
        self._exhausted_pr_backed(engine)

        engine.continuation.reconcile(self._board())

        feedback = engine.state.discovered_reworks[0].feedback
        assert feedback is not None
        assert "no validation record path was recorded" in feedback

    # -- 4. Existing budget only -----------------------------------------

    def test_the_next_cycle_comes_from_the_existing_cycle_owner(
        self, engine: Engine
    ) -> None:
        self._exhausted_pr_backed(
            engine, pr=_open_pr(labels=["rework-cycle-2"])
        )

        engine.continuation.reconcile(self._board())

        assert engine.state.discovered_reworks[0].rework_cycle == 3

    def test_a_spent_cycle_budget_escalates_instead_of_admitting(
        self, engine: Engine
    ) -> None:
        max_cycles = engine.config.max_rework_cycles
        self._exhausted_pr_backed(
            engine, pr=_open_pr(labels=[f"rework-cycle-{max_cycles}"])
        )

        engine.continuation.reconcile(self._board())

        assert engine.state.discovered_reworks == []
        assert len(engine.state.discovered_escalations) == 1
        escalation = engine.state.discovered_escalations[0]
        assert escalation.issue_number == ISSUE_NUMBER
        assert escalation.rework_cycle == max_cycles + 1

    def test_a_blocked_issue_is_not_handed_a_cycle(self, engine: Engine) -> None:
        self._exhausted_pr_backed(engine)

        engine.continuation.reconcile([_issue("agent:backend", "needs-human")])

        assert engine.state.discovered_reworks == []
        assert engine.state.discovered_escalations == []

    # -- 5. PR-backed shield preserved ------------------------------------

    def test_the_pr_backed_claim_survives_the_handoff(
        self, engine: Engine
    ) -> None:
        """#195's shield is untouched: the claim stands and the issue is not
        abandoned, before or after the rework is admitted."""
        issue = _issue("agent:backend")
        self._exhausted_pr_backed(engine)
        engine.state.session_history = [
            SessionHistoryEntry(
                issue_number=ISSUE_NUMBER,
                title=issue.title,
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=1,
                pr_url=f"https://example.test/{REPO}/pull/294",
            ),
            SessionHistoryEntry(
                issue_number=ISSUE_NUMBER,
                title=issue.title,
                agent_type="agent:backend",
                status="validation_failed",
                runtime_minutes=1,
            ),
        ]
        engine.state.cached_scope_issues = [issue]
        before = engine.queue_cache().abandoned_candidates()

        engine.continuation.reconcile([issue])
        after = engine.queue_cache().abandoned_candidates()

        assert len(engine.state.discovered_reworks) == 1
        assert before.issues == ()
        assert after.issues == ()
        assert all(
            entry.claim_released is False for entry in engine.state.session_history
        )

    # -- 6. No label authority --------------------------------------------

    def test_admission_survives_the_projection_being_removed(
        self, engine: Engine
    ) -> None:
        """``validation-failed`` / ``needs-rework`` are projections. Neither is
        present here, and the admission happens anyway, because it is derived
        from the continuation's durable facts and the open PR."""
        self._exhausted_pr_backed(engine)
        bare_issue = _issue("agent:backend")

        engine.continuation.reconcile([bare_issue])

        assert bare_issue.labels == ["agent:backend"]
        assert len(engine.state.discovered_reworks) == 1

    # -- 7. No duplication -------------------------------------------------

    def test_a_second_reconciliation_admits_nothing_further(
        self, engine: Engine
    ) -> None:
        self._exhausted_pr_backed(engine)
        board = self._board()

        engine.continuation.reconcile(board)
        engine.continuation.reconcile(board)
        engine.continuation.reconcile(board)

        assert len(engine.state.discovered_reworks) == 1

    def test_a_queued_rework_blocks_a_second_admission(
        self, engine: Engine
    ) -> None:
        """The planner has turned this tick's fact into a pending one; the
        discovered buffer is cleared. The claim still holds."""
        self._exhausted_pr_backed(engine)
        engine.state.pending_reworks = [
            PendingRework(
                issue_key=_issue_key(),
                agent_type="agent:backend",
                rework_cycle=1,
                issue_number=ISSUE_NUMBER,
                pr_number=294,
            )
        ]

        engine.continuation.reconcile(self._board())

        assert engine.state.discovered_reworks == []
        # The cheap refusal is asked BEFORE GitHub, so a re-derived exit that
        # nothing can act on costs no API call at all.
        assert engine.pull_requests.reads == []

    def test_a_pending_escalation_blocks_a_second_admission(
        self, engine: Engine
    ) -> None:
        max_cycles = engine.config.max_rework_cycles
        self._exhausted_pr_backed(
            engine, pr=_open_pr(labels=[f"rework-cycle-{max_cycles}"])
        )
        board = self._board()

        engine.continuation.reconcile(board)
        engine.continuation.reconcile(board)

        assert len(engine.state.discovered_escalations) == 1

    # -- 8. Restart idempotence -------------------------------------------

    def test_a_restart_before_admission_admits_exactly_one(
        self, tmp_path: Path
    ) -> None:
        prs = RecordingPullRequests(prs={ISSUE_NUMBER: _open_pr()})
        before = _engine(tmp_path, pull_requests=prs)
        _exhausted(before, RequestedAction.CREATE_PR)

        after = _engine(tmp_path, pull_requests=prs)
        after.continuation.reconcile([_issue("agent:backend")])

        assert before.state.discovered_reworks == []
        assert len(after.state.discovered_reworks) == 1
        assert after.state.discovered_reworks[0].rework_cycle == 1

    def test_a_restart_after_admission_reconstructs_the_same_cycle(
        self, tmp_path: Path
    ) -> None:
        """The cycle is a durable PR label, so a restart that loses the queue
        re-derives the SAME cycle rather than spending a second one."""
        prs = RecordingPullRequests(prs={ISSUE_NUMBER: _open_pr()})
        before = _engine(tmp_path, pull_requests=prs)
        _exhausted(before, RequestedAction.CREATE_PR)
        before.continuation.reconcile([_issue("agent:backend")])

        after = _engine(tmp_path, pull_requests=prs)
        after.continuation.reconcile([_issue("agent:backend")])

        assert len(after.state.discovered_reworks) == 1
        assert (
            after.state.discovered_reworks[0].rework_cycle
            == before.state.discovered_reworks[0].rework_cycle
        )

    def test_a_started_cycle_is_not_repeated_after_a_restart(
        self, tmp_path: Path
    ) -> None:
        """Once the rework launcher writes ``rework-cycle-1`` to the PR, the
        budget owner reads the spent cycle and the next one is 2 — one cycle
        per admission, counted by the label, across any number of processes."""
        prs = RecordingPullRequests(prs={ISSUE_NUMBER: _open_pr()})
        before = _engine(tmp_path, pull_requests=prs)
        _exhausted(before, RequestedAction.CREATE_PR)
        before.continuation.reconcile([_issue("agent:backend")])
        prs.prs[ISSUE_NUMBER] = _open_pr(labels=["rework-cycle-1"])

        after = _engine(tmp_path, pull_requests=prs)
        after.continuation.reconcile([_issue("agent:backend")])

        assert after.state.discovered_reworks[0].rework_cycle == 2

    # -- 9. Negative paths unchanged ---------------------------------------

    def test_a_non_pr_backed_exhausted_candidate_admits_nothing(
        self, engine: Engine
    ) -> None:
        _exhausted(engine, RequestedAction.CREATE_PR)

        engine.continuation.reconcile(self._board())

        assert engine.state.discovered_reworks == []
        assert engine.state.discovered_escalations == []

    def test_retry_pending_admits_nothing(self, engine: Engine) -> None:
        engine.pull_requests.prs[ISSUE_NUMBER] = _open_pr()
        _failed_candidate(engine, RequestedAction.CREATE_PR)

        result = engine.continuation.reconcile(self._board())

        assert result.owned[0].phase is ContinuationPhase.RETRY_PENDING
        assert engine.state.discovered_reworks == []
        assert engine.pull_requests.reads == []

    def test_pass_pending_review_admits_nothing(self, engine: Engine) -> None:
        engine.pull_requests.prs[ISSUE_NUMBER] = _open_pr()
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _passed(engine)

        result = engine.continuation.reconcile(self._board())

        assert result.owned[0].phase is ContinuationPhase.PASS_PENDING_REVIEW
        assert engine.state.discovered_reworks == []

    def test_a_settled_pr_admits_nothing(self, engine: Engine) -> None:
        engine.pull_requests.prs[ISSUE_NUMBER] = _open_pr()
        _failed_candidate(engine, RequestedAction.CREATE_PR)
        _settled(engine)

        engine.continuation.reconcile(self._board())

        assert engine.state.discovered_reworks == []
        assert engine.pull_requests.reads == []

    def test_an_awaiting_merge_board_admits_nothing(self, engine: Engine) -> None:
        engine.pull_requests.prs[ISSUE_NUMBER] = _open_pr()
        _exhausted(engine, RequestedAction.CREATE_PR)

        engine.continuation.reconcile([_issue("agent:backend", PR_PENDING)])

        assert engine.state.discovered_reworks == []
        assert engine.pull_requests.reads == []

    def test_an_active_session_blocks_admission(self, engine: Engine) -> None:
        issue = _issue("agent:backend")
        self._exhausted_pr_backed(engine)
        engine.state.active_sessions = [_active_session(engine, issue)]

        engine.continuation.reconcile([issue])

        assert engine.state.discovered_reworks == []
        assert engine.pull_requests.reads == []

    # -- 10. Mutation proof ------------------------------------------------

    def test_nothing_but_the_handoff_can_produce_this_rework(
        self, engine: Engine
    ) -> None:
        """Why removing the producer strands the candidate again.

        The exit is derived — live truth names it — and the ordinary lane
        cannot reach it: the ordinary producer scans PRs carrying
        ``needs-rework``, and this PR carries no label at all. So the handoff
        call in :meth:`ControlContinuation.reconcile` is the only thing between
        a derived exit and an admitted rework, which is exactly what #296
        measured the absence of.
        """
        pr = self._exhausted_pr_backed(engine)
        truth = ContinuationLiveTruth(
            engine.attempts,
            pr_pending_label=PR_PENDING,
            in_flight=engine.in_flight,
            runs=engine.runs,
        )

        reading = truth.read(self._board())

        assert reading.operations == ()
        assert [exit_.phase for exit_ in reading.rework_exits] == [
            ContinuationPhase.EXHAUSTED
        ]
        assert reading.rework_exits[0].key == _operation_key()
        assert pr.labels == []

    def test_a_handoff_over_no_open_pr_strands_the_candidate(
        self, tmp_path: Path
    ) -> None:
        """The pre-#297 engine, reconstructed: same release, same phase, no
        admission, because nothing produced one."""
        engine = _engine(tmp_path, pull_requests=RecordingPullRequests())
        _exhausted(engine, RequestedAction.CREATE_PR)

        result = engine.continuation.reconcile([_issue("agent:backend")])

        assert result.exclusions.entries == ()
        assert engine.state.discovered_reworks == []
