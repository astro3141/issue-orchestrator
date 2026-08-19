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
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.continuation_descriptor_writer import (
    ContinuationDescriptorWriter,
)
from issue_orchestrator.control.continuation_live_truth import (
    CONTINUATION_KIND,
    ContinuationLiveTruth,
    ContinuationReconciliation,
)
from issue_orchestrator.control.continuation_scheduling import ControlContinuation
from issue_orchestrator.control.control_operation_ownership import (
    ControlOperationOwnership,
)
from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.continuation_descriptor import ContinuationDescriptor
from issue_orchestrator.domain.continuation_phase import ContinuationPhase
from issue_orchestrator.domain.control_operation import ControlOperationKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    Issue,
    OrchestratorState,
    RequestedAction,
)
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
from issue_orchestrator.ports.control_operation_ownership_store import (
    ControlOperationOwnershipRead,
    ControlOperationReadStatus,
    ControlOperationRelease,
    ControlOperationReleaseStatus,
    ControlOperationReservation,
    ControlOperationReservationStatus,
)
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
class Engine:
    """One orchestrator engine's continuation stack over real durable stores."""

    root: Path
    state: OrchestratorState
    attempts: SidecarAttemptStore
    ownership: ControlOperationOwnership
    runner: RecordingRunner
    continuation: ControlContinuation
    config: Config

    def queue_cache(self) -> QueueCache:
        return QueueCache(self.config, self.state)

    def file(self, attempt: Attempt) -> Attempt:
        return self.attempts.update(attempt.key, lambda _current: attempt)


def _engine(root: Path, *, attempts: object | None = None) -> Engine:
    """A fresh engine over ``root``. A second one is a restart."""
    state = OrchestratorState()
    config = Config()
    config.repo = REPO
    store = SidecarAttemptStore(root) if attempts is None else attempts
    ownership = ControlOperationOwnership(
        state, SqliteControlOperationOwnershipStore(root / STORE_FILENAME)
    )
    runner = RecordingRunner()
    continuation = ControlContinuation(
        ownership,
        ContinuationLiveTruth(store, pr_pending_label=PR_PENDING),  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
    )
    return Engine(
        root=root,
        state=state,
        attempts=store,  # type: ignore[arg-type]
        ownership=ownership,
        runner=runner,
        continuation=continuation,
        config=config,
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


def _passed(engine: Engine, head_sha: str = SHA_A) -> Attempt:
    """The same candidate after #139 spent its allowance and the gate passed."""
    return engine.attempts.update(
        _attempt_key(head_sha),
        lambda attempt: attempt.with_revalidation_reserved().with_completed_evaluation(
            _receipt(head_sha, verdict=ValidationVerdict.PASSED)
        ),
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
                engine.attempts, pr_pending_label=PR_PENDING
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

        blind = ControlContinuation(
            readable.ownership,
            ContinuationLiveTruth(UnreadableAttempts(), pr_pending_label=PR_PENDING),  # type: ignore[arg-type]
            readable.runner,  # type: ignore[arg-type]
        )
        result = blind.reconcile(board)

        assert result.readable is False
        assert result.exclusions.owns(_operation_key())

    def test_an_unreadable_attempt_store_advances_nothing(
        self, tmp_path: Path
    ) -> None:
        readable = _engine(tmp_path)
        _failed_candidate(readable, RequestedAction.CREATE_PR)
        runner = RecordingRunner()
        blind = ControlContinuation(
            readable.ownership,
            ContinuationLiveTruth(UnreadableAttempts(), pr_pending_label=PR_PENDING),  # type: ignore[arg-type]
            runner,  # type: ignore[arg-type]
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
        continuation = ControlContinuation(
            blind_ownership,
            ContinuationLiveTruth(engine.attempts, pr_pending_label=PR_PENDING),
            engine.runner,  # type: ignore[arg-type]
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

        blind = ControlContinuation(
            readable.ownership,
            ContinuationLiveTruth(UnreadableAttempts(), pr_pending_label=PR_PENDING),  # type: ignore[arg-type]
            readable.runner,  # type: ignore[arg-type]
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
