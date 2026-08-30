"""Whose validation a managed run's completion runs (#293 / #319 / #370).

``coding-done completed`` runs a *code-candidate* quick gate before it will
write a completion record. That gate is the right question for an Actor and for
a rework agent: they produce the candidate, and the gate is fast feedback to the
agent that produced it.

It is the wrong question for every Tech Lead run, for two reasons that are
different facts about different flavors and are both stated below:

* a ``planning_investigation`` produces no candidate at all. It prepares one
  bounded issue from source and staged governing evidence, and it is launched
  behind the #289 guard that technically refuses the very gate commands the
  quick contract would run. A planning run that reached the gate spent its
  round on a command its sandbox could not satisfy and returned without the
  work it was launched to do;
* every OTHER Tech Lead flavor adjudicates work it did not write, and the
  mandatory repository validation its completion rests on belongs to the
  ORCHESTRATOR (#370). #364 proved that this is not a preference: the Tech Lead
  model session could not complete at all under a bounded provider sandbox,
  because the repository validation path needs host/repository-owned effects —
  a write to the shared git common dir
  (``.git/issue-orchestrator/validate-timings.jsonl``) among them — outside the
  model's scratch write boundary. Widening the sandbox to admit them is the
  authority grant this repair exists to avoid.

This module is the single owner of that discrimination, and it owns exactly
one question: *given the owner-injected managed-run context, does this
completion run the code-candidate quick gate?* It answers from the launch-time
assignment staged in that run's own directory, and from nothing else.

**Nothing is skipped, only reassigned.** A Tech Lead run that does offer a
change for review still meets the repository's validation contract — through
the orchestrator's own publication gate, which runs on the completed worktree
in the orchestrator's process rather than in the model's sandbox, and which is
unconditional for anything that would be published
(``CompletionProcessor._check_publish_gate_if_required``). What a merge-facing
Tech Lead PASS rests on is separately bound to the exact audited candidate as
:class:`~..domain.tech_lead_candidate.CandidatePassPrerequisite.
REPOSITORY_VALIDATION`. Neither owner is the model session.

**The answer is a routing hint, and the boundary is deliberate.** The
assignment copy lives inside the agent-writable worktree, so an agent could
rewrite it. What it can buy by doing so is bounded to *skipping its own quick
gate* — a gate whose only product is fast feedback to that same agent. It buys
no publication, no label, no zero-code settlement and no effect: those keep
reading the orchestrator-owned :class:`~...domain.tech_lead_session.TechLeadLaunchAuthority`
record persisted outside the worktree, plus the orchestrator's own observation
of HEAD (#202/#257), plus the publish gate above. Nothing decided here is
written into the completion record, so nothing decided here can travel
downstream as authority. In the other direction the same boundary holds: an
Actor cannot route ITSELF out of its gate, because forging a tech-lead
assignment into its own run directory would not make the orchestrator treat its
completion as a tech-lead one.

**Fail-safe direction.** Every way of not knowing — no managed run context, a
run directory that does not prove out as this session's, no assignment, an
assignment that will not parse — routes to the ordinary candidate quick gate.
The unsafe error is skipping a real coder candidate's gate; refusing to skip
costs a tech-lead run one wasted gate, which is what the product did before
this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..domain.tech_lead_session import (
    TechLeadSessionFlavor,
    read_run_assignment,
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
    #: A Tech Lead run whose mandatory repository validation is the
    #: orchestrator's to execute, outside the model-provider sandbox (#370).
    #: Distinct from the planning route because it states a different fact: not
    #: "there is nothing to validate" but "the validation owner is not this
    #: session". A future reader deciding whether some new flavor belongs here
    #: has to answer which of the two is true of it.
    TECH_LEAD_ORCHESTRATOR_OWNED_VALIDATION = "tech_lead_orchestrator_owned_validation"


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
    try:
        assignment = read_run_assignment(run_dir)
    except (OSError, ValueError) as exc:
        # Malformed, truncated, not even a JSON object, or internally
        # inconsistent (a focused flavor with no focus issue is rejected by
        # the assignment's own contract). ValueError is the parser's total
        # contract for bad content, which is what makes this catch a
        # guarantee rather than a hope.
        logger.warning(
            "[completion-routing] the tech_lead assignment staged in run %s is "
            "unusable (%s); routing to the ordinary code-candidate quick gate",
            run_dir,
            exc,
        )
        return _ordinary("tech_lead assignment is unreadable")
    if assignment is None:
        return _ordinary("managed run carries no tech_lead assignment")
    if assignment.flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION:
        return CompletionGateRouting(
            route=CompletionGateRoute.PLANNING_NO_CANDIDATE_GATE,
            reason=(
                f"{TechLeadSessionFlavor.PLANNING_INVESTIGATION.value} run prepares "
                "an issue and produces no code candidate to validate"
            ),
        )
    return CompletionGateRouting(
        route=CompletionGateRoute.TECH_LEAD_ORCHESTRATOR_OWNED_VALIDATION,
        reason=(
            f"mandatory repository validation for a {assignment.flavor.value} run"
            " is executed by the orchestrator outside the model-provider sandbox,"
            " not by this session"
        ),
    )
