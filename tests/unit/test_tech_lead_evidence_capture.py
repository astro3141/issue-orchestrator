"""Tech Lead session evidence capture before worktree teardown (#360).

The acceptance contract these tests hold the owner to:

* a tech_lead run produces a durable capture keyed by session/run identity;
* the manifest, candidate evidence/contracts, diff/materialization state,
  report and decision are all copied;
* two runs of the same issue/worktree never overwrite each other;
* teardown itself is untouched (the capture removes nothing and retains
  nothing);
* a capture that fails says so, rather than passing as evidence preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.control.tech_lead_evidence_capture import (
    MAX_CAPTURE_BYTES,
    capture_tech_lead_session_evidence,
)
from issue_orchestrator.domain.models import Issue, Session
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_evidence_capture import (
    CAPTURE_RECEIPT_FILENAME,
    TechLeadEvidenceCapture,
    TechLeadEvidenceCaptureError,
    tech_lead_evidence_capture_dir,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from tests.conftest import FakeIssueKey, MockEventSink
from tests.unit.session_run_helpers import make_session_run_assets

TECH_LEAD_AGENT = "agent:tech-lead"

#: The exact set #354 lost when anchor #358's worktree was reaped.
STAGED_EVIDENCE = {
    "manifest.json": '{"prs": [{"number": 358}]}',
    "candidate-evidence.json": '{"entries": []}',
    "candidate-contracts.json": '{"contracts": []}',
    "tech-lead-assignment.json": '{"flavor": "batch_review"}',
    "board-snapshot.json": '{"issues": []}',
    "pr-358-a1b2c3d4e5f6-diff.txt": "diff --git a/x b/x\n",
    "pr-358-a1b2c3d4e5f6-metadata.json": '{"head_sha": "a1b2c3d4e5f6"}',
    "tech-lead-decision.json": '{"verdict": "pass"}',
    "tech-lead-report.md": "# Report\n",
}


def _config(repo_root: Path, *, tech_lead_agent: str | None = TECH_LEAD_AGENT) -> Config:
    config = Config()
    config.repo_root = repo_root
    config.tech_lead_review_agent = tech_lead_agent
    return config


def _session(
    worktree: Path,
    *,
    issue_number: int = 358,
    agent_label: str | None = TECH_LEAD_AGENT,
    session_name: str = "issue-358",
    run_id: str = "20260830T101500000000Z",
    sample_agent_config,
) -> Session:
    worktree.mkdir(parents=True, exist_ok=True)
    run_assets = make_session_run_assets(
        worktree, session_name=session_name, run_id=run_id
    )
    issue = Issue(number=issue_number, title="Batch review", labels=[])
    return Session(
        key=SessionKey(issue=FakeIssueKey(name=str(issue_number)), task=TaskKind.CODE),
        issue=issue,
        agent_config=sample_agent_config,
        terminal_id=session_name,
        worktree_path=worktree,
        branch_name=f"issue-{issue_number}",
        run_assets=run_assets,
        agent_label=agent_label,
    )


def _stage_evidence(session: Session, files: dict[str, str] | None = None) -> Path:
    data_dir = session.run_assets.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (files if files is not None else STAGED_EVIDENCE).items():
        target = data_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return data_dir


def _capture_event(events: MockEventSink):
    published = events.get_events_by_name(EventName.TECH_LEAD_EVIDENCE_CAPTURED)
    assert len(published) == 1
    return published[0].data


class TestCaptureIdentity:
    """A capture is keyed by the run that produced it."""

    def test_capture_dir_is_keyed_by_session_and_run(self, tmp_path):
        path = tech_lead_evidence_capture_dir(
            tmp_path, session_name="issue-358", run_id="run-a"
        )
        assert path == (
            tmp_path
            / ".issue-orchestrator"
            / "tech-lead-evidence"
            / "issue-358"
            / "run-a"
        )

    @pytest.mark.parametrize("bad", ["", "   ", ".", "..", "a/b", "a\\b"])
    def test_capture_dir_refuses_a_non_segment_identity(self, tmp_path, bad):
        with pytest.raises(TechLeadEvidenceCaptureError):
            tech_lead_evidence_capture_dir(tmp_path, session_name=bad, run_id="run-a")
        with pytest.raises(TechLeadEvidenceCaptureError):
            tech_lead_evidence_capture_dir(
                tmp_path, session_name="issue-358", run_id=bad
            )

    def test_a_capture_holding_nothing_must_record_a_failure(self, tmp_path):
        with pytest.raises(ValueError):
            TechLeadEvidenceCapture(
                session_name="issue-358",
                run_id="run-a",
                issue_number=358,
                source_dir=tmp_path / "src",
                destination=tmp_path / "dst",
                captured_at="2026-08-30T00:00:00+00:00",
            )


class TestCapturesStagedEvidence:
    def test_copies_every_staged_artifact_before_teardown(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        data_dir = _stage_evidence(session)
        events = MockEventSink()

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=events
        )

        assert capture is not None
        assert capture.preserved is True
        captured = {artifact.relative_path for artifact in capture.artifacts}
        assert captured == set(STAGED_EVIDENCE)
        for name, body in STAGED_EVIDENCE.items():
            assert (capture.destination / name).read_text(encoding="utf-8") == body
        # Teardown is untouched: the capture reads the worktree, never empties it.
        assert sorted(p.name for p in data_dir.iterdir()) == sorted(STAGED_EVIDENCE)

    def test_capture_lives_outside_the_worktree_under_the_host_repo(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "wt"
        session = _session(worktree, sample_agent_config=sample_agent_config)
        _stage_evidence(session)

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        assert capture.destination == tech_lead_evidence_capture_dir(
            repo_root,
            session_name=session.run_assets.session_name,
            run_id=session.run_assets.run_id,
        )
        assert worktree not in capture.destination.parents

    def test_survives_the_worktree_being_removed(self, tmp_path, sample_agent_config):
        import shutil

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "wt"
        session = _session(worktree, sample_agent_config=sample_agent_config)
        _stage_evidence(session)

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )
        shutil.rmtree(worktree)

        assert capture is not None
        assert (capture.destination / "tech-lead-decision.json").exists()
        assert (capture.destination / "pr-358-a1b2c3d4e5f6-diff.txt").exists()

    def test_receipt_digests_every_captured_file(self, tmp_path, sample_agent_config):
        import hashlib

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        _stage_evidence(session)

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        receipt = json.loads(
            (capture.destination / CAPTURE_RECEIPT_FILENAME).read_text(encoding="utf-8")
        )
        assert receipt["preserved"] is True
        assert receipt["run_id"] == session.run_assets.run_id
        assert receipt["issue_number"] == 358
        digests = {entry["path"]: entry["sha256"] for entry in receipt["artifacts"]}
        for name, body in STAGED_EVIDENCE.items():
            assert digests[name] == hashlib.sha256(body.encode("utf-8")).hexdigest()

    def test_nested_staged_directories_are_captured(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        _stage_evidence(session, {"canonical/source-1.md": "governing text\n"})

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        assert [a.relative_path for a in capture.artifacts] == ["canonical/source-1.md"]
        assert (capture.destination / "canonical" / "source-1.md").exists()

    def test_publishes_a_preserved_event(self, tmp_path, sample_agent_config):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        _stage_evidence(session)
        events = MockEventSink()

        capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=events
        )

        payload = _capture_event(events)
        assert payload["preserved"] is True
        assert payload["issue_number"] == 358
        assert payload["artifact_count"] == len(STAGED_EVIDENCE)
        assert payload["failure"] == ""


class TestPerRunIsolation:
    def test_two_runs_of_one_worktree_do_not_overwrite_each_other(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "wt"
        first = _session(
            worktree, run_id="run-first", sample_agent_config=sample_agent_config
        )
        _stage_evidence(first, {"tech-lead-decision.json": '{"verdict": "first"}'})
        second = _session(
            worktree, run_id="run-second", sample_agent_config=sample_agent_config
        )
        _stage_evidence(second, {"tech-lead-decision.json": '{"verdict": "second"}'})
        config = _config(repo_root)

        first_capture = capture_tech_lead_session_evidence(
            config=config, session=first, events=MockEventSink()
        )
        second_capture = capture_tech_lead_session_evidence(
            config=config, session=second, events=MockEventSink()
        )

        assert first_capture is not None and second_capture is not None
        assert first_capture.destination != second_capture.destination
        assert (
            first_capture.destination / "tech-lead-decision.json"
        ).read_text(encoding="utf-8") == '{"verdict": "first"}'
        assert (
            second_capture.destination / "tech-lead-decision.json"
        ).read_text(encoding="utf-8") == '{"verdict": "second"}'

    def test_two_anchors_do_not_overwrite_each_other(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        one = _session(
            tmp_path / "wt-1",
            issue_number=358,
            session_name="issue-358",
            run_id="run-x",
            sample_agent_config=sample_agent_config,
        )
        _stage_evidence(one, {"manifest.json": '{"anchor": 358}'})
        two = _session(
            tmp_path / "wt-2",
            issue_number=359,
            session_name="issue-359",
            run_id="run-x",
            sample_agent_config=sample_agent_config,
        )
        _stage_evidence(two, {"manifest.json": '{"anchor": 359}'})
        config = _config(repo_root)

        one_capture = capture_tech_lead_session_evidence(
            config=config, session=one, events=MockEventSink()
        )
        two_capture = capture_tech_lead_session_evidence(
            config=config, session=two, events=MockEventSink()
        )

        assert one_capture is not None and two_capture is not None
        assert one_capture.destination != two_capture.destination
        assert (one_capture.destination / "manifest.json").read_text(
            encoding="utf-8"
        ) == '{"anchor": 358}'


class TestFailureIsExplicit:
    def test_missing_staged_directory_is_a_failure_not_a_success(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        events = MockEventSink()

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=events
        )

        assert capture is not None
        assert capture.preserved is False
        assert "no staged tech-lead data" in capture.failure
        assert capture.artifacts == ()
        payload = _capture_event(events)
        assert payload["preserved"] is False
        assert payload["failure"]

    def test_failed_capture_still_writes_a_receipt_saying_so(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        receipt = json.loads(
            (capture.destination / CAPTURE_RECEIPT_FILENAME).read_text(encoding="utf-8")
        )
        assert receipt["preserved"] is False
        assert receipt["artifacts"] == []
        assert receipt["failure"]

    def test_empty_staged_directory_is_a_failure(self, tmp_path, sample_agent_config):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        _stage_evidence(session, {})

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        assert capture.preserved is False
        assert "no regular files" in capture.failure

    def test_oversized_staged_tree_is_refused_explicitly(
        self, tmp_path, sample_agent_config, monkeypatch
    ):
        monkeypatch.setattr(
            "issue_orchestrator.control.tech_lead_evidence_capture.MAX_CAPTURE_BYTES",
            8,
        )
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        _stage_evidence(session, {"manifest.json": "x" * 64})

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        assert capture.preserved is False
        assert "capture budget" in capture.failure
        assert not (capture.destination / "manifest.json").exists()

    def test_unusable_run_identity_reports_without_an_unkeyed_receipt(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(
            tmp_path / "wt", run_id="bad/id", sample_agent_config=sample_agent_config
        )
        _stage_evidence(session)
        events = MockEventSink()

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=events
        )

        assert capture is not None
        assert capture.preserved is False
        assert "not a path segment" in capture.failure
        payload = _capture_event(events)
        assert payload["preserved"] is False
        # No receipt is filed anywhere unkeyed — the next unkeyed failure would
        # overwrite it, which is the opposite of durable evidence.
        assert not (
            repo_root
            / ".issue-orchestrator"
            / "tech-lead-evidence"
            / CAPTURE_RECEIPT_FILENAME
        ).exists()

    def test_default_budget_is_generous_enough_for_a_real_run(self):
        assert MAX_CAPTURE_BYTES >= 64 * 1024 * 1024

    def test_symlinked_entries_are_recorded_and_not_followed(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("host secret\n", encoding="utf-8")
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        data_dir = _stage_evidence(session, {"manifest.json": "{}"})
        (data_dir / "sneaky.txt").symlink_to(secret)

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=MockEventSink()
        )

        assert capture is not None
        assert capture.preserved is True
        assert [a.relative_path for a in capture.artifacts] == ["manifest.json"]
        assert capture.skipped == ("sneaky.txt",)
        assert not (capture.destination / "sneaky.txt").exists()


class TestOnlyTechLeadSessions:
    def test_a_coding_session_is_not_captured(self, tmp_path, sample_agent_config):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(
            tmp_path / "wt",
            agent_label="agent:backend",
            sample_agent_config=sample_agent_config,
        )
        _stage_evidence(session)
        events = MockEventSink()

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root), session=session, events=events
        )

        assert capture is None
        assert events.get_events_by_name(EventName.TECH_LEAD_EVIDENCE_CAPTURED) == []
        assert not (repo_root / ".issue-orchestrator" / "tech-lead-evidence").exists()

    def test_no_tech_lead_configured_captures_nothing(
        self, tmp_path, sample_agent_config
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        session = _session(tmp_path / "wt", sample_agent_config=sample_agent_config)
        _stage_evidence(session)

        capture = capture_tech_lead_session_evidence(
            config=_config(repo_root, tech_lead_agent=None),
            session=session,
            events=MockEventSink(),
        )

        assert capture is None
