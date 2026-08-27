"""Tests for Control Center Repository Engine activity evidence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.execution.control_center_worktree_audit import (
    RepositoryEngineWorktreeActivityReader,
)
from issue_orchestrator.infra.supervisor import MultiInstanceStatus, SupervisorStatus


def test_activity_reader_collects_worktrees_from_every_live_engine(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(state="running", port=18080),
            SupervisorStatus(state="running", port=18081),
        ],
    )
    status_reader = SimpleNamespace(
        read_status=lambda port: {
            "startup_status": "complete",
            "active_sessions": [
                {"worktree_path": str(tmp_path / f"project-{port}")}
            ]
        }
    )

    evidence = RepositoryEngineWorktreeActivityReader(
        supervisor,
        status_reader,
    ).read(tmp_path, RepositoryLaunchSelection.default())

    assert evidence.active_paths == frozenset({
        (tmp_path / "project-18080").resolve(),
        (tmp_path / "project-18081").resolve(),
    })


def test_activity_reader_fails_closed_when_live_status_lacks_worktree_path(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[SupervisorStatus(state="running", port=18080)],
    )
    status_reader = SimpleNamespace(
        read_status=lambda _port: {
            "startup_status": "complete",
            "active_sessions": [{"issue_number": 41}],
        }
    )

    evidence = RepositoryEngineWorktreeActivityReader(
        supervisor,
        status_reader,
    ).read(tmp_path, RepositoryLaunchSelection.default())

    assert evidence.active_paths is None


def test_activity_reader_fails_closed_while_engine_is_initializing(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[SupervisorStatus(state="running", port=18080)],
    )
    status_reader = SimpleNamespace(
        read_status=lambda _port: {
            "startup_status": "running",
            "active_sessions": [],
        }
    )

    evidence = RepositoryEngineWorktreeActivityReader(
        supervisor,
        status_reader,
    ).read(tmp_path, RepositoryLaunchSelection.default())

    assert evidence.active_paths is None


def test_activity_reader_fails_closed_when_startup_status_is_missing(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[SupervisorStatus(state="running", port=18080)],
    )
    status_reader = SimpleNamespace(
        read_status=lambda _port: {"active_sessions": []}
    )

    evidence = RepositoryEngineWorktreeActivityReader(
        supervisor,
        status_reader,
    ).read(tmp_path, RepositoryLaunchSelection.default())

    assert evidence.active_paths is None


def test_activity_reader_fails_closed_on_the_int_active_sessions_shape(
    tmp_path: Path,
) -> None:
    """A bare count is not evidence about individual worktrees.

    ``/api/status`` also ships ``active_sessions`` as an int count. Count-only
    consumers normalize that shape (see
    ``execution/repository_engine_status_payload``), but path-level activity
    needs the session rows themselves, so the int form must stay unknown here.
    """
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[SupervisorStatus(state="running", port=18080)],
    )
    status_reader = SimpleNamespace(
        read_status=lambda _port: {
            "startup_status": "complete",
            "active_sessions": 0,
            "sessions": [],
        }
    )

    evidence = RepositoryEngineWorktreeActivityReader(
        supervisor,
        status_reader,
    ).read(tmp_path, RepositoryLaunchSelection.default())

    assert evidence.active_paths is None
