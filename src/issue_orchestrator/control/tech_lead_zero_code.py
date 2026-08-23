"""Whether a finished planning run offers a change at all, and what follows.

A ``planning_investigation`` is sent to read an issue and propose work. It is
launched into a disposable scratch checkout, it is not asked to write code, and
the run that proved this seam mattered wrote none: its checkout stood at exactly
the commit it was handed. Yet the completion it produced asked for
``push_branch`` + ``create_pr`` like any coder's, because the completion CLI
gives every ``completed`` the same publication intent — so the run was held to
the code-candidate publish contract, and the planning actions it had already
been authorized to take never settled (#202).

This module answers one question — *did this run change any code?* — and, when
the answer is a proven no, removes the publication intent the run never meant.
What is left settles through the ordinary tech_lead effect path, unchanged.

**The answer must be PROVEN, never assumed.** Six facts are required, and the
absence of any one of them is a refusal rather than a benefit of the doubt:

1. the AUTHORITATIVE flavor is ``planning_investigation`` — read from the
   orchestrator-owned launch authority, never from the agent-visible copy;
2. that authority carries a ``launch_base_sha``. A row written before the field
   existed carries none, and no reader may infer, guess, or backfill one;
3. the completion-time HEAD read succeeded;
4. it is the SAME commit the run was launched on;
5. the tracked-dirt enumeration succeeded;
6. it found nothing.

Unobservable is never read as zero-code, for the reason
:mod:`.candidate_integrity` states about the same two reads: a checkout whose
state could not be read is not a checkout that was proven unchanged.

**Neither vocabulary this module uses is re-answered here.** *Which actions
publish* belongs to :data:`~..domain.models.PUBLICATION_ACTIONS`, and the
dropping is stated as intent via
:func:`~..domain.models.without_publication_intent`, so an action that joins
the publication family joins it for this lane too, in one edit.

**What counts as dirt is not decided here.** ``list_dirty_files`` owns that
vocabulary and is asked for :data:`~.candidate_integrity.CANDIDATE_DIRT_MODE`,
the same tracked-content question the candidate postflight asks. Untracked
files are excluded at the source, because a session run legitimately writes
untracked artifacts into its own checkout; every tracked path the mode reports
is judged, with no runtime-path filter on top — a *tracked* file the run
modified is a change to the repository whatever its path spells.

**Ordering is load-bearing and belongs to the caller.** This runs only after a
completion's launch authority and decision have been validated. A malformed,
tampered, or unauthorized completion must produce zero effects, and suppressing
its publication intent first would quietly convert a rejection into a settled
zero-code run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.models import RequestedAction, without_publication_intent
from ..domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor
from .candidate_integrity import CANDIDATE_DIRT_MODE

_DIRT_PREVIEW = 5
"""How many altered paths a refusal names before summarising."""


class ZeroCodeWorktreeReader(Protocol):
    """The two orchestrator-side reads that decide the lane.

    Deliberately the narrowest contract this owner needs, and satisfied by the
    completion path's existing git adapter — no new port, and no rummaging
    through a broader one for two methods.
    """

    def get_head_sha(self, worktree: Path) -> str | None:
        """The commit the checkout stands at, or ``None`` when unreadable."""
        ...

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        """Dirty paths for ``mode``, or ``None`` when enumeration failed."""
        ...


@dataclass(frozen=True, slots=True)
class ZeroCodePlanningSettlement:
    """What a completed tech_lead run's publication intent settles to.

    ``requested_actions`` is what the caller should carry forward: the shaped
    tuple when the zero-code lane applies, and the caller's own tuple untouched
    when it does not. ``detail`` always says why, so an operator reading the
    log of a run that took the ordinary path sees which fact was missing.
    """

    zero_code: bool
    detail: str
    requested_actions: tuple[RequestedAction, ...]


def settle_zero_code_planning_completion(
    *,
    authority: TechLeadLaunchAuthority,
    requested_actions: tuple[RequestedAction, ...],
    worktree: Path,
    worktree_reader: ZeroCodeWorktreeReader,
) -> ZeroCodePlanningSettlement:
    """Decide the lane for one VALIDATED tech_lead completion.

    Args:
        authority: The orchestrator-owned launch record this run was admitted
            under — already loaded and already verified by the caller.
        requested_actions: What the completion asked for, after any earlier
            tech_lead shaping.
        worktree: The run's checkout, read as it is right now.
        worktree_reader: The two reads above.

    Returns:
        The settlement. Every flavor other than ``planning_investigation``, and
        every planning run whose zero-code status is not fully proven, gets its
        requested actions back unchanged and keeps today's behaviour.
    """
    refusal = _zero_code_refusal(
        authority, worktree=worktree, worktree_reader=worktree_reader
    )
    if refusal is not None:
        return ZeroCodePlanningSettlement(False, refusal, requested_actions)
    return ZeroCodePlanningSettlement(
        True,
        (
            "planning run left its checkout at the commit it was launched on "
            f"({authority.launch_base_sha}) with no tracked change; it offers "
            "no code candidate, so its publication intent is dropped"
        ),
        without_publication_intent(requested_actions),
    )


def _zero_code_refusal(
    authority: TechLeadLaunchAuthority,
    *,
    worktree: Path,
    worktree_reader: ZeroCodeWorktreeReader,
) -> str | None:
    """The first missing fact, or ``None`` when all six are in hand."""
    if authority.flavor is not TechLeadSessionFlavor.PLANNING_INVESTIGATION:
        return (
            f"run flavor is {authority.flavor.value}; the zero-code lane is"
            f" {TechLeadSessionFlavor.PLANNING_INVESTIGATION.value} only"
        )
    if not authority.launch_base_sha:
        return (
            "the launch authority records no launch base commit, so what this"
            " run started from is unknown and cannot be reconstructed"
        )
    head = worktree_reader.get_head_sha(worktree)
    if head is None:
        return f"the commit {worktree} stands at could not be read"
    if head != authority.launch_base_sha:
        return (
            f"the checkout moved since launch: {authority.launch_base_sha}"
            f" -> {head}"
        )
    dirt = worktree_reader.list_dirty_files(worktree, CANDIDATE_DIRT_MODE)
    if dirt is None:
        return f"the tracked changes in {worktree} could not be enumerated"
    if dirt:
        return f"tracked content is modified in {worktree}: {_summarise(dirt)}"
    return None


def _summarise(paths: list[str]) -> str:
    ordered = sorted(paths)
    preview = ", ".join(ordered[:_DIRT_PREVIEW])
    remaining = len(ordered) - _DIRT_PREVIEW
    return f"{preview} (+{remaining} more)" if remaining > 0 else preview


__all__ = [
    "ZeroCodePlanningSettlement",
    "ZeroCodeWorktreeReader",
    "settle_zero_code_planning_completion",
]
