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
