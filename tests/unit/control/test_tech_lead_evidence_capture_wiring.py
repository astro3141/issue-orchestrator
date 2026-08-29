"""The completion handoff captures tech_lead evidence before teardown (#360).

The unit tests for the capture owner itself live in
``tests/unit/test_tech_lead_evidence_capture.py``. These prove the OTHER half:
that the owner is actually reached from the pipeline that files the cleanup
fact a worktree removal is planned from, on the outcomes that matter — a
landed run and a run that timed out and never produced a decision pair — and
that reaching it changes nothing about the teardown that follows.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.completion_handler import CleanupDecision
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
    SessionStatus,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_evidence_capture import (
    CAPTURE_RECEIPT_FILENAME,
    tech_lead_evidence_capture_dir,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.session_output import SessionOutput
from tests.unit.session_run_helpers import make_session_run_assets

TECH_LEAD_AGENT = "agent:tech-lead"
ANCHOR = 358


def _claim_store(tmp_path: Path):
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    return SqlitePendingWorkClaimStore.for_repo(tmp_path)


def _session(tmp_path: Path, *, agent_label: str = TECH_LEAD_AGENT) -> Session:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    issue = Issue(
        number=ANCHOR, title="Tech Lead Review", labels=[agent_label], repo="owner/repo"
    )
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(ANCHOR)), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(
            prompt_path=tmp_path / "prompt.md", timeout_minutes=45
        ),
        terminal_id=f"issue-{ANCHOR}",
        worktree_path=worktree,
        branch_name=f"{ANCHOR}-review",
        run_assets=make_session_run_assets(worktree, session_name=f"issue-{ANCHOR}"),
        agent_label=agent_label,
    )


def _stage(session: Session) -> Path:
    data_dir = session.run_assets.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "manifest.json").write_text('{"prs": []}', encoding="utf-8")
    (data_dir / "candidate-evidence.json").write_text('{"entries": []}', encoding="utf-8")
    (data_dir / "candidate-contracts.json").write_text('{"c": []}', encoding="utf-8")
    (data_dir / "tech-lead-decision.json").write_text('{"v": "pass"}', encoding="utf-8")
    (data_dir / "tech-lead-report.md").write_text("# Report\n", encoding="utf-8")
    return data_dir


def _handler(status_value: str) -> MagicMock:
    completion_handler = MagicMock()
    completion_handler.process_completion.return_value = MagicMock(
        actions=[],
        history_entry=SessionHistoryEntry(
            issue_number=ANCHOR,
            title="Tech Lead Review",
            agent_type=TECH_LEAD_AGENT,
            status=status_value,
            runtime_minutes=3,
            pr_url=None,
        ),
        cleanup=CleanupDecision.immediate(),
        should_queue_review=False,
        pr_url=None,
        pr_number=None,
    )
    return completion_handler


def _run(
    tmp_path: Path,
    session: Session,
    *,
    status: SessionStatus,
    events: InMemoryEventSink,
    completion_handler: MagicMock | None = None,
) -> OrchestratorState:
    config = Config()
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir(parents=True, exist_ok=True)
    config.tech_lead_review_agent = TECH_LEAD_AGENT

    state = OrchestratorState()
    state.active_sessions = [session]
    session_output = MagicMock(spec=SessionOutput)
    session_output.attach_claude_log.return_value = None

    handle_session_completion(
        session=session,
        status=status,
        state=state,
        completion_handler=completion_handler or _handler(status.value),
        action_applier=MagicMock(apply_all=MagicMock(return_value=[])),
        observer=MagicMock(),
        worktree_manager=None,
        kill_session_fn=lambda _x: None,
        config=config,
        session_output=session_output,
        pending_work_claims=_claim_store(tmp_path),
        events=events,
    )
    return state


def _destination(tmp_path: Path, session: Session) -> Path:
    return tech_lead_evidence_capture_dir(
        tmp_path / "repo",
        session_name=session.run_assets.session_name,
        run_id=session.run_assets.run_id,
    )


class TestCaptureRunsFromTheCompletionHandoff:
    def test_landed_run_is_captured_and_still_files_its_cleanup(self, tmp_path):
        session = _session(tmp_path)
        _stage(session)
        events = InMemoryEventSink()

        state = _run(
            tmp_path, session, status=SessionStatus.COMPLETED, events=events
        )

        destination = _destination(tmp_path, session)
        assert (destination / "tech-lead-decision.json").exists()
        assert (destination / "manifest.json").exists()
        assert (destination / "candidate-evidence.json").exists()
        assert (destination / "candidate-contracts.json").exists()
        assert (destination / "tech-lead-report.md").exists()
        # Teardown is unchanged: the cleanup fact the removal is planned from is
        # still filed, and the worktree is still there for it to remove.
        assert [c.issue_number for c in state.immediate_cleanups] == [ANCHOR]
        assert session.worktree_path.exists()
        [event] = events.get_events(EventName.TECH_LEAD_EVIDENCE_CAPTURED.value)
        assert event.data["preserved"] is True

    def test_timed_out_run_is_captured_too(self, tmp_path):
        session = _session(tmp_path)
        _stage(session)
        events = InMemoryEventSink()

        _run(tmp_path, session, status=SessionStatus.TIMED_OUT, events=events)

        assert (_destination(tmp_path, session) / "manifest.json").exists()

    def test_capture_precedes_completion_processing(self, tmp_path):
        """The capture must already be durable by the time processing runs, so a
        completion that raises mid-processing does not take the evidence with
        it."""
        session = _session(tmp_path)
        _stage(session)
        destination = _destination(tmp_path, session)
        seen: dict[str, bool] = {}

        completion_handler = _handler("completed")

        def _observe(*_args, **_kwargs):
            seen["captured"] = (destination / "tech-lead-decision.json").exists()
            return completion_handler.process_completion.return_value

        completion_handler.process_completion.side_effect = _observe

        _run(
            tmp_path,
            session,
            status=SessionStatus.COMPLETED,
            events=InMemoryEventSink(),
            completion_handler=completion_handler,
        )

        assert seen["captured"] is True

    def test_missing_staged_data_reports_an_explicit_capture_failure(self, tmp_path):
        session = _session(tmp_path)
        events = InMemoryEventSink()

        state = _run(
            tmp_path, session, status=SessionStatus.COMPLETED, events=events
        )

        [event] = events.get_events(EventName.TECH_LEAD_EVIDENCE_CAPTURED.value)
        assert event.data["preserved"] is False
        assert event.data["failure"]
        receipt = _destination(tmp_path, session) / CAPTURE_RECEIPT_FILENAME
        assert receipt.exists()
        # ...and the session's own lifecycle is untouched by the failure.
        assert [c.issue_number for c in state.immediate_cleanups] == [ANCHOR]

    def test_ordinary_coding_session_captures_nothing(self, tmp_path):
        session = _session(tmp_path, agent_label="agent:backend")
        _stage(session)
        events = InMemoryEventSink()

        _run(tmp_path, session, status=SessionStatus.COMPLETED, events=events)

        assert events.get_events(EventName.TECH_LEAD_EVIDENCE_CAPTURED.value) == []
        assert not (
            tmp_path / "repo" / ".issue-orchestrator" / "tech-lead-evidence"
        ).exists()
