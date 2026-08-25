"""Whether a completion's LOCAL quick-validation gate has a verdict to give (#293).

``coding-done completed`` runs the repository's configured *quick* contract
before it writes a completion record. That run is immediate feedback for the
agent — the canonical publication validation is a separate,
orchestrator-controlled gate that runs later — and for any session offering a
code candidate the feedback is worth what it costs.

A ``planning_investigation`` Tech Lead offers no code candidate. #289 made the
code-candidate gate technically unreachable from inside such a session, and the
very next live run proved the barrier was only half the blocker: the session
reached ``coding-done completed``, which ran that same gate *itself*, failed on
the same sandbox class the guard exists to avoid, and produced no completion
record at all. A lane that cannot complete while its agent follows its contract
perfectly is not a gate; it is a wall.

This module is the single owner of one question — *does this completion's local
quick gate have a verdict to contribute?* — and it answers only that. It
removes a local feedback gate; it grants nothing.

**The signal is a routing HINT, never authority.** The launch-time
:class:`~..domain.tech_lead_session.TechLeadAssignment` read here lives in the
run directory, which :class:`~..domain.session_run.SessionRunAssets` requires to
be *inside the agent-writable worktree* — so an agent can write one. That is
acceptable for this use and for no other: what a spoofed assignment buys is the
loss of the spoofer's own local feedback, which is authority over nothing. It
buys no publication, no zero-code settlement, no issue creation, and no
recovery. Those are decided downstream from the orchestrator-owned
:class:`~..domain.tech_lead_session.TechLeadLaunchAuthority` and the
orchestrator's own read of HEAD (#202/#257) — which never consult this answer,
and which reject outright a worktree assignment diverging from the recorded
authority (:func:`~.tech_lead_completion.resolve_tech_lead_launch_authority`).
Nothing decided here is written into the completion record, so there is no field
for a later reader to mistake for a verdict.

**Unreadable is never planning.** The gate is dropped only when the assignment
parses cleanly *and* names ``planning_investigation``. Missing, malformed,
unreadable, or any other flavor keeps today's behaviour exactly. That is the
safe direction: a needless gate run costs one session its time, while a wrongly
dropped one lets a code candidate reach the orchestrator with no agent-side
feedback at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.session_run import SessionRunAssets
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .tech_lead_session_policy import read_tech_lead_assignment

__all__ = ["QuickGateRouting", "route_completion_quick_gate"]


@dataclass(frozen=True, slots=True)
class QuickGateRouting:
    """Whether one completion runs its local quick gate, and why.

    ``detail`` is always populated, including on the ordinary path, so an
    operator reading a session log can see which fact produced the answer
    rather than inferring it from the gate's absence.
    """

    runs_quick_gate: bool
    detail: str


def route_completion_quick_gate(assets: SessionRunAssets) -> QuickGateRouting:
    """Decide the local quick gate for one ORCHESTRATOR-MANAGED completion.

    Args:
        assets: The owner-injected run contract for this session, already
            proven by ``require_orchestrator_run_assets_for_session`` to name
            this session and this worktree. The whole contract is taken rather
            than a bare ``run_dir`` precisely so out-of-run planning metadata is
            unrepresentable: there is no way in for a directory the
            orchestrator did not hand this session.

    Returns:
        The routing answer. Only a cleanly parsed ``planning_investigation``
        assignment drops the gate; every other outcome — including every
        failure to read one — keeps it.
    """
    try:
        assignment = read_tech_lead_assignment(assets.run_dir)
    except (OSError, ValueError) as exc:
        return QuickGateRouting(
            runs_quick_gate=True,
            detail=(
                "this run's tech_lead assignment could not be read"
                f" ({exc}); an unreadable hint is not a planning run"
            ),
        )
    if assignment is None:
        return QuickGateRouting(
            runs_quick_gate=True,
            detail="this run carries no tech_lead assignment",
        )
    if assignment.flavor is not TechLeadSessionFlavor.PLANNING_INVESTIGATION:
        return QuickGateRouting(
            runs_quick_gate=True,
            detail=(
                "this run's tech_lead assignment is"
                f" {assignment.flavor.value}"
            ),
        )
    return QuickGateRouting(
        runs_quick_gate=False,
        detail=(
            "this run's tech_lead assignment is"
            f" {TechLeadSessionFlavor.PLANNING_INVESTIGATION.value}, which"
            " prepares a bounded issue rather than a code candidate, so the"
            " code-candidate quick gate has no verdict to contribute"
        ),
    )
