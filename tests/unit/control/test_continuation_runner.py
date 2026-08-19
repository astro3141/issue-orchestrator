"""Executing an owned control operation (#149 §5, §6, §7).

The runner is the part that acts, so these tests are about what it composes and
what it refuses to decide. In particular: a ``RETRY_PENDING`` operation is
handed WHOLE to #139, and nothing here re-checks contract match, allowance or
reserve-before-execute; a candidate the branch has left behind has its intent
retired rather than being retried forever; and a live session is an execution
refusal that leaves ownership standing.

Every collaborator is faked at its port boundary. The runner's own composition
of the completion pipeline is what is under test, not the pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.continuation_live_truth import (
    CONTINUATION_KIND,
    ContinuationReconciliation,
    LiveContinuation,
)
from issue_orchestrator.control.continuation_runner import ControlContinuationRunner
from issue_orchestrator.control.publication_revalidation import RevalidationOutcome
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.continuation_descriptor import ContinuationDescriptor
from issue_orchestrator.domain.continuation_phase import ContinuationPhase
from issue_orchestrator.domain.control_operation import (
    ControlOperationExclusions,
    ControlOperationKey,
    ControlOperationOwnershipEntry,
    ControlOperationOwnershipStatus,
)
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    RequestedAction,
)
from issue_orchestrator.domain.review_verdict_binding import (
    BoundReviewVerdict,
    ReviewVerdictOutcome,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.ports.worktree_manager import WorktreeInfo

REPO = "owner/repo"
ISSUE_NUMBER = 149
SHA_A = "a" * 40
SHA_A_PRIME = "b" * 40
PUBLISH_COMMAND = "make validate-pr-raw"
PROFILE = "default"
AGENT = "agent:backend"


# ----------------------------------------------------------------------
# Fakes, one per port the runner composes
# ----------------------------------------------------------------------


@dataclass
class FakeRevalidation:
    """Stands in for #139. Records what it was handed; decides nothing here."""

    candidates: list[Attempt] = field(default_factory=list)
    outcome: RevalidationOutcome = RevalidationOutcome(
        started=True, reason="revalidation_completed"
    )

    def revalidate(self, candidate: Attempt) -> RevalidationOutcome:
        self.candidates.append(candidate)
        return self.outcome


@dataclass
class FakeWorktrees:
    """A worktree manager over real temporary directories."""

    root: Path
    created: list[str] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    error: Exception | None = None

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        **kwargs: object,
    ) -> WorktreeInfo:
        if self.error is not None:
            raise self.error
        name = str(kwargs.get("worktree_name") or f"repo-{issue_number}")
        self.created.append(name)
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return WorktreeInfo(path=path, branch_name=f"issue-{issue_number}")

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        self.removed.append(worktree_path)


@dataclass
class FakeWorkingCopy:
    """Reports whatever HEAD the test says the branch currently stands at."""

    head: str | None = SHA_A

    def get_head_sha(self, worktree: Path) -> str | None:
        return self.head


@dataclass
class FakeSessionOutput:
    """Allocates a real run directory under the worktree, as the real one does."""

    runs: list[SessionRunAssets] = field(default_factory=list)
    profiles: list[str | None] = field(default_factory=list)

    def start_run(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int | None = None,
        agent_label: str | None = None,
        validation_profile: str | None = None,
        **kwargs: object,
    ) -> SessionRunAssets:
        run_dir = worktree_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        assets = SessionRunAssets.from_paths(
            session_name=session_name,
            run_id="run-1",
            worktree_path=worktree_path,
            run_dir=run_dir,
            terminal_recording_path=run_dir / "terminal.cast",
            manifest_path=run_dir / "manifest.json",
            started_at="2026-08-19T00:00:00Z",
        )
        self.profiles.append(validation_profile)
        self.runs.append(assets)
        return assets


@dataclass
class ProcessingOutcome:
    success: bool = True
    message: str = "processed"


@dataclass
class FakeCompletionOwner:
    """Records the completion re-entry, and what intent it was handed."""

    calls: list[dict[str, object]] = field(default_factory=list)
    records: list[dict[str, object]] = field(default_factory=list)

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        **kwargs: object,
    ) -> ProcessingOutcome:
        completion_path = str(kwargs["completion_path"])
        self.records.append(
            json.loads((worktree / completion_path).read_text(encoding="utf-8"))
        )
        self.calls.append(
            {
                "worktree": worktree,
                "issue_number": issue_number,
                "issue_title": issue_title,
                **kwargs,
            }
        )
        return ProcessingOutcome()


@dataclass
class FakeVerdicts:
    binding: BoundReviewVerdict | None = None

    def for_run(self, run_dir: Path) -> BoundReviewVerdict | None:
        return self.binding


@dataclass
class InlineJobs:
    """A background runner that runs inline, so the tests stay deterministic."""

    submitted: list[str] = field(default_factory=list)
    running: set[str] = field(default_factory=set)

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        if job_id in self.running:
            return False
        self.submitted.append(job_id)
        self.running.add(job_id)
        try:
            fn()
        finally:
            self.running.discard(job_id)
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running

    def drain_completed(self) -> list[object]:
        return []


class RefusingJobs:
    """A runner that accepts nothing, as one with the job already in flight does."""

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        return False

    def is_running(self, job_id: str) -> bool:
        return True

    def drain_completed(self) -> list[object]:
        return []


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _issue(*labels: str) -> Issue:
    return Issue(
        number=ISSUE_NUMBER,
        title=f"Issue {ISSUE_NUMBER}",
        labels=list(labels),
        repo=REPO,
    )


def _descriptor(*actions: RequestedAction) -> ContinuationDescriptor:
    return ContinuationDescriptor(
        requested_actions=tuple(actions),
        implementation="what the agent claimed to build",
        problems="a real caveat",
        suite=ValidationGateKind.PUBLISH.suite,
        command=PUBLISH_COMMAND,
        profile=PROFILE,
    )


def _attempt(
    *actions: RequestedAction,
    head_sha: str = SHA_A,
    verdict: ValidationVerdict = ValidationVerdict.PASSED,
) -> Attempt:
    key = AttemptKey(_issue().key, head_sha)
    return Attempt(key=key).with_completed_evaluation(
        ValidationVerdictReceipt(
            suite=ValidationGateKind.PUBLISH.suite,
            head_sha=head_sha,
            verdict=verdict,
            command=PUBLISH_COMMAND,
            profile=PROFILE,
        )
    ).with_continuation_descriptor(_descriptor(*actions))


def _operation(
    attempt: Attempt,
    phase: ContinuationPhase,
    *labels: str,
) -> LiveContinuation:
    return LiveContinuation(
        key=ControlOperationKey(
            _issue().key, attempt.key.head_sha, CONTINUATION_KIND
        ),
        issue=_issue(*(labels or (AGENT,))),
        attempt=attempt,
        phase=phase,
    )


def _owned(*operations: LiveContinuation) -> ContinuationReconciliation:
    return ContinuationReconciliation(
        exclusions=ControlOperationExclusions(
            tuple(
                ControlOperationOwnershipEntry(
                    operation.key, ControlOperationOwnershipStatus.OWNED
                )
                for operation in operations
            )
        ),
        operations=tuple(operations),
    )


@dataclass
class Harness:
    runner: ControlContinuationRunner
    state: OrchestratorState
    revalidation: FakeRevalidation
    worktrees: FakeWorktrees
    working_copy: FakeWorkingCopy
    session_output: FakeSessionOutput
    completion: FakeCompletionOwner
    verdicts: FakeVerdicts
    jobs: InlineJobs
    attempts: SidecarAttemptStore


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    state = OrchestratorState()
    revalidation = FakeRevalidation()
    worktrees = FakeWorktrees(root=tmp_path / "worktrees")
    working_copy = FakeWorkingCopy()
    session_output = FakeSessionOutput()
    completion = FakeCompletionOwner()
    verdicts = FakeVerdicts()
    jobs = InlineJobs()
    attempts = SidecarAttemptStore(tmp_path / "primary")
    runner = ControlContinuationRunner(
        state=state,
        revalidation_route=revalidation,  # type: ignore[arg-type]
        attempts=attempts,
        worktrees=worktrees,  # type: ignore[arg-type]
        working_copy=working_copy,  # type: ignore[arg-type]
        session_output=session_output,  # type: ignore[arg-type]
        completion_processor=completion,  # type: ignore[arg-type]
        review_verdicts=verdicts,  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        repo_root=tmp_path / "primary",
    )
    return Harness(
        runner=runner,
        state=state,
        revalidation=revalidation,
        worktrees=worktrees,
        working_copy=working_copy,
        session_output=session_output,
        completion=completion,
        verdicts=verdicts,
        jobs=jobs,
        attempts=attempts,
    )


# ======================================================================


class TestOnlyOwnedOperationsAreAdvanced:
    def test_a_contended_operation_is_never_executed(self, harness: Harness) -> None:
        operation = _operation(
            _attempt(RequestedAction.CREATE_PR, verdict=ValidationVerdict.FAILED),
            ContinuationPhase.RETRY_PENDING,
        )
        contended = ContinuationReconciliation(
            exclusions=ControlOperationExclusions(
                (
                    ControlOperationOwnershipEntry(
                        operation.key, ControlOperationOwnershipStatus.CONTENDED
                    ),
                )
            ),
            operations=(operation,),
        )

        harness.runner.advance(contended)

        assert harness.revalidation.candidates == []

    def test_an_unavailable_operation_is_never_executed(
        self, harness: Harness
    ) -> None:
        operation = _operation(
            _attempt(RequestedAction.CREATE_PR, verdict=ValidationVerdict.FAILED),
            ContinuationPhase.RETRY_PENDING,
        )
        unavailable = ContinuationReconciliation(
            exclusions=ControlOperationExclusions(
                (
                    ControlOperationOwnershipEntry(
                        operation.key, ControlOperationOwnershipStatus.UNAVAILABLE
                    ),
                )
            ),
            operations=(operation,),
        )

        harness.runner.advance(unavailable)

        assert harness.revalidation.candidates == []


class TestSingleAdmissionOwner:
    """#139 decides admission; the runner only hands it the candidate."""

    def test_retry_pending_hands_the_candidate_whole_to_the_route(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(
            RequestedAction.CREATE_PR, verdict=ValidationVerdict.FAILED
        )

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.RETRY_PENDING))
        )

        assert harness.revalidation.candidates == [attempt]

    def test_a_refused_revalidation_is_not_second_guessed(
        self, harness: Harness
    ) -> None:
        harness.revalidation.outcome = RevalidationOutcome(
            started=False, reason="revalidation_allowance_consumed"
        )
        attempt = _attempt(
            RequestedAction.CREATE_PR, verdict=ValidationVerdict.FAILED
        )

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.RETRY_PENDING))
        )

        assert len(harness.revalidation.candidates) == 1
        assert harness.completion.calls == []
        assert harness.worktrees.created == []

    def test_the_runner_never_reserves_the_allowance_itself(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(
            RequestedAction.CREATE_PR, verdict=ValidationVerdict.FAILED
        )
        harness.attempts.update(attempt.key, lambda _current: attempt)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.RETRY_PENDING))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.revalidation_budget_used == 0


class TestActiveSessionRefusal:
    """A live session means the continuation is not this candidate's driver."""

    def test_an_issue_with_a_live_session_is_not_advanced(
        self, harness: Harness, make_session
    ) -> None:
        harness.state.active_sessions.append(
            make_session(issue_number=ISSUE_NUMBER)
        )

        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert harness.jobs.submitted == []
        assert harness.completion.calls == []

    def test_a_job_already_in_flight_is_not_resubmitted(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        runner = ControlContinuationRunner(
            state=harness.state,
            revalidation_route=harness.revalidation,  # type: ignore[arg-type]
            attempts=harness.attempts,
            worktrees=harness.worktrees,  # type: ignore[arg-type]
            working_copy=harness.working_copy,  # type: ignore[arg-type]
            session_output=harness.session_output,  # type: ignore[arg-type]
            completion_processor=harness.completion,  # type: ignore[arg-type]
            review_verdicts=harness.verdicts,  # type: ignore[arg-type]
            jobs=RefusingJobs(),  # type: ignore[arg-type]
            repo_root=tmp_path / "primary",
        )

        runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert harness.completion.calls == []


class TestExactCandidateMaterialisation:
    def test_the_continuation_worktree_is_verified_to_stand_at_the_candidate(
        self, harness: Harness
    ) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert len(harness.completion.calls) == 1
        assert harness.worktrees.created == [f"continuation-{ISSUE_NUMBER}-{SHA_A[:12]}"]

    def test_a_moved_branch_retires_the_recorded_intent(
        self, harness: Harness
    ) -> None:
        """Supersession, reached from the other side: the branch has left the
        candidate behind, so its intent stops being what the issue offers."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.working_copy.head = SHA_A_PRIME

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_descriptor is None
        assert harness.completion.calls == []

    def test_a_moved_branch_leaves_the_evaluation_history_intact(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.working_copy.head = SHA_A_PRIME

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert len(stored.publication_evaluations) == 1

    def test_the_worktree_is_always_handed_back(self, harness: Harness) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert len(harness.worktrees.removed) == 1

    def test_an_issue_with_no_agent_lane_is_refused(self, harness: Harness) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                    "needs-code-review",
                )
            )
        )

        assert harness.worktrees.created == []
        assert harness.completion.calls == []


class TestRecordedIntentIsReplayedNotInvented:
    def test_the_completion_record_carries_the_recorded_actions(
        self, harness: Harness
    ) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert harness.completion.records[0]["requested_actions"] == ["create_pr"]

    def test_an_intent_without_create_pr_replays_without_it(
        self, harness: Harness
    ) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.PUSH_BRANCH),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert harness.completion.records[0]["requested_actions"] == ["push_branch"]

    def test_the_agents_own_summary_fields_are_copied_verbatim(
        self, harness: Harness
    ) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        record = harness.completion.records[0]
        assert record["implementation"] == "what the agent claimed to build"
        assert record["problems"] == "a real caveat"

    def test_the_run_freezes_the_profile_the_descriptor_recorded(
        self, harness: Harness
    ) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert harness.session_output.profiles == [PROFILE]

    def test_the_candidates_canonical_issue_key_reaches_the_gate(
        self, harness: Harness
    ) -> None:
        harness.runner.advance(
            _owned(
                _operation(
                    _attempt(RequestedAction.CREATE_PR),
                    ContinuationPhase.PASS_PENDING_REVIEW,
                )
            )
        )

        assert harness.completion.calls[0]["issue_key"] == _issue().key


class TestDurableReviewVerdict:
    def test_an_exact_a_verdict_is_promoted_onto_the_attempt(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.verdicts.binding = BoundReviewVerdict(
            verdict=ReviewVerdictOutcome.CHANGES_REQUESTED,
            reviewed_sha=SHA_A,
            decided_at="2026-08-19T01:00:00Z",
            completed_rounds=1,
        )

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_review_verdict is not None
        assert (
            stored.continuation_review_verdict.verdict
            is ReviewVerdictOutcome.CHANGES_REQUESTED
        )

    def test_a_verdict_bound_to_another_commit_is_discarded(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.verdicts.binding = BoundReviewVerdict(
            verdict=ReviewVerdictOutcome.APPROVED,
            reviewed_sha=SHA_A_PRIME,
            decided_at="2026-08-19T01:00:00Z",
            completed_rounds=1,
        )

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_review_verdict is None

    def test_a_run_that_bound_no_verdict_records_none(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_review_verdict is None


class TestFailureLeavesTruthUnchanged:
    def test_a_worktree_that_cannot_be_created_changes_nothing(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.worktrees.error = RuntimeError("worktree add failed")

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_descriptor is not None
        assert harness.completion.calls == []
