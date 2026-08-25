"""Which completion record file speaks for a run (#264).

The producer writes an error placeholder at the canonical path when
``coding-done`` crashes while building the record, then writes the
successful retry to a numeric-suffixed sibling. These tests pin the one
owner that decides which of those files the orchestrator acts on, and
prove both readers ask it rather than resolving the path themselves.
"""

import json
from pathlib import Path

import pytest

from issue_orchestrator.control.completion_record_validation import (
    _MAX_COMPLETION_FILE_BYTES,
    CompletionPathChoice,
    CompletionRecordLoadFailure,
    CompletionRecordValidator,
    select_completion_record,
)
from issue_orchestrator.control.completion_result_artifacts import (
    preserve_completion_record,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

from tests.unit.session_run_helpers import make_session_run_assets

CANONICAL_NAME = "completion-agent_backend.json"
SESSION_ID = "issue-264-run-1"


class _NoGitAdapter:
    """The record reader never touches git; selection must not either."""

    def get_current_branch(self, worktree: Path) -> str | None:
        raise AssertionError("selection must not consult git")

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        raise AssertionError("selection must not consult git")

    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool:
        raise AssertionError("selection must not consult git")

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        raise AssertionError("selection must not consult git")


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "worktree" / ".issue-orchestrator" / "sessions" / "run-1"
    directory.mkdir(parents=True)
    return directory


def _write_record(
    path: Path,
    *,
    session_id: str = SESSION_ID,
    summary: str = "work done",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "timestamp": "2026-08-25T10:55:25",
                "outcome": "completed",
                "summary": summary,
                "requested_actions": ["push_branch"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_placeholder(
    path: Path,
    *,
    session_id: str = SESSION_ID,
    error: str = "follow_up evidence validation exploded",
) -> Path:
    """Exactly what ``write_error_completion`` leaves behind."""
    path.write_text(
        json.dumps(
            {
                "outcome": "completed",
                "agent_done_error": error,
                "session_id": session_id,
                "timestamp": "2026-08-25T10:55:25",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_missing_canonical_reports_nothing_to_parse(run_dir: Path) -> None:
    selection = select_completion_record(run_dir / CANONICAL_NAME)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.path == run_dir / CANONICAL_NAME
    assert selection.record is None
    assert selection.load_result.failure is CompletionRecordLoadFailure.MISSING
    assert selection.load_result.invalid is False


def test_valid_canonical_wins_and_siblings_are_ignored(run_dir: Path) -> None:
    """A suffixed sibling is a supported second review, not a takeover."""
    canonical = _write_record(run_dir / CANONICAL_NAME, summary="first")
    _write_record(run_dir / "completion-agent_backend-2.json", summary="second")

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.path == canonical
    assert selection.record is not None
    assert selection.record.summary == "first"
    assert selection.producer_error is None
    assert selection.unresolved_candidates == ()


def test_placeholder_hands_over_to_the_one_matching_retry(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    retry = _write_record(
        run_dir / "completion-agent_backend-2.json", summary="retried cleanly"
    )

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.PRODUCER_ERROR_RETRY
    assert selection.path == retry
    assert selection.canonical_path == canonical
    assert selection.record is not None
    assert selection.record.summary == "retried cleanly"
    # The failure that made the retry necessary is not erased by it.
    assert selection.producer_error == "follow_up evidence validation exploded"
    assert canonical.exists() and retry.exists()


def test_placeholder_evidence_reaches_the_log_at_info(
    run_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME, error="boom in evidence")
    _write_record(run_dir / "completion-agent_backend-2.json")

    with caplog.at_level("INFO"):
        select_completion_record(canonical)

    assert any(
        "boom in evidence" in record.getMessage()
        for record in caplog.records
        if record.levelname == "INFO"
    )


def test_retry_for_a_different_session_is_not_selected(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    _write_record(
        run_dir / "completion-agent_backend-2.json", session_id="issue-999-run-7"
    )

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.path == canonical
    assert selection.record is None
    assert selection.producer_error == "follow_up evidence validation exploded"


def test_placeholder_without_any_retry_stays_on_the_canonical_path(
    run_dir: Path,
) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.path == canonical
    assert selection.record is None
    assert selection.load_result.failure is CompletionRecordLoadFailure.INVALID_SCHEMA
    assert selection.producer_error == "follow_up evidence validation exploded"


def test_two_valid_retries_are_ambiguity_and_fail_closed(
    run_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No newest / lowest-suffix / mtime precedence is invented here."""
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    second = _write_record(run_dir / "completion-agent_backend-2.json", summary="a")
    third = _write_record(run_dir / "completion-agent_backend-3.json", summary="b")

    with caplog.at_level("ERROR"):
        selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.AMBIGUOUS_PRODUCER_ERROR_RETRY
    assert selection.path == canonical
    assert selection.record is None
    # Falls onto the existing rejected-record path rather than guessing.
    assert selection.load_result.failure is CompletionRecordLoadFailure.INVALID_SCHEMA
    assert set(selection.unresolved_candidates) == {second, third}
    assert selection.producer_error == "follow_up evidence validation exploded"
    assert any(
        "Ambiguous completion retry" in record.getMessage()
        for record in caplog.records
        if record.levelname == "ERROR"
    )


def test_oversized_retry_is_rejected_by_the_shared_gate(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    oversized = run_dir / "completion-agent_backend-2.json"
    oversized.write_text("x" * (_MAX_COMPLETION_FILE_BYTES + 1), encoding="utf-8")

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.path == canonical
    assert selection.record is None


def test_unparseable_retry_is_rejected(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    (run_dir / "completion-agent_backend-2.json").write_text(
        "{ not valid json", encoding="utf-8"
    )

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.record is None


def test_an_unreadable_record_is_a_load_failure_not_a_crash(run_dir: Path) -> None:
    """Stands in for any OS-level read failure on a polled path.

    The observer asks every tick, and post-processing cleanup unlinks
    the record, so "it existed a moment ago and cannot be read now" has
    to come back as a result rather than an exception.
    """
    canonical = run_dir / CANONICAL_NAME
    canonical.mkdir()

    selection = select_completion_record(canonical)

    assert selection.record is None
    assert selection.load_result.failure is CompletionRecordLoadFailure.UNREADABLE


def test_only_the_producers_own_suffix_names_are_candidates(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    _write_record(run_dir / "completion-agent_backend-latest.json")
    _write_record(run_dir / "completion-agent_reviewer.json")
    _write_record(run_dir / "completion-agent_backend-2.txt")

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.record is None


def test_rejected_canonical_without_producer_error_is_unchanged(run_dir: Path) -> None:
    """Only a producer-error placeholder can be superseded."""
    canonical = run_dir / CANONICAL_NAME
    canonical.write_text(json.dumps({"outcome": "completed"}), encoding="utf-8")
    _write_record(run_dir / "completion-agent_backend-2.json")

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.path == canonical
    assert selection.record is None
    assert selection.producer_error is None


def test_placeholder_missing_its_session_id_is_not_a_placeholder(
    run_dir: Path,
) -> None:
    canonical = run_dir / CANONICAL_NAME
    canonical.write_text(
        json.dumps({"outcome": "completed", "agent_done_error": "boom"}),
        encoding="utf-8",
    )
    _write_record(run_dir / "completion-agent_backend-2.json")

    selection = select_completion_record(canonical)

    assert selection.choice is CompletionPathChoice.CANONICAL
    assert selection.record is None
    assert selection.producer_error is None


def test_producer_error_text_is_bounded_before_it_travels(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME, error="E" * 50_000)

    selection = select_completion_record(canonical)

    assert selection.producer_error is not None
    assert len(selection.producer_error) < 5_000
    assert "truncated" in selection.producer_error


def test_lookup_fields_explain_the_choice(run_dir: Path) -> None:
    canonical = _write_placeholder(run_dir / CANONICAL_NAME)
    retry = _write_record(run_dir / "completion-agent_backend-2.json")

    fields = select_completion_record(canonical).lookup_fields()

    assert fields["completion_selected_path"] == str(retry.resolve())
    assert fields["completion_path_choice"] == "producer_error_retry"
    assert fields["completion_producer_error"] == (
        "follow_up evidence validation exploded"
    )
    assert fields["completion_unresolved_candidates"] == []


def test_preserved_audit_copy_is_the_record_that_was_acted_on(
    tmp_path: Path,
) -> None:
    """The run-scoped copy must not be a superseded placeholder (#264)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    run_assets = make_session_run_assets(worktree)
    completion_rel = ".issue-orchestrator/sessions/run-1/completion-agent_backend.json"
    canonical = worktree / completion_rel
    canonical.parent.mkdir(parents=True, exist_ok=True)
    _write_placeholder(canonical)
    _write_record(
        canonical.parent / "completion-agent_backend-2.json", summary="retried cleanly"
    )

    preserved = preserve_completion_record(
        session_output=FileSystemSessionOutput(),
        worktree=worktree,
        completion_path=completion_rel,
        run_assets=run_assets,
    )

    assert preserved is not None
    copied = json.loads(Path(preserved).read_text(encoding="utf-8"))
    assert copied["summary"] == "retried cleanly"
    assert "agent_done_error" not in copied


def test_preserved_audit_copy_is_canonical_when_nothing_supersedes_it(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    run_assets = make_session_run_assets(worktree)
    completion_rel = ".issue-orchestrator/sessions/run-1/completion-agent_backend.json"
    canonical = worktree / completion_rel
    canonical.parent.mkdir(parents=True, exist_ok=True)
    _write_record(canonical, summary="first")
    _write_record(canonical.parent / "completion-agent_backend-2.json", summary="second")

    preserved = preserve_completion_record(
        session_output=FileSystemSessionOutput(),
        worktree=worktree,
        completion_path=completion_rel,
        run_assets=run_assets,
    )

    assert preserved is not None
    copied = json.loads(Path(preserved).read_text(encoding="utf-8"))
    assert copied["summary"] == "first"


class TestValidatorRoutesThroughSelection:
    """The publish-path reader must see whatever the owner selected."""

    def test_read_result_returns_the_selected_retry(self, run_dir: Path) -> None:
        worktree = run_dir.parents[2]
        completion_rel = str((run_dir / CANONICAL_NAME).relative_to(worktree))
        _write_placeholder(run_dir / CANONICAL_NAME)
        retry = _write_record(
            run_dir / "completion-agent_backend-2.json", summary="retried cleanly"
        )
        validator = CompletionRecordValidator(config=None, git_adapter=_NoGitAdapter())

        result = validator.read_completion_record_result(worktree, completion_rel)

        assert result.path == retry
        assert result.record is not None
        assert result.record.summary == "retried cleanly"

    def test_select_matches_the_read_it_backs(self, run_dir: Path) -> None:
        worktree = run_dir.parents[2]
        completion_rel = str((run_dir / CANONICAL_NAME).relative_to(worktree))
        _write_placeholder(run_dir / CANONICAL_NAME)
        _write_record(run_dir / "completion-agent_backend-2.json")
        validator = CompletionRecordValidator(config=None, git_adapter=_NoGitAdapter())

        selection = validator.select_completion_record(worktree, completion_rel)

        assert selection.choice is CompletionPathChoice.PRODUCER_ERROR_RETRY
        assert (
            selection.load_result.path
            == validator.read_completion_record_result(worktree, completion_rel).path
        )
