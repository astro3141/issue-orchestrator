"""The pre-action policy phase's answer, and the tech_lead half that settles it.

Completion processing runs its policies BEFORE the completion record is
preserved and before a single requested action executes, so what those policies
return is what the generic action executor is allowed to see. The phase produces
two things, and until #328 it returned only one of them:

* a refusal, when the completion may not proceed at all;
* the settled fact of whether what proceeds still offers a code candidate.

The second is the one that was being dropped. ``settle_tech_lead_completion``
already proved that a ``planning_investigation`` run left its checkout at the
commit it was launched on and therefore offers nothing to validate (#202), the
processor logged that fact, and then returned a result that did not carry it —
so ``SessionController`` ran the ordinary quick gate over an unchanged base
commit and recorded a candidate-shaped PASS for a run with no candidate.

This module is the seam where the tech_lead owner's answer becomes a value the
rest of completion processing carries. It decides nothing itself: the launch
authority, the decision contract, the zero-code proof and the recovery policy
all belong to :mod:`.tech_lead_completion`, and everything here does is supply
that owner with the run's identity and its two worktree reads, then hand back
what the owner said in the shape the pipeline can carry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import CompletionRecord
from ..domain.session_run import SessionRunAssets
from .completion_types import CodeCandidateSettlement, ProcessingResult
from .tech_lead_completion import settle_tech_lead_completion
from .tech_lead_session_policy import is_tech_lead_session
from .tech_lead_zero_code import ZeroCodeWorktreeReader

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreActionOutcome:
    """What the pre-action policy phase decided about one completion.

    ``refusal`` is a result the caller must return VERBATIM: the completion is
    refused, and no requested action may execute. ``code_candidate`` is what the
    phase settled about the work itself, and it travels on the same value so a
    caller cannot take the refusal branch and leave the settlement behind.
    """

    refusal: ProcessingResult | None
    code_candidate: CodeCandidateSettlement

    @classmethod
    def admitted(cls, code_candidate: CodeCandidateSettlement) -> "PreActionOutcome":
        """The completion proceeds, offering what ``code_candidate`` says."""
        return cls(refusal=None, code_candidate=code_candidate)

    @classmethod
    def refused(cls, refusal: ProcessingResult) -> "PreActionOutcome":
        """The completion is refused, and nothing about its work was settled.

        The candidate fact is deliberately the ordinary one. A refusal is the
        orchestrator declining to act on a completion, not a proof about the
        checkout behind it — and a rejected, tampered, or unresolvable tech_lead
        authority must never be the thing that buys a skipped validation gate.
        """
        return cls(refusal=refusal, code_candidate=CodeCandidateSettlement.presented())


def settle_tech_lead_pre_action(
    config: "Config | None",
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
    worktree: Path,
    record: CompletionRecord,
    agent_label: str | None,
    issue_number: int,
    run_assets: SessionRunAssets,
    worktree_reader: ZeroCodeWorktreeReader,
) -> PreActionOutcome:
    """Ask the tech_lead owner what this completion may still do (ADR-0031).

    Running in the pre-action policy phase — before the completion record is
    preserved and before ANY requested action executes (#6769 finding 1) — is
    what makes the owner's answer the boundary rather than a suggestion: a
    rejection produces ZERO push/PR/comment calls and a failed processing result
    whose tagged error is classified critical, so history records FAILED for
    every flavor and the tech_lead failure labeling path fires downstream; and a
    shaped action tuple is the only one the generic executor below ever sees.

    Which completions are held to the admission contract, which policies shape
    the survivors, and in what order, all belong to
    ``settle_tech_lead_completion`` (#202 publication intent, #182/#136 recovery
    intent, for BLOCKED as well as COMPLETED — #257). This seam only supplies
    the run's identity and the two worktree reads, and hands the lane's own
    :attr:`~.tech_lead_completion.TechLeadCompletionLane.code_candidate` on so
    the downstream validation decision reads the settlement instead of guessing
    at it (#328).

    A completion no tech_lead owner governs — a coder's, a reviewer's, or any
    completion at all when no config is wired — is admitted offering the
    ordinary code candidate, which is exactly today's behaviour.
    """
    if config is None or not is_tech_lead_session(
        config.tech_lead_review_agent, agent_label
    ):
        return PreActionOutcome.admitted(CodeCandidateSettlement.presented())
    lane = settle_tech_lead_completion(
        config,
        tech_lead_authority=tech_lead_authority,
        run_dir=run_assets.run_dir,
        run_id=run_assets.run_id,
        session_name=run_assets.session_name,
        outcome=record.outcome,
        requested_actions=tuple(record.requested_actions),
        worktree=worktree,
        worktree_reader=worktree_reader,
    )
    if lane.rejection is not None:
        logger.warning(
            "Tech Lead completion rejected before any action for issue #%d: %s",
            issue_number,
            lane.rejection,
        )
        return PreActionOutcome.refused(
            ProcessingResult(
                success=False,
                message=f"Tech Lead completion rejected: {lane.rejection}",
                errors=[lane.rejection],
            )
        )
    record.requested_actions = list(lane.requested_actions)
    logger.info(
        "Tech Lead completion lane for issue #%d: outcome=%s zero_code=%s (%s)",
        issue_number,
        record.outcome.value,
        lane.zero_code,
        lane.detail,
    )
    return PreActionOutcome.admitted(lane.code_candidate)


__all__ = ["PreActionOutcome", "settle_tech_lead_pre_action"]
