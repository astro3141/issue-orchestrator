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

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.actions import AddLabelAction
from issue_orchestrator.control.continuation_finalize import ContinuationFinalizer
from issue_orchestrator.control.continuation_in_flight import ContinuationsInFlight
from issue_orchestrator.control.continuation_runs import ContinuationRuns
from issue_orchestrator.control.continuation_live_truth import (
    CONTINUATION_KIND,
    ContinuationReconciliation,
    LiveContinuation,
)
from issue_orchestrator.control.continuation_runner import ControlContinuationRunner
from issue_orchestrator.control.continuation_scheduling import (
    ControlContinuation,
    build_control_continuation,
)
from issue_orchestrator.control.publication_revalidation import RevalidationOutcome
from issue_orchestrator.control.worktree_runnability import WorktreeRunnability
from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
from issue_orchestrator.domain.attempt import (
    CONTINUATION_RUN_ALLOWANCE,
    Attempt,
    AttemptKey,
)
from issue_orchestrator.domain.continuation_descriptor import ContinuationDescriptor
from issue_orchestrator.domain.continuation_phase import ContinuationPhase
from issue_orchestrator.domain.continuation_settlement import (
    ContinuationSettlementKind,
)
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
from issue_orchestrator.infra.config import AgentConfig, Config
from issue_orchestrator.ports.command_runner import CommandResult
from issue_orchestrator.ports.worktree_manager import WorktreeInfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.ports.attempt_store import AttemptStore

REPO = "owner/repo"
ISSUE_NUMBER = 149
SHA_A = "a" * 40
SHA_A_PRIME = "b" * 40
PUBLISH_COMMAND = "make validate-pr-raw"
PROFILE = "default"
AGENT = "agent:backend"
PR_PENDING = "pr-pending"
PR_URL = f"https://example.test/{REPO}/pull/7"
#: The operator's provisioning recipe for these tests. A sentinel rather than
#: this repository's real ``make worktree-setup``, because what is under test is
#: that the CONFIGURED commands run: a name no production file mentions cannot
#: pass by being hard-coded somewhere.
SETUP_SENTINEL = "provision-the-continuation-worktree"


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
    journal: list[str] = field(default_factory=list)
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
        self.journal.append("materialize")
        self.created.append(name)
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return WorktreeInfo(path=path, branch_name=f"issue-{issue_number}")

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        self.removed.append(worktree_path)


@dataclass
class FakeWorkingCopy:
    """Reports whatever HEAD the test says the branch currently stands at.

    Also answers the candidate-integrity questions the runnability core asks
    around the recipe. Both are read from the same fields, so a recipe that
    moves ``HEAD`` or dirties the checkout says so here exactly as a real one
    would in a real worktree.
    """

    head: str | None = SHA_A
    dirty: bool = False

    def get_head_sha(self, worktree: Path) -> str | None:
        return self.head

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return self.dirty


@dataclass
class SetupCommands:
    """The operator's provisioning recipe, at the ``CommandRunner`` port.

    Records what ran and where, so "the configured recipe ran in the
    continuation's own checkout" is a claim the tests can make rather than
    infer. ``while_running`` is the only window a test has into the middle of a
    recipe, and two directions need one: a recipe that misbehaves in a fake
    worktree the way a real one misbehaves in a real one (changing what the
    working copy reports about the candidate), and a reconciliation landing
    while a run is open but no run assets exist yet.
    """

    journal: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    cwds: list[Path | None] = field(default_factory=list)
    failing: bool = False
    timing_out: bool = False
    while_running: Callable[[], None] | None = None

    def run(self, command: str | list[str], **kwargs: Any) -> CommandResult:
        self.journal.append("setup")
        self.commands.append(str(command))
        cwd = kwargs.get("cwd")
        self.cwds.append(Path(cwd) if cwd is not None else None)
        if self.while_running is not None:
            self.while_running()
        if self.timing_out:
            return CommandResult(
                returncode=137, stdout="", stderr="killed", timed_out=True
            )
        if self.failing:
            return CommandResult(
                returncode=1,
                stdout="",
                stderr="pyright: not found",
                timed_out=False,
            )
        return CommandResult(returncode=0, stdout="", stderr="", timed_out=False)


@dataclass
class FakeSessionOutput:
    """Allocates a real run directory under the worktree, as the real one does.

    Each call mints a DISTINCT ``run_id``, as the real one does. That is what
    makes "the same run was resumed" a testable claim rather than an accident
    of the double: the exchange's job identity is built from ``run_id``, so a
    second allocation is a second exchange.
    """

    journal: list[str] = field(default_factory=list)
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
        self.journal.append("start_run")
        run_id = f"run-{len(self.runs) + 1}"
        run_dir = worktree_path / ".issue-orchestrator" / "sessions" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        assets = SessionRunAssets.from_paths(
            session_name=session_name,
            run_id=run_id,
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
    """The pipeline's answer, in the shape ``ProcessingResult`` reports it."""

    success: bool = True
    message: str = "processed"
    pr_url: str | None = None
    review_exchange_deferred: bool = False
    validation_failed_rerouted: bool = False

    @property
    def is_non_terminal(self) -> bool:
        return self.review_exchange_deferred or self.validation_failed_rerouted


@dataclass
class FakeCompletionOwner:
    """Records the completion re-entry, and what intent it was handed."""

    journal: list[str] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    records: list[dict[str, object]] = field(default_factory=list)
    outcome: ProcessingOutcome = field(default_factory=ProcessingOutcome)
    during_process: Callable[[], None] | None = None

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        **kwargs: object,
    ) -> ProcessingOutcome:
        self.journal.append("process")
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
        if self.during_process is not None:
            self.during_process()
        return self.outcome


@dataclass
class LabelResult:
    success: bool = True
    error: str | None = None


@dataclass
class FakeActionApplier:
    """Records the board signal the finalizer emits, and can refuse it."""

    applied: list[AddLabelAction] = field(default_factory=list)
    result: LabelResult = field(default_factory=LabelResult)

    def apply(self, action: AddLabelAction) -> LabelResult:
        self.applied.append(action)
        return self.result


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


def _worktree_name_for(attempt: Attempt) -> str:
    """The deterministic per-candidate checkout name the runner asks for."""
    return f"continuation-{ISSUE_NUMBER}-{attempt.key.head_sha[:12]}"


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
    setup: SetupCommands
    session_output: FakeSessionOutput
    completion: FakeCompletionOwner
    verdicts: FakeVerdicts
    jobs: InlineJobs
    attempts: SidecarAttemptStore
    labels: FakeActionApplier
    in_flight: ContinuationsInFlight
    runs: ContinuationRuns
    #: Every step of opening a run, in the order it happened. The ordering is
    #: itself the contract (#160): a worktree that is not runnable must not
    #: reach the run assets, the completion record or the exchange.
    journal: list[str]


def _runnability(
    working_copy: FakeWorkingCopy,
    setup: SetupCommands,
    *,
    commands: list[str] | None = None,
) -> WorktreeRunnability:
    """The provisioning CORE, holding the operator's recipe and nothing else.

    The real one, not a double: what #160 requires is that the continuation
    consumes this core, so a test that stubbed it out could not tell the
    difference between consuming it and reimplementing it.
    """
    config = Config()
    config.setup_worktree = [SETUP_SENTINEL] if commands is None else commands
    return WorktreeRunnability(
        config=config,
        command_runner=setup,  # type: ignore[arg-type]
        working_copy=working_copy,  # type: ignore[arg-type]
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    state = OrchestratorState()
    revalidation = FakeRevalidation()
    journal: list[str] = []
    worktrees = FakeWorktrees(root=tmp_path / "worktrees", journal=journal)
    working_copy = FakeWorkingCopy()
    setup = SetupCommands(journal=journal)
    session_output = FakeSessionOutput(journal=journal)
    completion = FakeCompletionOwner(journal=journal)
    verdicts = FakeVerdicts()
    jobs = InlineJobs()
    attempts = SidecarAttemptStore(tmp_path / "primary")
    labels = FakeActionApplier()
    in_flight = ContinuationsInFlight()
    runs = ContinuationRuns(worktrees)  # type: ignore[arg-type]
    runner = ControlContinuationRunner(
        state=state,
        revalidation_route=revalidation,  # type: ignore[arg-type]
        attempts=attempts,
        worktrees=worktrees,  # type: ignore[arg-type]
        working_copy=working_copy,  # type: ignore[arg-type]
        runnability=_runnability(working_copy, setup),
        session_output=session_output,  # type: ignore[arg-type]
        completion_processor=completion,  # type: ignore[arg-type]
        review_verdicts=verdicts,  # type: ignore[arg-type]
        finalizer=ContinuationFinalizer(
            attempts=attempts,
            action_applier=labels,  # type: ignore[arg-type]
            pr_pending_label=PR_PENDING,
        ),
        in_flight=in_flight,
        runs=runs,
        jobs=jobs,  # type: ignore[arg-type]
        repo_root=tmp_path / "primary",
    )
    return Harness(
        runner=runner,
        state=state,
        revalidation=revalidation,
        worktrees=worktrees,
        working_copy=working_copy,
        setup=setup,
        session_output=session_output,
        completion=completion,
        verdicts=verdicts,
        jobs=jobs,
        attempts=attempts,
        labels=labels,
        in_flight=in_flight,
        runs=runs,
        journal=journal,
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
            runnability=_runnability(harness.working_copy, harness.setup),
            session_output=harness.session_output,  # type: ignore[arg-type]
            completion_processor=harness.completion,  # type: ignore[arg-type]
            review_verdicts=harness.verdicts,  # type: ignore[arg-type]
            finalizer=ContinuationFinalizer(
                attempts=harness.attempts,
                action_applier=harness.labels,  # type: ignore[arg-type]
                pr_pending_label=PR_PENDING,
            ),
            in_flight=harness.in_flight,
            runs=harness.runs,
            jobs=RefusingJobs(),  # type: ignore[arg-type]
            repo_root=tmp_path / "primary",
        )
        operation = _operation(
            _attempt(RequestedAction.CREATE_PR),
            ContinuationPhase.PASS_PENDING_REVIEW,
        )

        runner.advance(_owned(operation))

        assert harness.completion.calls == []
        # The claim this tick took is given back, or the operation would be
        # pinned live by a run that never started.
        assert harness.in_flight.is_executing(operation.key) is False


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


class TestTheRunSettlesFromWhatItProduced:
    """The board never learns about this PR, so the run must record it (F1)."""

    def test_a_created_pull_request_is_recorded_as_the_settlement(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(pr_url=PR_URL)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.APPROVED_PENDING_PR))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_settlement is not None
        assert (
            stored.continuation_settlement.kind
            is ContinuationSettlementKind.PULL_REQUEST_OPENED
        )
        assert stored.continuation_settlement.pr_url == PR_URL

    def test_the_board_is_told_a_pull_request_now_exists(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(pr_url=PR_URL)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.APPROVED_PENDING_PR))
        )

        assert [action.label for action in harness.labels.applied] == [PR_PENDING]
        assert harness.labels.applied[0].issue_number == ISSUE_NUMBER

    def test_a_board_signal_that_could_not_be_applied_settles_nothing(
        self, harness: Harness
    ) -> None:
        """Settling on an unannounced PR would hand the lane back with it open."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(pr_url=PR_URL)
        harness.labels.result = LabelResult(success=False, error="label API refused")

        with pytest.raises(RuntimeError, match="label API refused"):
            harness.runner.advance(
                _owned(_operation(attempt, ContinuationPhase.APPROVED_PENDING_PR))
            )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_settlement is None

    def test_a_run_that_produced_no_requested_pull_request_settles_nothing(
        self, harness: Harness
    ) -> None:
        """The intent is undischarged, so the next pass must try again."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(success=False, pr_url=None)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.APPROVED_PENDING_PR))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_settlement is None
        assert harness.labels.applied == []

    def test_an_intent_that_asked_for_no_pull_request_settles_on_a_clean_run(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.PUSH_BRANCH)
        harness.attempts.update(attempt.key, lambda _current: attempt)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_settlement is not None
        assert (
            stored.continuation_settlement.kind
            is ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED
        )
        assert harness.labels.applied == []

    def test_a_deferred_review_exchange_settles_nothing(
        self, harness: Harness
    ) -> None:
        """Completion has NOT finished for this record, and the pipeline says so."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(review_exchange_deferred=True)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_settlement is None


class TestTheEngineKnowsWhatItIsExecuting:
    """The one fact no durable record can state (F2)."""

    def test_the_operation_is_claimed_for_the_whole_run(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)
        seen: list[bool] = []
        harness.completion.during_process = lambda: seen.append(
            harness.in_flight.is_executing(operation.key)
        )

        harness.runner.advance(_owned(operation))

        assert seen == [True]
        assert harness.in_flight.is_executing(operation.key) is False

    def test_a_revalidation_is_claimed_while_it_spends_the_allowance(
        self, harness: Harness
    ) -> None:
        """#139 spends the allowance before the gate runs; the claim spans both."""
        attempt = _attempt(
            RequestedAction.CREATE_PR, verdict=ValidationVerdict.FAILED
        )
        operation = _operation(attempt, ContinuationPhase.RETRY_PENDING)
        seen: list[bool] = []

        def _observe(candidate: Attempt) -> RevalidationOutcome:
            seen.append(harness.in_flight.is_executing(operation.key))
            return RevalidationOutcome(started=True, reason="revalidation_completed")

        harness.revalidation.revalidate = _observe  # type: ignore[method-assign]

        harness.runner.advance(_owned(operation))

        assert seen == [True]
        assert harness.in_flight.is_executing(operation.key) is False

    def test_a_run_that_raised_still_gives_the_claim_back(
        self, harness: Harness
    ) -> None:
        """Ownership is durable and survives; the claim is about right now."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        def _explode() -> None:
            raise RuntimeError("the pipeline blew up")

        harness.completion.during_process = _explode

        with pytest.raises(RuntimeError, match="the pipeline blew up"):
            harness.runner.advance(_owned(operation))

        assert harness.in_flight.is_executing(operation.key) is False

    def test_an_operation_already_claimed_is_not_started_again(
        self, harness: Harness
    ) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)
        assert harness.in_flight.claim(operation.key) is True

        harness.runner.advance(_owned(operation))

        assert harness.jobs.submitted == []
        assert harness.completion.calls == []

    def test_an_executing_phase_starts_nothing_and_raises_nothing(
        self, harness: Harness
    ) -> None:
        """Reachable when a run ends between reconciliation and submit."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.EXECUTING))
        )

        assert harness.completion.calls == []
        assert harness.revalidation.candidates == []


class TestTheRunOutlivesThePass:
    """``process`` is not necessarily finished when it returns (F3).

    With a background supervisor wired — the only configuration in which this
    runner executes at all, since its own job goes through the same supervisor —
    the review exchange becomes its own job and the result says
    ``review_exchange_deferred``. Deleting the worktree then deletes the working
    directory of the exchange still running in it.
    """

    def _deferred(self, harness: Harness) -> Attempt:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(review_exchange_deferred=True)
        return attempt

    def test_a_deferred_exchange_keeps_its_worktree(self, harness: Harness) -> None:
        attempt = self._deferred(harness)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        assert harness.worktrees.removed == []

    def test_the_next_pass_resumes_the_same_run(self, harness: Harness) -> None:
        """A fresh ``run_id`` is a second exchange: nothing dedupes on the old one."""
        attempt = self._deferred(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        harness.runner.advance(_owned(operation))
        harness.runner.advance(_owned(operation))

        assert len(harness.completion.calls) == 2
        assert len(harness.session_output.runs) == 1
        assert harness.worktrees.created == [_worktree_name_for(attempt)]
        run_ids = [call["run_assets"].run_id for call in harness.completion.calls]
        assert run_ids == ["run-1", "run-1"]

    def test_the_recorded_intent_is_written_once_for_the_run(
        self, harness: Harness
    ) -> None:
        """A resumed pass leaves the record exactly as the pipeline left it."""
        attempt = self._deferred(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        harness.runner.advance(_owned(operation))
        first = harness.completion.records[0]
        harness.runner.advance(_owned(operation))

        assert harness.completion.records == [first, first]

    def test_a_terminal_result_closes_the_run(self, harness: Harness) -> None:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(pr_url=PR_URL)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        harness.runner.advance(_owned(operation))

        assert harness.worktrees.removed == [
            harness.session_output.runs[0].worktree_path
        ]

    def test_a_pass_after_the_run_closed_mints_a_new_one(
        self, harness: Harness
    ) -> None:
        """Closing forgets the run; nothing rediscovers a disposed checkout."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(pr_url=PR_URL)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        harness.runner.advance(_owned(operation))
        harness.runner.advance(_owned(operation))

        assert len(harness.session_output.runs) == 2

    def test_a_raised_pipeline_keeps_the_run_open(self, harness: Harness) -> None:
        """An exception is not evidence the exchange stopped using the worktree."""
        attempt = self._deferred(harness)

        def _explode() -> None:
            raise RuntimeError("the pipeline blew up")

        harness.completion.during_process = _explode

        with pytest.raises(RuntimeError, match="the pipeline blew up"):
            harness.runner.advance(
                _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
            )

        assert harness.worktrees.removed == []

    def test_a_resumed_pass_does_not_re_verify_the_candidate_commit(
        self, harness: Harness
    ) -> None:
        """The HEAD check belongs to opening a run, not to re-entering one.

        An open run's worktree is where the exchange works, and a rework round
        inside it commits — moving that HEAD past ``A`` is the exchange doing
        its job, not the supersession the check exists to catch.
        """
        attempt = self._deferred(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)
        harness.runner.advance(_owned(operation))

        harness.working_copy.head = SHA_A_PRIME
        harness.runner.advance(_owned(operation))

        assert len(harness.completion.calls) == 2
        assert harness.worktrees.removed == []
        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_descriptor is not None

    def test_an_operation_that_leaves_live_truth_has_its_run_closed(
        self, harness: Harness
    ) -> None:
        """A newer candidate supersedes the intent; nobody holds this run now."""
        attempt = self._deferred(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)
        harness.runner.advance(_owned(operation))
        assert harness.worktrees.removed == []

        harness.runner.advance(
            ContinuationReconciliation(
                exclusions=ControlOperationExclusions(()), operations=()
            )
        )

        assert harness.worktrees.removed == [
            harness.session_output.runs[0].worktree_path
        ]

    def test_a_contended_operation_keeps_its_run(self, harness: Harness) -> None:
        """Contended is live: another holder is running it, not nobody."""
        attempt = self._deferred(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)
        harness.runner.advance(_owned(operation))

        harness.runner.advance(
            ContinuationReconciliation(
                exclusions=ControlOperationExclusions(
                    (
                        ControlOperationOwnershipEntry(
                            operation.key,
                            ControlOperationOwnershipStatus.CONTENDED,
                        ),
                    )
                ),
                operations=(operation,),
            )
        )

        assert harness.worktrees.removed == []


class TestTheRunAllowanceBoundsTheRetry:
    """A terminal run that discharged nothing must not retry forever (F4)."""

    def _fruitless(self, harness: Harness) -> Attempt:
        """A terminal result carrying no pull request for a CREATE_PR intent."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(success=False, pr_url=None)
        return attempt

    def test_opening_a_run_spends_one_allowance(self, harness: Harness) -> None:
        attempt = self._fruitless(harness)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_runs_used == 1

    def test_the_allowance_is_spent_per_run_not_per_pass(
        self, harness: Harness
    ) -> None:
        """A deferred exchange re-enters every reconciliation; that is one attempt."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(review_exchange_deferred=True)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        for _ in range(4):
            harness.runner.advance(_owned(operation))

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_runs_used == 1
        assert len(harness.completion.calls) == 4

    def test_the_retry_stops_when_the_allowance_runs_out(
        self, harness: Harness
    ) -> None:
        attempt = self._fruitless(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        for _ in range(CONTINUATION_RUN_ALLOWANCE + 2):
            harness.runner.advance(_owned(operation))

        assert len(harness.session_output.runs) == CONTINUATION_RUN_ALLOWANCE
        assert len(harness.completion.calls) == CONTINUATION_RUN_ALLOWANCE

    def test_a_refused_run_leaves_the_evidence_intact(self, harness: Harness) -> None:
        """Exhaustion is a clean return to rework, not a loss of what was learned."""
        attempt = self._fruitless(harness)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        for _ in range(CONTINUATION_RUN_ALLOWANCE + 1):
            harness.runner.advance(_owned(operation))

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_descriptor is not None
        assert stored.publication_evaluations
        assert stored.continuation_settlement is None

    def test_the_allowance_is_spent_before_the_checkout_exists(
        self, harness: Harness
    ) -> None:
        """A start budget: an interrupted run must not refund itself."""
        attempt = self._fruitless(harness)
        harness.worktrees.error = RuntimeError("worktree add failed")

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_runs_used == 1
        assert harness.completion.calls == []

    def test_a_settled_run_never_reaches_the_bound(self, harness: Harness) -> None:
        """The bound is on fruitless retries, not on the work succeeding."""
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(pr_url=PR_URL)

        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_runs_used == 1
        assert stored.continuation_settlement is not None


class TestTheCoderWorktreeIsMadeRunnable:
    """#160: the continuation's checkout is an execution environment.

    It is not a read-only carrier for a cached PASS. The persistent review
    exchange opens on it as the CODER's worktree, and a ``CHANGES_REQUESTED``
    round asks that coder to edit and validate in it — so a checkout that only
    holds the source bytes fails on a toolchain that was never installed, and
    the failure is recorded against the candidate (#48).

    What runs is the operator's own ``setup_worktree`` recipe, through the
    runnability core #153 extracted. Nothing here knows what that recipe is.
    """

    def _open(self, harness: Harness) -> Attempt:
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        harness.completion.outcome = ProcessingOutcome(review_exchange_deferred=True)
        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )
        return attempt

    def test_the_recipe_runs_before_any_run_asset_exists(
        self, harness: Harness
    ) -> None:
        """The ordering IS the requirement, so it is asserted whole.

        Run assets, the completion record and the exchange's ``run_id`` are what
        the next pass resumes; a run opened around an unrunnable worktree is one
        the pipeline would keep re-entering.
        """
        self._open(harness)

        assert harness.journal == ["materialize", "setup", "start_run", "process"]

    def test_the_recipe_is_the_operators_configured_one(
        self, harness: Harness
    ) -> None:
        """No hard-coded ``make worktree-setup``, and no second recipe."""
        self._open(harness)

        assert harness.setup.commands == [SETUP_SENTINEL]

    def test_the_recipe_runs_in_the_continuations_own_checkout(
        self, harness: Harness
    ) -> None:
        """Exactly one worktree is provisioned: the coder's.

        The sibling REVIEWER worktree keeps its own policy — deliberately
        unprovisioned, guarded where the provider supports it — and this leaf
        does not reach it. One recorded working directory, and it is the
        checkout this run materialised.
        """
        self._open(harness)

        worktree = harness.session_output.runs[0].worktree_path
        assert harness.setup.cwds == [worktree]

    def test_a_provisioned_worktree_still_standing_at_the_candidate_is_used(
        self, harness: Harness
    ) -> None:
        """Direction 3: runtime state may appear; the candidate may not change.

        The core takes its checkpoint around the recipe, so "the run opened"
        already means HEAD is still ``A`` and the candidate's tracked content
        was not left modified.
        """
        self._open(harness)

        worktree = harness.session_output.runs[0].worktree_path
        assert harness.completion.calls[0]["worktree"] == worktree
        assert harness.working_copy.head == SHA_A
        assert harness.worktrees.removed == []

    def test_a_runnable_worktree_proceeds_straight_into_the_review_exchange(
        self, harness: Harness
    ) -> None:
        """Direction 12: no coder work turn stands between setup and review.

        What the pipeline is handed is the intent the agent already recorded,
        replayed for this exact commit — so the run enters the reviewer-first
        exchange rather than starting a session for the coder to work in first.
        """
        self._open(harness)

        assert harness.journal == ["materialize", "setup", "start_run", "process"]
        assert harness.completion.records[0]["outcome"] == "completed"
        assert harness.completion.records[0]["requested_actions"] == ["create_pr"]
        assert harness.state.active_sessions == []

    def test_a_repository_with_no_recipe_opens_its_run_exactly_as_before(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """An empty recipe is not a provisioning failure: nothing to install."""
        runner = ControlContinuationRunner(
            state=harness.state,
            revalidation_route=harness.revalidation,  # type: ignore[arg-type]
            attempts=harness.attempts,
            worktrees=harness.worktrees,  # type: ignore[arg-type]
            working_copy=harness.working_copy,  # type: ignore[arg-type]
            runnability=_runnability(
                harness.working_copy, harness.setup, commands=[]
            ),
            session_output=harness.session_output,  # type: ignore[arg-type]
            completion_processor=harness.completion,  # type: ignore[arg-type]
            review_verdicts=harness.verdicts,  # type: ignore[arg-type]
            finalizer=ContinuationFinalizer(
                attempts=harness.attempts,
                action_applier=harness.labels,  # type: ignore[arg-type]
                pr_pending_label=PR_PENDING,
            ),
            in_flight=harness.in_flight,
            runs=harness.runs,
            jobs=harness.jobs,  # type: ignore[arg-type]
            repo_root=tmp_path / "primary",
        )
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)

        runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        assert harness.setup.commands == []
        assert len(harness.completion.calls) == 1

    def test_the_runner_composes_the_core_and_no_retry_policy(self) -> None:
        """The collaborator, named: a recipe, and no launch-shaped bound.

        Asserted on the constructor rather than on behaviour because what must
        not exist cannot be observed by running it. ``WorktreeProvisioner``
        carries a consecutive-failure ledger and a ``needs-human`` escalation,
        and admitting it here would be a second bound over a run whose
        allowance #149 has already spent.

        The annotation is compared as text because the runner declares it under
        ``TYPE_CHECKING``; that it is the CORE and behaves like one is what the
        rest of this suite exercises, with the real
        :class:`WorktreeRunnability` wired in.
        """
        parameters = inspect.signature(ControlContinuationRunner.__init__).parameters

        annotation = parameters["runnability"].annotation.strip("\"'")
        assert annotation == WorktreeRunnability.__name__
        assert "provisioner" not in parameters
        assert "issue_number" not in parameters


class TestAnUnrunnableWorktreeOpensNoRun:
    """#160's failure direction: nothing downstream of the recipe happens.

    Every case below asserts the same things — no run assets, no completion
    processing, no verdict, no settlement, and a durable PASS(A) still latest —
    because a continuation that provisioned badly and continued anyway would
    file evidence about an environment under the candidate's name.
    """

    def _unrunnable(self, harness: Harness, attempt: Attempt | None = None) -> Attempt:
        candidate = attempt if attempt is not None else _attempt(
            RequestedAction.CREATE_PR
        )
        harness.attempts.update(candidate.key, lambda _current: candidate)
        harness.runner.advance(
            _owned(_operation(candidate, ContinuationPhase.PASS_PENDING_REVIEW))
        )
        stored = harness.attempts.for_key(candidate.key)
        assert stored is not None
        assert harness.session_output.runs == []
        assert harness.completion.calls == []
        return stored

    def test_a_failing_setup_command_starts_no_run(self, harness: Harness) -> None:
        harness.setup.failing = True

        self._unrunnable(harness)

        assert harness.journal == ["materialize", "setup"]

    def test_a_setup_command_that_times_out_starts_no_run(
        self, harness: Harness
    ) -> None:
        harness.setup.timing_out = True

        self._unrunnable(harness)

        assert harness.setup.commands == [SETUP_SENTINEL]

    def test_setup_that_moves_head_off_the_candidate_starts_no_run(
        self, harness: Harness
    ) -> None:
        """Provisioning installs tooling; it does not get to move the candidate."""
        harness.setup.while_running = lambda: setattr(
            harness.working_copy, "head", SHA_A_PRIME
        )

        self._unrunnable(harness)

    def test_setup_that_dirties_the_candidates_tracked_content_starts_no_run(
        self, harness: Harness
    ) -> None:
        harness.setup.while_running = lambda: setattr(
            harness.working_copy, "dirty", True
        )

        self._unrunnable(harness)

    def test_the_failed_checkout_is_removed(self, harness: Harness) -> None:
        """Nothing else will: the run was never opened, so nobody holds it."""
        harness.setup.failing = True

        self._unrunnable(harness)

        assert harness.worktrees.removed == [
            harness.worktrees.root / f"continuation-{ISSUE_NUMBER}-{SHA_A[:12]}"
        ]

    def test_the_run_allowance_stays_spent(self, harness: Harness) -> None:
        """A start budget: a broken environment must not refund its own run."""
        harness.setup.failing = True

        stored = self._unrunnable(harness)

        assert stored.continuation_runs_used == 1

    def test_the_durable_pass_is_left_untouched(self, harness: Harness) -> None:
        """No publication evaluation is appended, and PASS(A) is still latest."""
        harness.setup.failing = True

        stored = self._unrunnable(harness)

        assert len(stored.publication_evaluations) == 1
        latest = stored.latest_publication_evaluation
        assert latest is not None
        assert latest.verdict is ValidationVerdict.PASSED
        assert latest.head_sha == SHA_A

    def test_no_verdict_or_settlement_is_fabricated(self, harness: Harness) -> None:
        harness.setup.failing = True
        harness.verdicts.binding = BoundReviewVerdict(
            verdict=ReviewVerdictOutcome.APPROVED,
            reviewed_sha=SHA_A,
            decided_at="2026-08-19T01:00:00Z",
            completed_rounds=1,
        )

        stored = self._unrunnable(harness)

        assert stored.continuation_review_verdict is None
        assert stored.continuation_settlement is None
        assert harness.labels.applied == []

    def test_the_recorded_intent_survives_for_a_later_pass(
        self, harness: Harness
    ) -> None:
        """The candidate is not retired: it is the branch tip, and still live."""
        harness.setup.failing = True

        stored = self._unrunnable(harness)

        assert stored.continuation_descriptor is not None

    def test_a_repaired_environment_opens_a_run_on_a_later_pass(
        self, harness: Harness
    ) -> None:
        """No new counter refuses the retry: the allowance alone bounds it."""
        harness.setup.failing = True
        attempt = self._unrunnable(harness)

        harness.setup.failing = False
        harness.runner.advance(
            _owned(_operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW))
        )

        assert len(harness.completion.calls) == 1
        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_runs_used == 2

    def test_the_retry_stops_when_the_existing_allowance_runs_out(
        self, harness: Harness
    ) -> None:
        """Exhaustion is #149's own ``RUNS_EXHAUSTED``, not a second bound."""
        harness.setup.failing = True
        attempt = _attempt(RequestedAction.CREATE_PR)
        harness.attempts.update(attempt.key, lambda _current: attempt)
        operation = _operation(attempt, ContinuationPhase.PASS_PENDING_REVIEW)

        for _ in range(CONTINUATION_RUN_ALLOWANCE + 2):
            harness.runner.advance(_owned(operation))

        assert harness.setup.commands == [SETUP_SENTINEL] * CONTINUATION_RUN_ALLOWANCE
        assert len(harness.worktrees.created) == CONTINUATION_RUN_ALLOWANCE
        stored = harness.attempts.for_key(attempt.key)
        assert stored is not None
        assert stored.continuation_runs_used == CONTINUATION_RUN_ALLOWANCE
        assert stored.continuation_run_allowance_available is False


# ======================================================================
# The production assembly
# ======================================================================


@dataclass
class BuiltEngine:
    """One engine's continuation stack, as the PRODUCTION builder makes it.

    Everything above hand-wires the runner, which is the right shape for asking
    what the runner does and the wrong shape for asking what it is made of: a
    suite that supplies its own ``WorktreeRunnability`` cannot tell a builder
    that wires one correctly from a builder that wires one from the wrong
    ``Config``, and it cannot tell one registry from two.

    So nothing here is assembled locally. The container comes from the real
    composition root and the owner from :func:`build_control_continuation`; the
    only substitutions are the ports at the edge of the process — the checkout,
    the shell, the run directory, the completion pipeline and the job runner —
    because a unit test may not create git worktrees or run installers.
    """

    continuation: ControlContinuation
    state: OrchestratorState
    attempts: "AttemptStore"
    worktrees: FakeWorktrees
    setup: SetupCommands
    session_output: FakeSessionOutput
    completion: FakeCompletionOwner
    journal: list[str]
    issue: Issue

    def reconcile(self) -> ContinuationReconciliation:
        """One tick's hydration over a board holding just this issue."""
        return self.continuation.reconcile([self.issue])

    def file(self, attempt: Attempt) -> Attempt:
        return self.attempts.update(attempt.key, lambda _current: attempt)

    def spend_all_but_one_run(self) -> None:
        """Burn every continuation run but the last on a broken environment.

        Both non-durable facts — a job in flight, a run held open — only decide
        anything once the durable allowance is spent: while one remains, the
        candidate derives ``PASS_PENDING_REVIEW`` from the record alone and a
        duplicated registry is invisible. A worktree that could not be made
        runnable spends an allowance and opens no run, which is exactly the way
        to arrive at the last one with nothing else changed.
        """
        self.setup.failing = True
        for _ in range(CONTINUATION_RUN_ALLOWANCE - 1):
            self.reconcile()
        self.setup.failing = False


def _config_for(root: Path, *, agents: bool = False) -> Config:
    """A repository configured the way an operator configures one."""
    config = Config()
    config.repo = REPO
    config.repo_root = root
    root.mkdir(parents=True, exist_ok=True)
    config.worktree_base = root / "worktrees"
    config.setup_worktree = [SETUP_SENTINEL]
    if agents:
        config.agents = {
            AGENT: AgentConfig(
                prompt_path=root / "prompt.md", model="sonnet", timeout_minutes=45
            )
        }
    return config


def _orchestrator_for(config: Config, board: list[Issue]) -> "Orchestrator":
    """The real composition root, over a repository host that returns ``board``."""
    github = MagicMock()
    github.list_issues.return_value = board
    github.get_issue_labels.return_value = list(board[0].labels) if board else []
    github.get_issue_labels_fresh.return_value = (
        list(board[0].labels) if board else []
    )

    with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
        return build_orchestrator_for_testing(config=config, github=github)


def _built(tmp_path: Path) -> BuiltEngine:
    """The continuation this repository's own configuration assembles."""
    config = _config_for(tmp_path / "primary")
    issue = _issue(AGENT)
    orchestrator = _orchestrator_for(config, [issue])

    journal: list[str] = []
    worktrees = FakeWorktrees(root=tmp_path / "checkouts", journal=journal)
    setup = SetupCommands(journal=journal)
    session_output = FakeSessionOutput(journal=journal)
    # Deferred, because that is what a wired background supervisor produces:
    # the exchange becomes its own job, the run stays open across passes, and
    # "the engine still knows it holds that run" becomes observable.
    completion = FakeCompletionOwner(
        journal=journal, outcome=ProcessingOutcome(review_exchange_deferred=True)
    )
    deps = replace(
        orchestrator.deps,
        worktree_manager=worktrees,  # type: ignore[arg-type]
        working_copy=FakeWorkingCopy(),  # type: ignore[arg-type]
        command_runner=setup,  # type: ignore[arg-type]
        session_output=session_output,  # type: ignore[arg-type]
        completion_processor=completion,  # type: ignore[arg-type]
        action_applier=FakeActionApplier(),  # type: ignore[arg-type]
        services=replace(
            orchestrator.deps.services,
            background_job_supervisor=InlineJobs(),  # type: ignore[arg-type]
        ),
    )
    return BuiltEngine(
        continuation=build_control_continuation(
            state=orchestrator.state, config=config, deps=deps
        ),
        state=orchestrator.state,
        attempts=orchestrator.deps.attempt_store,
        worktrees=worktrees,
        setup=setup,
        session_output=session_output,
        completion=completion,
        journal=journal,
        issue=issue,
    )


class TestTheBuilderAssemblesWhatProductionRuns:
    """#160: the wiring is where this change's contract actually lives.

    The runner consumes a ``WorktreeRunnability`` it is handed, so every claim
    the suites above make is a claim about a core THIS module wired. Two ways
    the builder can diverge from them are exactly the failures #160 exists to
    close, and neither is visible from a hand-wired runner: a runnability built
    from the wrong ``Config`` (an empty recipe provisions nothing and returns
    success, so the exchange dies on a missing toolchain and the candidate is
    blamed — #48), and registries duplicated by the very refactor that moved
    this assembly out of the facade.
    """

    def test_the_recipe_is_the_one_this_repository_configured(
        self, tmp_path: Path
    ) -> None:
        """Not a recipe the test supplied: the operator's ``setup_worktree``.

        The sentinel appears in no production file, so a builder reading the
        commands from anywhere but the ``Config`` it was handed cannot pass.
        """
        engine = _built(tmp_path)
        engine.file(_attempt(RequestedAction.CREATE_PR))

        engine.reconcile()

        assert engine.setup.commands == [SETUP_SENTINEL]

    def test_the_configured_recipe_runs_before_any_run_asset_exists(
        self, tmp_path: Path
    ) -> None:
        """The ordering the runner is tested on, proved through the real wiring."""
        engine = _built(tmp_path)
        engine.file(_attempt(RequestedAction.CREATE_PR))

        engine.reconcile()

        assert engine.journal == ["materialize", "setup", "start_run", "process"]
        assert engine.setup.cwds == [engine.session_output.runs[0].worktree_path]

    def test_live_truth_reads_the_open_runs_the_runner_holds(
        self, tmp_path: Path
    ) -> None:
        """One ``ContinuationRuns``, or a mid-exchange pass forgets the run.

        Read on the LAST allowance, because that is the only reading in which
        the registry decides anything: the durable record then says the
        allowance is spent with nothing to show for it — ``RUNS_EXHAUSTED``,
        which is not live — and the open run the RUNNER holds is the single fact
        that keeps the operation live, and resumable. A second registry would
        drop it from live truth, release the lease, and close the run out from
        under the exchange still working in that checkout.
        """
        engine = _built(tmp_path)
        engine.file(_attempt(RequestedAction.CREATE_PR))
        engine.spend_all_but_one_run()
        engine.reconcile()

        second = engine.reconcile()

        assert [operation.phase for operation in second.owned] == [
            ContinuationPhase.PASS_PENDING_REVIEW
        ]
        # Resumed, not reopened: the same run the runner is already carrying,
        # so the exchange keeps its identity across the pass.
        assert len(engine.session_output.runs) == 1
        assert len(engine.completion.calls) == 2
        # The one disposal is of the checkout whose provisioning failed above.
        # A second would be this run's, closed by a pass that had forgotten it.
        assert len(engine.worktrees.removed) == 1

    def test_live_truth_reads_the_in_flight_registry_the_runner_claims_into(
        self, tmp_path: Path
    ) -> None:
        """One ``ContinuationsInFlight``, or a tick mid-run frees the issue.

        The probe runs while the recipe does, on the last allowance: it is spent
        and no run is open yet, so the claim the runner took is the ONLY thing
        keeping the operation live. A second registry reads "nothing is
        executing", derives ``RUNS_EXHAUSTED``, and drops the exclusion that is
        the whole reason ordinary rework cannot race a control operation (#148).
        """
        engine = _built(tmp_path)
        engine.file(_attempt(RequestedAction.CREATE_PR))
        engine.spend_all_but_one_run()
        mid_run: list[ContinuationReconciliation] = []
        engine.setup.while_running = lambda: mid_run.append(engine.reconcile())

        engine.reconcile()

        assert [operation.phase for operation in mid_run[0].owned] == [
            ContinuationPhase.EXECUTING
        ]
        assert mid_run[0].exclusions.excludes_issue(engine.issue.key)
        # The probe is a tick, not a second run: one run opened, one checkout.
        assert len(engine.session_output.runs) == 1


class TestTheEngineHydratesThroughTheOwnerItBuilt:
    """A builder nothing at the composition root calls is unreachable.

    So this drives the REAL orchestrator through a PUBLIC hydration point,
    rather than asserting that a factory exists: the facade's ``cached_property``
    is on the tested path only if a queue refresh reconciles through it.
    """

    def test_a_queue_refresh_excludes_the_issue_a_continuation_owns(
        self, tmp_path: Path
    ) -> None:
        """Reconcile-then-hydrate, through the owner the facade assembled.

        The issue is in scope and fetched — it reaches the scope snapshot — and
        it is out of the queue for exactly one reason: the exclusion this
        engine's own continuation owner published for a live control operation.

        No run is started: the test composition wires no background runner, and
        the runner refuses to execute without one.
        """
        config = _config_for(tmp_path / "primary", agents=True)
        issue = _issue(AGENT)
        orchestrator = _orchestrator_for(config, [issue])
        attempt = _attempt(RequestedAction.CREATE_PR)
        orchestrator.deps.attempt_store.update(attempt.key, lambda _current: attempt)

        orchestrator.update_queue_cache()

        assert [i.number for i in orchestrator.state.cached_scope_issues] == [
            ISSUE_NUMBER
        ]
        assert orchestrator.state.cached_queue_issues == []
        assert orchestrator.state.control_operation_exclusions.excludes_issue(
            issue.key
        )
