"""Tests for reading ``active_sessions`` from external engine status payloads.

Two shipped producers publish the same key at different shapes: the control API
publishes an ``int`` count, the web status route publishes the ``list`` of
session rows. Count-only consumers must accept both, and must never turn a
value they cannot interpret into a confident zero.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.repository_engine_status_payload import (
    ActiveSessionCount,
    build_orphaned_engine_status,
    publish_active_session_count,
    read_active_session_count,
)


class TestReadActiveSessionCount:
    """Both real producer shapes, and nothing else, yield a known count."""

    def test_empty_list_shape_is_zero(self) -> None:
        count = read_active_session_count({"active_sessions": []})

        assert count.is_known
        assert count.count == 0

    def test_populated_list_shape_counts_rows(self) -> None:
        payload = {
            "active_sessions": [
                {"issue_number": 41, "worktree_path": "/tmp/wt-41"},
                {"issue_number": 42, "worktree_path": "/tmp/wt-42"},
            ]
        }

        count = read_active_session_count(payload)

        assert count.is_known
        assert count.count == 2

    def test_zero_int_shape_is_known_zero(self) -> None:
        count = read_active_session_count({"active_sessions": 0})

        assert count.is_known
        assert count.count == 0

    def test_positive_int_shape_is_taken_as_the_count(self) -> None:
        count = read_active_session_count({"active_sessions": 3})

        assert count.is_known
        assert count.count == 3

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            -1,
            "2",
            "",
            {"issue_number": 41},
            None,
            3.0,
            ({"issue_number": 41},),
        ],
        ids=[
            "true",
            "false",
            "negative-int",
            "numeric-string",
            "empty-string",
            "mapping",
            "null",
            "float",
            "tuple",
        ],
    )
    def test_invalid_values_are_unknown_not_zero(self, value: object) -> None:
        count = read_active_session_count({"active_sessions": value})

        assert not count.is_known
        assert count.count is None

    def test_absent_key_is_unknown_not_zero(self) -> None:
        count = read_active_session_count({"shutdown_requested": True})

        assert not count.is_known
        assert count.count is None

    def test_known_rejects_a_negative_count(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            ActiveSessionCount.known(-1)

    def test_direct_construction_also_rejects_a_negative_count(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            ActiveSessionCount(-1)


class TestPublishActiveSessionCount:
    """Publication never manufactures a count the engine did not state."""

    def test_publishes_count_from_the_int_shape(self) -> None:
        status_payload: dict[str, object] = {"state": "running"}

        publish_active_session_count(status_payload, {"active_sessions": 2})

        assert status_payload["active_session_count"] == 2

    def test_publishes_count_from_the_list_shape(self) -> None:
        status_payload: dict[str, object] = {"state": "running"}

        publish_active_session_count(
            status_payload, {"active_sessions": [{"issue_number": 41}]}
        )

        assert status_payload["active_session_count"] == 1

    def test_unknown_count_leaves_the_key_absent(self) -> None:
        status_payload: dict[str, object] = {"state": "running"}

        publish_active_session_count(status_payload, {"active_sessions": "many"})

        assert "active_session_count" not in status_payload

    def test_unknown_count_removes_a_previously_published_count(self) -> None:
        status_payload: dict[str, object] = {"active_session_count": 4}

        publish_active_session_count(status_payload, {})

        assert "active_session_count" not in status_payload


def _detected_engine(**overrides: object) -> dict[str, object]:
    """A ``detect_repository_orchestrators`` entry for a live engine."""
    detected: dict[str, object] = {
        "port": 8770,
        "health": "healthy",
        "tick_age_seconds": 12.5,
        "status": {"shutdown_requested": False, "active_sessions": 2},
        "info": {
            "configuration_mode": "default",
            "config_name": "selfhost.yaml",
            "config_fingerprint": "abc123",
        },
    }
    detected.update(overrides)
    return detected


class TestBuildOrphanedEngineStatus:
    """One owner builds the orphaned-engine payload both seams publish."""

    def test_builds_the_supervisor_shaped_payload_for_a_live_engine(self) -> None:
        payload = build_orphaned_engine_status(_detected_engine())

        assert payload == {
            "state": "running",
            "pid": None,
            "port": 8770,
            "started_at": None,
            "recovered": False,
            "error": None,
            "orphaned": True,
            "health": "healthy",
            "tick_age_seconds": 12.5,
            "shutdown_requested": False,
            "active_session_count": 2,
        }

    def test_configuration_identity_is_omitted_by_default(self) -> None:
        payload = build_orphaned_engine_status(_detected_engine())

        assert "configuration_mode" not in payload
        assert "config_name" not in payload
        assert "config_fingerprint" not in payload

    def test_configuration_identity_is_published_when_requested(self) -> None:
        payload = build_orphaned_engine_status(
            _detected_engine(), include_configuration_identity=True
        )

        assert payload["configuration_mode"] == "default"
        assert payload["config_name"] == "selfhost.yaml"
        assert payload["config_fingerprint"] == "abc123"

    def test_missing_configuration_identity_reads_as_null(self) -> None:
        payload = build_orphaned_engine_status(
            _detected_engine(info={}), include_configuration_identity=True
        )

        assert payload["configuration_mode"] is None
        assert payload["config_name"] is None
        assert payload["config_fingerprint"] is None

    def test_list_shape_active_sessions_is_counted(self) -> None:
        payload = build_orphaned_engine_status(
            _detected_engine(
                status={"active_sessions": [{"issue_number": 41}], "x": 1}
            )
        )

        assert payload["active_session_count"] == 1

    def test_an_unstated_count_is_omitted_rather_than_zeroed(self) -> None:
        payload = build_orphaned_engine_status(
            _detected_engine(status={"shutdown_requested": True})
        )

        assert "active_session_count" not in payload
        assert payload["shutdown_requested"] is True

    def test_an_unprobeable_engine_status_still_yields_a_payload(self) -> None:
        payload = build_orphaned_engine_status(
            _detected_engine(status={}, health=None, tick_age_seconds=None)
        )

        assert payload["state"] == "running"
        assert payload["orphaned"] is True
        assert payload["shutdown_requested"] is False
        assert "active_session_count" not in payload

    def test_absent_health_reads_as_unknown(self) -> None:
        detected = _detected_engine()
        del detected["health"]

        payload = build_orphaned_engine_status(detected)

        assert payload["health"] == "unknown"

    def test_a_probe_without_a_port_fails_loudly(self) -> None:
        detected = _detected_engine()
        del detected["port"]

        with pytest.raises(KeyError):
            build_orphaned_engine_status(detected)
