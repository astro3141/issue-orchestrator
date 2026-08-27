"""Which completion gate one managed run routes to (#293 / #319).

``coding-done completed`` runs a *code-candidate* quick gate before it will
write a completion record. That gate is the right question for an Actor, for a
rework agent, and for every Tech Lead flavor whose session can produce a code
candidate at all. It is the wrong question for a ``planning_investigation``
run: that flavor prepares one bounded issue from source and staged governing
evidence, produces no candidate, and is launched behind the #289 guard that
technically refuses the very gate commands the quick contract would run. A
planning run that reached the gate spent its round on a command its sandbox
could not satisfy and returned without the work it was launched to do.

This module is the single owner of that discrimination, and it owns exactly
one question: *given the owner-injected managed-run context, does this
completion run the code-candidate quick gate?* It answers from the launch-time
assignment staged in that run's own directory, and from nothing else.

**The answer is a routing hint, and the boundary is deliberate.** The
assignment copy lives inside the agent-writable worktree, so an agent could
rewrite it. What it can buy by doing so is bounded to *skipping its own quick
gate* — a gate whose only product is fast feedback to that same agent. It buys
no publication, no label, no zero-code settlement and no effect: those keep
reading the orchestrator-owned :class:`~...domain.tech_lead_session.TechLeadLaunchAuthority`
record persisted outside the worktree, plus the orchestrator's own observation
of HEAD (#202/#257). Nothing decided here is written into the completion
record, so nothing decided here can travel downstream as authority.

**Fail-safe direction.** Every way of not knowing — no managed run context, a
run directory that does not prove out as this session's, no assignment, an
assignment that will not parse, a flavor that is not planning — routes to the
ordinary candidate quick gate. The unsafe error is skipping a real candidate's
gate; refusing to skip costs a planning run one wasted gate, which is what the
product did before this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..domain.tech_lead_session import (
    TechLeadAssignment,
    TechLeadSessionFlavor,
    tech_lead_assignment_path,
)

logger = logging.getLogger(__name__)


class CompletionGateRoute(Enum):
    """What ``coding-done completed`` does about the quick gate."""

    #: Read the quick-validation configuration and run the gate, as ever.
    CANDIDATE_QUICK_GATE = "candidate_quick_gate"
    #: A planning run: no candidate, so no candidate gate — and no gate
    #: configuration is read either, because reading it is the first half of
    #: running it.
    PLANNING_NO_CANDIDATE_GATE = "planning_no_candidate_gate"


@dataclass(frozen=True, slots=True)
class CompletionGateRouting:
    """The route this completion takes, and the evidence it took it on."""

    route: CompletionGateRoute
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(
                "a CompletionGateRouting must say what it was decided from"
            )

    @property
    def runs_candidate_quick_gate(self) -> bool:
        """True when the candidate quick contract must be read and run."""
        return self.route is CompletionGateRoute.CANDIDATE_QUICK_GATE


def _ordinary(reason: str) -> CompletionGateRouting:
    return CompletionGateRouting(
        route=CompletionGateRoute.CANDIDATE_QUICK_GATE, reason=reason
    )


def route_completion_gate(run_dir: Path | None) -> CompletionGateRouting:
    """Route one completion from its owner-injected managed-run directory.

    Args:
        run_dir: The run directory the session owner injected, already proven
            to belong to this worktree and this session. ``None`` for a
            standalone invocation, and for a managed run whose injected
            context did not prove out — the caller does not get to hand over
            a directory it merely found.

    Returns:
        The route, never an exception: every unreadable, absent or ambiguous
        signal resolves to the ordinary candidate quick gate.
    """
    if run_dir is None:
        return _ordinary("no owner-injected managed-run context")
    assignment_path = tech_lead_assignment_path(run_dir)
    if not assignment_path.is_file():
        return _ordinary("managed run carries no tech_lead assignment")
    try:
        assignment = TechLeadAssignment.read(assignment_path)
    except (OSError, ValueError) as exc:
        # Malformed, truncated, or internally inconsistent (a focused flavor
        # with no focus issue is rejected by the assignment's own contract).
        logger.warning(
            "[completion-routing] tech_lead assignment at %s is unusable (%s); "
            "routing to the ordinary code-candidate quick gate",
            assignment_path,
            exc,
        )
        return _ordinary("tech_lead assignment is unreadable")
    if assignment.flavor is not TechLeadSessionFlavor.PLANNING_INVESTIGATION:
        return _ordinary(
            f"tech_lead flavor {assignment.flavor.value} produces a code candidate"
        )
    return CompletionGateRouting(
        route=CompletionGateRoute.PLANNING_NO_CANDIDATE_GATE,
        reason=(
            f"{TechLeadSessionFlavor.PLANNING_INVESTIGATION.value} run prepares "
            "an issue and produces no code candidate to validate"
        ),
    )
