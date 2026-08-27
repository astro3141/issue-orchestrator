"""A settled zero-code completion must not pay for a code gate (#328).

``settle_tech_lead_completion`` already proves that a ``planning_investigation``
run left its checkout at the commit it was launched on and therefore offers no
code candidate (#202). Before #328 the processor logged that fact and dropped
it, so ``SessionController`` mapped the completion to ``COMPLETED``, ran the
ordinary quick gate over the unchanged base commit, and recorded a
candidate-shaped PASS for a run with nothing to validate — measured live, ~270s
of it, on a real planning run.

These tests run the REAL owner and the REAL controller against each other,
because the defect was never in either one: it was in the hop between them.
A double standing in for the completion processor would let the propagation be
deleted without a single failure here, which is exactly the mutation the suite
has to catch.

The boundary matters as much as the fix, so both negative controls are pinned:
an ordinary code-bearing completion still runs the configured gate, and a
tech_lead completion whose settlement is anything less than a proven zero-code
run keeps today's behaviour. A blanket ``TaskKind.TECH_LEAD`` skip fails here.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_processor import (
    CompletionProcessor,
    GitAdapter,
    LabelAdapter,
    PRAdapter,
)
from issue_orchestrator.control.session_controller import SessionController
from issue_orchestrator.domain.events import EventBus
from issue_orchestrator.domain.models import (
    AgentConfig,
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
    SessionStatus,
    sanitize_agent_label,
)
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.observation.observation import SessionObservationResult
from issue_orchestrator.ports import EventSink
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import (
    BranchPathsResult,
    BranchTextFilesResult,
    DiffResult,
    PushResult,
)
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.unit.session_run_helpers import make_session_run_assets

TECH_LEAD_AGENT = "agent:tech-lead"
LAUNCH_SHA = "c" * 40
MOVED_SHA = "d" * 40
QUICK_CMD = "./scripts/validate-quick.sh"


class RecordingCommandRunner:
    """Every local command the quick gate would run, and nothing else.

    The primary falsification instrument: "zero quick command executions" is
    only meaningful if a real runner would have recorded one.
    """

    def __init__(self) -> None:
        self.run_calls: list[str | list[str]] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.run_calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="", timed_out=False)


class RecordingEventSink:
    """Captures the trace so evidence-direction can be asserted on."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [event.event_type for event in self.events]


class StubWorkingCopy:
    """The controller's git reads: a checkout standing where it was launched."""

    def get_head_sha(self, worktree: Path) -> str | None:
        return LAUNCH_SHA

    def get_current_branch(self, worktree: Path) -> str | None:
        return "issue-123"


@pytest.fixture
def git_adapter() -> Mock:
    """The processor's worktree reads, armed as an untouched checkout."""
    adapter = Mock(spec=GitAdapter)
    adapter.default_branch = Mock(return_value="main")
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.get_head_sha = Mock(return_value=LAUNCH_SHA)
    adapter.list_dirty_files = Mock(return_value=[])
    adapter.has_uncommitted_changes = Mock(return_value=False)
    adapter.has_tracked_changes = Mock(return_value=False)
    adapter.push = Mock(
        return_value=PushResult(
            success=True, branch="issue-123", remote="origin", message="Pushed"
        )
    )
    adapter.rebase_on_branch = Mock(return_value=SimpleNamespace(success=True, message="Rebased"))
    adapter.create_branch_from_current = Mock()
    adapter.list_branch_names = Mock(return_value=["issue-123"])
    adapter.diff_against_base = Mock(return_value=DiffResult(success=True, diff_text=""))
    adapter.read_branch_text_files = Mock(
        return_value=BranchTextFilesResult(success=True)
    )
    adapter.branch_post_image_paths_against_base = Mock(
        return_value=BranchPathsResult(success=True, paths=())
    )
    return adapter


@pytest.fixture
def authority_store(tmp_path: Path) -> SqliteTechLeadAuthorityStore:
    return SqliteTechLeadAuthorityStore.for_repo(tmp_path)


def _config(tmp_path: Path) -> Config:
    prompt = tmp_path / "tech-lead.md"
    prompt.write_text("Tech Lead prompt")
    config = Config()
    config.repo_root = tmp_path
    config.tech_lead_review_agent = TECH_LEAD_AGENT
    config.agents = {TECH_LEAD_AGENT: AgentConfig(prompt_path=prompt)}
    return config


def _completion_record(session_id: str = "planning-run") -> CompletionRecord:
    """What ``coding-done completed`` writes — planning runs included."""
    return CompletionRecord(
        session_id=session_id,
        timestamp=datetime.now().isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary="Prepared #123",
        requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        implementation="Read the issue and proposed follow-up work",
    )


def _write_completion(worktree: Path, record: CompletionRecord, filename: str) -> str:
    record_dir = worktree / ".issue-orchestrator"
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / filename).write_text(json.dumps(record.to_dict()))
    return f".issue-orchestrator/{filename}"


def _plant_decision_pair(run_dir: Path) -> None:
    """A valid pair, so the completion is ADMITTED and reaches the zero-code lane."""
    data_dir = run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Preparation complete.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Missing groundwork",
                        "classification": "task",
                        "evidence": ["issue #123 body"],
                    }
                ],
                "proposed_actions": [
                    {
                        "id": "A1",
                        "action_type": "create_issue",
                        "title": "Land the groundwork",
                        "body": "Do the thing first.",
                        "finding_ids": ["T1"],
                    }
                ],
            }
        )
    )
    (data_dir / "tech-lead-report.md").write_text(
        "# Report\n\nFinding T1 prepared.\n\nProposals: A1.\n"
    )


def _arm_planning_run(
    authority_store: SqliteTechLeadAuthorityStore,
    run_assets,
    *,
    launch_base_sha: str | None,
    record_authority: bool = True,
) -> None:
    """Arm the run the way the orchestrator arms a real planning session."""
    run_dir = run_assets.run_dir
    assignment_path = run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    TechLeadAssignment(
        flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
        focus_issue_number=123,
        focus_reason="Prepare: open and unblocked",
    ).write(assignment_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tech_lead_assignment"] = str(assignment_path)
    manifest_path.write_text(json.dumps(manifest))
    _plant_decision_pair(run_dir)
    if not record_authority:
        return
    authority_store.record(
        run_id=run_assets.run_id,
        session_name=run_assets.session_name,
        authority=TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            anchor_issue_number=123,
            focus_issue_number=123,
            launch_base_sha=launch_base_sha,
        ),
    )


def _processor(
    config: Config | None,
    git_adapter: Mock,
    authority_store: SqliteTechLeadAuthorityStore,
) -> CompletionProcessor:
    label_adapter = Mock(spec=LabelAdapter)
    pr_adapter = Mock(spec=PRAdapter)
    pr_adapter.get_prs_for_issue = Mock(return_value=[])
    pr_adapter.get_prs_for_branch = Mock(return_value=[])
    pr_adapter.create_pr = Mock(
        return_value=PRInfo(
            number=42,
            title="Do the work",
            url="https://github.com/owner/repo/pull/42",
            branch="issue-123",
            body="",
            state="open",
            labels=[],
        )
    )
    pr_adapter.add_comment = Mock(return_value="comment-id")
    return CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=label_adapter,
        pr_adapter=pr_adapter,
        git_adapter=git_adapter,
        session_output=FileSystemSessionOutput(),
        event_bus=EventBus(),
        label_config={},
        config=config,
        tech_lead_authority=authority_store,
    )


def _controller(
    processor: CompletionProcessor,
    events: EventSink,
    command_runner: RecordingCommandRunner,
) -> SessionController:
    return SessionController(
        completion_processor=processor,
        events=events,
        session_output=FileSystemSessionOutput(),
        working_copy=StubWorkingCopy(),
        command_runner=command_runner,
        validation_cmd=QUICK_CMD,
        validation_timeout_seconds=60,
        max_validation_retries=3,
    )


def _decide(controller: SessionController, worktree: Path, run_assets, completion_path):
    return controller.decide_outcome(
        SessionObservationResult.terminated(runtime_minutes=10.0),
        worktree,
        123,
        "Prepare #123",
        run_assets.session_name,
        completion_path,
        session_run_assets=run_assets,
        task_kind=TaskKind.TECH_LEAD,
    )


def _validation_records(run_dir: Path) -> list[Path]:
    return list(run_dir.rglob("validation-record.json"))


class TestSettledZeroCodePlanningRun:
    """The measured defect: a proven zero-code run paid for a quick gate."""

    def _run(self, tmp_path, git_adapter, authority_store):
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        run_assets = make_session_run_assets(worktree, session_name="tech-lead-123")
        _arm_planning_run(authority_store, run_assets, launch_base_sha=LAUNCH_SHA)
        completion_path = _write_completion(
            worktree,
            _completion_record(),
            f"completion-{sanitize_agent_label(TECH_LEAD_AGENT)}.json",
        )
        events = RecordingEventSink()
        command_runner = RecordingCommandRunner()
        controller = _controller(
            _processor(_config(tmp_path), git_adapter, authority_store),
            events,
            command_runner,
        )
        decision = _decide(controller, worktree, run_assets, completion_path)
        return decision, events, command_runner, run_assets

    def test_the_quick_command_is_never_executed(
        self, tmp_path: Path, git_adapter: Mock, authority_store
    ) -> None:
        """Proof 1: zero quick command executions on the settled planning path."""
        decision, _events, command_runner, _run_assets = self._run(
            tmp_path, git_adapter, authority_store
        )

        assert decision.status == SessionStatus.COMPLETED
        assert command_runner.run_calls == []

    def test_no_candidate_validation_evidence_is_written_for_the_base_commit(
        self, tmp_path: Path, git_adapter: Mock, authority_store
    ) -> None:
        """Proof 2: nothing claims this unchanged base commit was validated."""
        decision, events, _runner, run_assets = self._run(
            tmp_path, git_adapter, authority_store
        )

        assert decision.validation_passed is None
        assert decision.validation_error is None
        assert _validation_records(run_assets.run_dir) == []
        assert EventName.SESSION_VALIDATION_PASSED not in events.names()
        assert EventName.SESSION_VALIDATION_FAILED not in events.names()
        assert EventName.SESSION_VALIDATION_RETRY_NEEDED not in events.names()

    def test_the_completion_keeps_the_settled_zero_code_result(
        self, tmp_path: Path, git_adapter: Mock, authority_store
    ) -> None:
        """The skip changes nothing else: the completion stands as settled."""
        decision, _events, _runner, _run_assets = self._run(
            tmp_path, git_adapter, authority_store
        )

        assert decision.completion_processed is True
        assert decision.processing_result is not None
        assert decision.processing_result.success is True
        assert decision.processing_result.code_candidate.offers_code_candidate is False
        assert decision.processing_result.code_candidate.detail


class TestTheGateSurvivesEverywhereElse:
    """The boundary: only a PROVEN zero-code settlement buys the skip."""

    def test_an_ordinary_code_bearing_completion_still_runs_the_quick_gate(
        self, tmp_path: Path, git_adapter: Mock, authority_store
    ) -> None:
        """Proof 3: the Actor path is untouched — configured quick still runs."""
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        run_assets = make_session_run_assets(worktree, session_name="issue-123")
        completion_path = _write_completion(
            worktree, _completion_record("coder-run"), "completion.json"
        )
        events = RecordingEventSink()
        command_runner = RecordingCommandRunner()
        controller = _controller(
            _processor(_config(tmp_path), git_adapter, authority_store),
            events,
            command_runner,
        )

        decision = controller.decide_outcome(
            SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree,
            123,
            "Do the work",
            run_assets.session_name,
            completion_path,
            session_run_assets=run_assets,
            task_kind=TaskKind.CODE,
        )

        assert decision.status == SessionStatus.COMPLETED
        assert command_runner.run_calls == [QUICK_CMD]
        assert decision.validation_passed is True

    @pytest.mark.parametrize(
        ("launch_base_sha", "head_sha", "dirty_files", "why"),
        [
            (LAUNCH_SHA, MOVED_SHA, [], "the checkout moved after launch"),
            (LAUNCH_SHA, LAUNCH_SHA, ["src/thing.py"], "tracked content is modified"),
            ("", LAUNCH_SHA, [], "the authority records no launch base commit"),
        ],
    )
    def test_a_tech_lead_run_that_is_not_settled_zero_code_keeps_the_gate(
        self,
        tmp_path: Path,
        git_adapter: Mock,
        authority_store,
        launch_base_sha: str | None,
        head_sha: str,
        dirty_files: list[str],
        why: str,
    ) -> None:
        """Proof 4: Tech Lead is not itself a reason to skip.

        Each case reaches the owner and comes back ``zero_code=False``. A
        blanket ``TaskKind.TECH_LEAD`` skip, or any inference from the role
        name or the session prefix, fails every row here.
        """
        git_adapter.get_head_sha.return_value = head_sha
        git_adapter.list_dirty_files.return_value = dirty_files
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        run_assets = make_session_run_assets(worktree, session_name="tech-lead-123")
        _arm_planning_run(authority_store, run_assets, launch_base_sha=launch_base_sha)
        completion_path = _write_completion(
            worktree,
            _completion_record(),
            f"completion-{sanitize_agent_label(TECH_LEAD_AGENT)}.json",
        )
        command_runner = RecordingCommandRunner()
        controller = _controller(
            _processor(_config(tmp_path), git_adapter, authority_store),
            RecordingEventSink(),
            command_runner,
        )

        decision = _decide(controller, worktree, run_assets, completion_path)

        assert command_runner.run_calls == [QUICK_CMD], why
        assert decision.processing_result is not None
        assert decision.processing_result.code_candidate.offers_code_candidate is True

    def test_an_unresolvable_launch_authority_cannot_manufacture_the_skip(
        self, tmp_path: Path, git_adapter: Mock, authority_store
    ) -> None:
        """Proof 5: a refused completion proves nothing about a checkout.

        The run's checkout is armed exactly as the settled case — untouched, at
        the launch commit — and the ONLY difference is that no orchestrator-owned
        authority record exists. The completion is rejected, and the fail-safe
        direction holds: the gate is not skipped on a refusal's behalf.
        """
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        run_assets = make_session_run_assets(worktree, session_name="tech-lead-123")
        _arm_planning_run(
            authority_store,
            run_assets,
            launch_base_sha=LAUNCH_SHA,
            record_authority=False,
        )
        completion_path = _write_completion(
            worktree,
            _completion_record(),
            f"completion-{sanitize_agent_label(TECH_LEAD_AGENT)}.json",
        )
        command_runner = RecordingCommandRunner()
        controller = _controller(
            _processor(_config(tmp_path), git_adapter, authority_store),
            RecordingEventSink(),
            command_runner,
        )

        decision = _decide(controller, worktree, run_assets, completion_path)

        assert decision.processing_result is not None
        assert decision.processing_result.success is False
        assert decision.processing_result.code_candidate.offers_code_candidate is True
        assert command_runner.run_calls == [QUICK_CMD]
