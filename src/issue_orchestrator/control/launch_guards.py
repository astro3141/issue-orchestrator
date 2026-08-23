"""Reasons a session launch is refused before any work happens.

Pure control policy over explicit arguments — no dependency bundle, no
infra — so these rules can be read and tested without standing up a
launcher, and so every launch flavor applies the same ones.

They live together because they share a contract: return a
``LaunchResult`` describing the refusal, or ``None`` to proceed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from .session_launch_types import LaunchDisposition, LaunchResult
from .transition_log import log_transition

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint

logger = logging.getLogger(__name__)

CALLBACK_ENDPOINT_DEFERRAL_REASON = (
    "Agent callback endpoint not published yet; deferring launch"
)


def callback_endpoint_not_ready(
    endpoint: "AgentCallbackEndpoint",
) -> LaunchResult | None:
    """Defer while the agent callback endpoint is unresolved.

    Every flavor spawns an agent with the same callback-dependent
    completion environment, so every flavor must observe this rule — it
    previously lived inside one launcher's precondition helper, which
    review, retrospective-review and rework never reach (#6924 F7-R3).

    Retryable, and now says so: the refusal carries
    ``RETRYABLE_FAILURE`` explicitly rather than inheriting
    ``LaunchResult``'s ``PERMANENT_FAILURE`` default, which sent this
    branch — alone among the guards here — down the settlement's
    destructive path. A deferral that drops the pending item and retires
    its durable claim is not a deferral: the next tick had nothing left
    to launch once the server published (#193). The disposition the
    queue owner already reserves for "this attempt failed and may work
    next time, on a bounded budget" is exactly what a race against the
    server's publish is.

    The reason is logged here rather than left to the settlement. A
    retained item is reported by its queue owner as a generic retryable
    failure and a dropped one said nothing at all, so the one fact that
    explains both — *which* precondition refused — never reached an
    operator between "session starting" and "launch declined" (#193).
    """
    if endpoint.is_ready():
        return None
    logger.warning("[LAUNCH] %s", CALLBACK_ENDPOINT_DEFERRAL_REASON)
    return LaunchResult(
        None, False,
        CALLBACK_ENDPOINT_DEFERRAL_REASON,
        disposition=LaunchDisposition.RETRYABLE_FAILURE,
    )


def retrospective_session_conflict(
    session_name: str,
    issue_number: int,
    active_sessions: list["Session"],
    *,
    session_exists: Callable[[str], bool],
) -> LaunchResult | None:
    """Whether a retrospective review for this issue is already live.

    Two conflicts with different queue semantics: an in-flight session
    drops the request, while a lingering terminal keeps it queued for a
    later tick.
    """
    if any(s.terminal_id == session_name for s in active_sessions):
        log_transition(
            "retrospective-review", issue_number, "QUEUED", "SKIP",
            "already in active_sessions",
        )
        return LaunchResult(None, False, "Already in active sessions")
    if session_exists(session_name):
        log_transition(
            "retrospective-review", issue_number, "QUEUED", "SKIP",
            "terminal session already running",
        )
        return LaunchResult(
            None,
            False,
            "Terminal session already running",
            disposition=LaunchDisposition.EXISTING_TERMINAL,
        )
    return None
