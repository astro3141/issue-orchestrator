"""Typed reading of external Repository Engine ``/api/status`` payloads.

Two legitimate producers publish ``active_sessions`` at different shapes:
``entrypoints/control_api.py`` publishes an ``int`` count (with the detailed
rows under ``sessions``), while ``entrypoints/web_status_routes.py`` publishes
the ``list`` of session rows. Consumers that only need "how many sessions are
running" must accept both without crashing and without inventing a count.

This module owns exactly that count reading. Consumers that need *path-level*
session detail (see ``control_center_worktree_audit``) must keep requiring the
list form: a bare count is not evidence about individual worktrees.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass

ACTIVE_SESSION_COUNT_KEY = "active_session_count"


@dataclass(frozen=True)
class ActiveSessionCount:
    """A known active-session count, or an explicit unknown state."""

    count: int | None

    @classmethod
    def known(cls, count: int) -> ActiveSessionCount:
        if count < 0:
            raise ValueError(f"active session count cannot be negative: {count}")
        return cls(count)

    @classmethod
    def unknown(cls) -> ActiveSessionCount:
        return cls(None)

    @property
    def is_known(self) -> bool:
        return self.count is not None


def read_active_session_count(payload: Mapping[str, object]) -> ActiveSessionCount:
    """Read the active-session count from an engine status payload.

    Accepts both wire shapes: a list of session rows (counted) and a
    non-negative integer count (taken as-is). Every other value - booleans,
    negative integers, strings, mappings, or an absent key - is unknown rather
    than a silent zero or an exception.
    """
    value = payload.get("active_sessions")
    if isinstance(value, list):
        return ActiveSessionCount.known(len(value))
    if isinstance(value, bool):
        return ActiveSessionCount.unknown()
    if isinstance(value, int) and value >= 0:
        return ActiveSessionCount.known(value)
    return ActiveSessionCount.unknown()


def publish_active_session_count(
    status_payload: MutableMapping[str, object],
    engine_payload: Mapping[str, object],
) -> None:
    """Publish ``active_session_count`` only when the engine states one.

    An unknown count leaves the key absent - the same explicit "no count"
    state a Control Center status payload already carries when the engine
    cannot be probed at all. A count is never manufactured.
    """
    active_sessions = read_active_session_count(engine_payload)
    if active_sessions.count is None:
        status_payload.pop(ACTIVE_SESSION_COUNT_KEY, None)
        return
    status_payload[ACTIVE_SESSION_COUNT_KEY] = active_sessions.count


__all__ = [
    "ACTIVE_SESSION_COUNT_KEY",
    "ActiveSessionCount",
    "publish_active_session_count",
    "read_active_session_count",
]
