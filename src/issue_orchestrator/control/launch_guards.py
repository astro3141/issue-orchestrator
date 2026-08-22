"""Reasons a session launch is refused before any work happens.

Pure control policy over explicit arguments — no dependency bundle, no
infra — so these rules can be read and tested without standing up a
launcher, and so every launch flavor applies the same ones.

They live together because they share a contract: return a
``LaunchResult`` describing the refusal, or ``None`` to proceed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .session_launch_types import LaunchDisposition, LaunchResult
from .transition_log import log_transition

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint


def callback_endpoint_not_ready(
    endpoint: "AgentCallbackEndpoint",
) -> LaunchResult | None:
    """Defer while the agent callback endpoint is unresolved.

    Every flavor spawns an agent with the same callback-dependent
    completion environment, so every flavor must observe this rule — it
    previously lived inside one launcher's precondition helper, which
    review, retrospective-review and rework never reach (#6924 F7-R3).

    A deferral, and said so EXPLICITLY: the next tick launches once the
    server has published, or a run mode has declared that it serves no
    Control API. ``LaunchResult`` defaults to ``PERMANENT_FAILURE`` — "the
    launcher gave up" — so a deferral that took the default was settled by
    :class:`~.launch_transaction.LaunchSettlement` as a drop: the queued
    item removed and its durable claim retired, over a window that had not
    yet had a chance to close and a request nothing had failed about
    (#193).

    ``LAUNCH_DEFERRED`` is the member whose contract this refusal actually
    meets: it fires BEFORE anything is attempted, so no attempt failed and
    nothing was consumed, and the item waits with its budget intact.
    ``RETRYABLE_FAILURE`` — the nearest-looking alternative — would promise
    a bound this path cannot deliver: the guard refuses before the durable
    claim is ever held, so the ledger has no deferred row to spend against
    and the settlement declines to spend one in memory. The budget would
    never move, the "bound" would be fiction, and every deferral would take
    the branch that exists to announce a store fault.
    """
    if endpoint.is_ready():
        return None
    return LaunchResult(
        None, False,
        "Agent callback endpoint not published yet; deferring launch",
        disposition=LaunchDisposition.LAUNCH_DEFERRED,
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
