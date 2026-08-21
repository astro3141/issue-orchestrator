"""Which logical run a queued item or a live session belongs to (#6994, #136).

Extracted from :mod:`.tech_lead_run_admission`, which owns the POLICY question
("may this run start?"). Answering that policy question first requires a
different, purely descriptive one: given a queued item or a running session,
WHICH logical run is this, and what do we call it? That classification has more
readers than admission has — the launch gate, the launch-authority recorder, the
termination path, the ownership reconciler, the dashboard's run affordances —
and every one of them consulted it through the policy owner, so the policy
module was the de-facto home for vocabulary its own decisions barely used.

Everything here is a pure function of already-observed facts: no GitHub reads,
no state mutation, no verdicts. The one rule it enforces is that a queued item,
a restored session, and a fresh request are classified by the SAME map
(:func:`...domain.tech_lead_run.scope_for_flavor`), so the three can never
disagree about which run they are talking about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

from ..domain.models import PendingTechLeadReview
from ..domain.tech_lead_run import (
    TechLeadRunScope,
    TechLeadRunScopeKind,
    scope_for_flavor,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..infra.config import Config


def scope_of_pending(item: PendingTechLeadReview) -> TechLeadRunScope:
    """The scope a queued item runs at, derived from its declared flavor.

    Health reviews AND batch reviews audit the whole repository (one walks the
    board, the other the accumulated PR manifest), so both are global — but they
    are DIFFERENT global identities, not one bucket (#6994 round 1 F2). Both are
    exclusive of every other run; neither deduplicates against the other. The
    two issue-scoped flavors are likewise two identities on one subject (#136).
    """
    return scope_for_flavor(item.flavor, issue_number=item.issue_number)


def is_global_pending(item: PendingTechLeadReview) -> bool:
    """True when a queued item holds an exclusive whole-repository scope."""
    return scope_of_pending(item).kind.is_global


def run_key_of_pending(item: PendingTechLeadReview) -> str:
    """The logical run identity of a queued item."""
    return scope_of_pending(item).run_key


def scope_of_session(session: "Session") -> Optional[TechLeadRunScope]:
    """The scope an ACTIVE tech-lead session is running at, or None.

    ``None`` only when the session carries no launch stamp at all, which after
    #6994 round 1 F3 means a session whose flavor could not be recovered — not
    the routine restart case, which now restores the stamp from marker truth.
    """
    scope = session.tech_lead_scope
    if scope is None:
        return None
    return scope_for_flavor(scope.flavor, issue_number=session.issue.number)


def running_session_for_run(
    config: "Config", active_sessions: "Sequence[Session]", run_key: str
) -> "Optional[Session]":
    """The active tech-lead session executing this logical run, if any."""
    for session in active_tech_lead_sessions(config, active_sessions):
        scope = scope_of_session(session)
        if scope is not None and scope.run_key == run_key:
            return session
    return None


def queued_item_for_run(
    pending: "Sequence[PendingTechLeadReview]", run_key: str
) -> Optional[PendingTechLeadReview]:
    """The queued item that IS this logical run, if any."""
    return next(
        (item for item in pending if run_key_of_pending(item) == run_key), None
    )


def slot_occupant(
    pending: "Sequence[PendingTechLeadReview]", issue_number: int
) -> Optional[PendingTechLeadReview]:
    """The queued item holding an issue's single tech-lead queue slot, if any.

    Deliberately keyed by ISSUE NUMBER rather than by run key, because that is
    how the queue itself deduplicates: it holds at most one item per issue. A
    run key answers "is this my run?" — this answers the different question
    "could my run even be queued?", and the two differ precisely when a
    DIFFERENT run occupies the subject's slot (a batch anchor sharing the
    number, or the other focused flavor of the same issue — #136).
    """
    return next(
        (item for item in pending if item.issue_number == issue_number), None
    )


def subject_slot_holder(
    config: "Config",
    pending: "Sequence[PendingTechLeadReview]",
    active_sessions: "Sequence[Session]",
    *,
    issue_number: int,
    run_key: str,
) -> Optional[str]:
    """Name a DIFFERENT tech-lead run occupying an issue's slot, else None.

    One issue supports ONE tech-lead run at a time, whichever flavor: the queue
    holds a single item per issue number and every variant launches as the same
    ``issue-{N}`` session. Run IDENTITY is finer than that (#136) — a planning
    run and an investigation of one issue are two runs — so identity alone
    cannot answer "may this run be admitted?", and a run admitted past an
    occupied slot would either be silently dropped by the queue's dedup or
    become a second session on one subject.

    The active side is checked first because it is the stronger claim: a run
    that is already executing cannot be waited out by anything the queue does.
    Returned as the operator-facing NAME of the holder rather than its scope,
    because a live session whose flavor could not be recovered still holds the
    slot and has no scope to report.
    """
    running = next(
        (
            session
            for session in active_tech_lead_sessions(config, active_sessions)
            if session.issue.number == issue_number
        ),
        None,
    )
    if running is not None:
        scope = scope_of_session(running)
        if scope is None:
            return "A tech-lead session"
        if scope.run_key != run_key:
            return scope_phrase(scope)
    occupant = slot_occupant(pending, issue_number)
    if occupant is not None and run_key_of_pending(occupant) != run_key:
        return scope_phrase(scope_of_pending(occupant))
    return None


def live_run_scopes(
    config: "Config",
    pending: "Sequence[PendingTechLeadReview]",
    active_sessions: "Sequence[Session]",
) -> tuple[TechLeadRunScope, ...]:
    """Every logical run this engine currently has queued or running.

    The input to :meth:`.tech_lead_run_ownership.TechLeadRunOwnership.reconcile`:
    ownership must cover a run for its WHOLE life, and a run's life spans the
    queue and the session, so neither collection alone is the answer. Scopes
    rather than bare keys, because the shared ledger judges the CONFLICT MATRIX
    and cannot do that without knowing which runs are whole-repository ones.
    """
    scopes: dict[str, TechLeadRunScope] = {
        run_key_of_pending(item): scope_of_pending(item) for item in pending
    }
    for session in active_tech_lead_sessions(config, active_sessions):
        scope = scope_of_session(session)
        if scope is not None:
            scopes.setdefault(scope.run_key, scope)
    return tuple(scopes[key] for key in sorted(scopes))


def active_tech_lead_sessions(
    config: "Config", active_sessions: "Sequence[Session]"
) -> tuple["Session", ...]:
    """The active sessions that ARE tech-lead runs (ADR-0031 identity rule)."""
    return tuple(
        session
        for session in active_sessions
        if is_tech_lead_session(config.tech_lead_review_agent, session.agent_label)
    )


def has_active_global_run(
    config: "Config", active_sessions: "Sequence[Session]"
) -> bool:
    """True when a whole-repository tech-lead run is executing right now.

    Read from the launch scope stamped onto the session. That stamp is present
    on a RESTORED session too (``SessionRestorer`` rebuilds it from the recorded
    launch authority and the anchor's marker label — #6994 round 1 F3), so a
    global run survives a restart as a barrier instead of silently becoming
    issue-scoped and letting targeted work run alongside it.

    A tech-lead session with no stamp at all is a session whose flavor could not
    be established. It is treated as GLOBAL — the conservative direction: the
    cost of being wrong is a targeted run waiting a while, whereas failing the
    other way runs work concurrently with an exclusive review.
    """
    for session in active_tech_lead_sessions(config, active_sessions):
        scope = scope_of_session(session)
        if scope is None or scope.kind.is_global:
            return True
    return False


# Operator-facing name of each whole-repository run. One map so the admission
# detail text, the launch log, and the dashboard all call the same run the same
# thing rather than each inventing a phrase.
_GLOBAL_RUN_LABELS: dict[TechLeadRunScopeKind, str] = {
    TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW: "board health review",
    TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW: "batch review",
}


def global_run_label(kind: TechLeadRunScopeKind) -> str:
    """The operator-facing name of a whole-repository run."""
    return _GLOBAL_RUN_LABELS[kind]


def scope_phrase(scope: TechLeadRunScope) -> str:
    """How a scope is named in operator-facing detail text.

    Named per run FAMILY, not per kind: the two issue-scoped families share a
    kind, and telling an operator that "an investigation of #109 is already
    running" when what runs is a planning session would describe the wrong work.
    """
    if scope.kind.is_global:
        return f"A {global_run_label(scope.kind)}"
    return f"{_focused_run_phrase(scope)} of issue #{scope.subject_issue_number}"


def _focused_run_phrase(scope: TechLeadRunScope) -> str:
    if scope.flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION:
        return "A tech-lead planning investigation"
    return "A tech-lead investigation"


def already_running_detail(scope: TechLeadRunScope) -> str:
    return f"{scope_phrase(scope)} is already running."


def already_queued_detail(scope: TechLeadRunScope) -> str:
    return f"{scope_phrase(scope)} is already queued."


__all__ = [
    "active_tech_lead_sessions",
    "already_queued_detail",
    "already_running_detail",
    "global_run_label",
    "has_active_global_run",
    "is_global_pending",
    "live_run_scopes",
    "queued_item_for_run",
    "run_key_of_pending",
    "running_session_for_run",
    "scope_of_pending",
    "scope_of_session",
    "scope_phrase",
    "slot_occupant",
    "subject_slot_holder",
]
