"""Execution-time owner for tech_lead ``reset_retry`` proposals (ADR-0031 §2, #6764).

Authority made ``reset_retry`` plannable; this module makes executing it
safe. A proposal is a stale-checkable fact recorded against a board
snapshot, not a command: by the time the tech_lead session completes, the
board may have moved. The executor therefore re-validates the proposal's
preconditions against CURRENT state immediately before invoking the reset
owner:

1. the target issue can still be read and is still open;
2. no runtime the reset boundary would terminate is active for the issue
   — a visible issue/rework session, the persistent coder/reviewer pair, a
   supervised review-exchange job, or a pending publish retry. The reset
   force-terminates ALL of these, which an operator clicking "Reset & Retry"
   consents to, but an agent proposal written before that work started must
   never kill unobserved live work (the freshness check reads the same owner
   set the teardown mutates — see ``has_active_reset_retry_runtime``);
3. the issue still carries at least one blocking-class label
   (:meth:`LabelManager.get_blocking` — the same classification the retry
   entry points clear via ``labels_to_remove_for_retry``). If nothing
   blocking remains, the diagnosed failure has already been recovered and
   the proposal is stale.

On a stale precondition the proposal DOWNGRADES (ADR-0031 §2): the
surfaced-proposal event (``TECH_LEAD_ACTION_PROPOSED`` with
``mode="stale_downgrade"``) is emitted and no mutations are posted. On
success a ``TECH_LEAD_ACTION_EXECUTED`` event records the boundary effects.
Reset-owner failures fail the action loudly — never a silent success.

A ``ResetRetryIssueAction`` is one member of the COMPLETION GATE, whose
apply-time boundary and terminal-status verdict live next door in
:mod:`.completion_effect_gate` — general machinery with a second member since
#337 r3, and no longer this module's to own. What stays here is the
tech_lead-specific consequence:
:func:`build_required_act_level_failure_actions` routes a failed mandated
reset to a durable needs-human label + comment so the FAILED terminal
:func:`~.completion_effect_gate.effective_terminal_status` assigns is not
merely in-memory. A failed mandated reset can therefore never be recorded as
a clean success.

The reset itself is NOT reimplemented here: ``run_reset`` is the injected
production boundary — the same ``reset_and_retry_issue`` pipeline the
dashboard's ``/api/reset-retry`` endpoint uses (runtime termination, PR
superseding, branch deletion, label/history/timeline clearing,
pending-label relaunch marking, and queue re-insertion). Production wiring
lives in ``entrypoints/tech_lead_reset_retry_wiring.py``.

This module also owns the surfaced-proposal event payload
(:func:`publish_proposal_surfaced`) so the shadow/pattern/rejected surface
handler and the stale-downgrade path cannot drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .actions import (
    Action,
    ActionResult,
    AddCommentAction,
    AddLabelAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
)
from .completion_effect_gate import CompletionGateFailure
from .needs_human_block import NeedsHumanCause

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

# Surfaced-proposal mode for an execute-authority proposal whose recorded
# preconditions no longer held at execution time (ADR-0031 §2).
STALE_DOWNGRADE_MODE = "stale_downgrade"

# Cap applied to rationale previews in surfaced events, matching
# tech_lead_decision_actions._BODY_PREVIEW_CHARS for planned surfaces.
_RATIONALE_PREVIEW_CHARS = 500


def publish_proposal_surfaced(
    events: EventSink,
    *,
    issue_number: int,
    action_id: str,
    proposal_type: str,
    target_number: int,
    target_is_pr: bool,
    title: str,
    body_preview: str,
    finding_ids: Sequence[str],
    mode: str,
    stale_reason: str | None = None,
) -> None:
    """Publish the surfaced-proposal trace event (single payload owner).

    Shadow/pattern/stale-downgrade surfaces emit ``TECH_LEAD_ACTION_PROPOSED``;
    rejected decision artifacts (``mode == "rejected"``) emit
    ``TECH_LEAD_DECISION_REJECTED``. No GitHub calls — surfacing is the whole
    effect.
    """
    payload: dict[str, Any] = {
        "issue_number": issue_number,
        "action_id": action_id,
        "proposal_type": proposal_type,
        "target_number": target_number,
        "target_is_pr": target_is_pr,
        "title": title,
        "body_preview": body_preview,
        "finding_ids": list(finding_ids),
        "mode": mode,
    }
    if stale_reason is not None:
        payload["stale_reason"] = stale_reason
    event_name = (
        EventName.TECH_LEAD_DECISION_REJECTED
        if mode == "rejected"
        else EventName.TECH_LEAD_ACTION_PROPOSED
    )
    events.publish(make_trace_event(event_name, payload))
    logger.info(
        issue_log(issue_number, "Tech Lead proposal surfaced: mode=%s type=%s action_id=%s"),
        mode, proposal_type, action_id,
    )


def apply_surface_tech_lead_proposal(
    action: SurfaceTechLeadProposalAction, events: EventSink
) -> ActionResult:
    """Apply a :class:`SurfaceTechLeadProposalAction` (event-only, ADR-0031)."""
    publish_proposal_surfaced(
        events,
        issue_number=action.issue_number,
        action_id=action.action_id,
        proposal_type=action.proposal_type,
        target_number=action.target_number,
        target_is_pr=action.target_is_pr,
        title=action.title,
        body_preview=action.body_preview,
        finding_ids=action.finding_ids,
        mode=action.mode,
    )
    return ActionResult.ok(
        action,
        issue_number=action.issue_number,
        action_id=action.action_id,
        proposal_type=action.proposal_type,
        mode=action.mode,
    )


@dataclass(frozen=True)
class ResetRetryRunOutcome:
    """Typed result of one reset-owner invocation (the injected boundary)."""

    success: bool
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


# The production boundary: (issue_number, current_labels) -> outcome. The
# labels are the fresh read the executor already made for re-validation,
# passed through so the reset owner does not re-fetch them (GitHub API
# discipline).
RunResetFn = Callable[[int, Sequence[str]], ResetRetryRunOutcome]


def reset_retry_stale_reason(
    *,
    issue: "Issue | None",
    active_runtime: bool,
    label_manager: "LabelManager",
) -> str | None:
    """Why the proposal's preconditions no longer hold, or None when valid.

    Pure policy — see the module docstring for the precondition rationale.
    ``active_runtime`` is the aggregate signal from every runtime owner the
    reset boundary would terminate (visible session, persistent review-exchange
    pair/job, or pending publish retry), not just a visible session.
    """
    if issue is None:
        return "target issue could not be read from the repository host"
    if issue.state != "open":
        return f"target issue #{issue.number} is {issue.state}, not open"
    if active_runtime:
        return (
            f"issue #{issue.number} has active runtime (session, review-exchange"
            " pair/job, or publish retry); resetting would terminate live work"
            " the proposal did not observe"
        )
    if not label_manager.get_blocking(issue.labels):
        return (
            f"issue #{issue.number} no longer carries a blocking-class"
            " label; the diagnosed failure appears already recovered"
        )
    return None


@dataclass
class TechLeadResetRetryExecutor:
    """Applies :class:`ResetRetryIssueAction` with execution-time re-validation.

    All collaborators are injected: reads come from the composition root's
    closures over live orchestrator state, and ``run_reset`` is the reused
    dashboard reset pipeline. The executor owns only the
    validate/downgrade/execute/surface policy.
    """

    events: EventSink
    label_manager: "LabelManager"
    read_issue: Callable[[int], "Issue | None"]
    has_active_issue_runtime: Callable[[int], bool]
    run_reset: RunResetFn

    def apply(self, action: ResetRetryIssueAction) -> ActionResult:
        issue = self.read_issue(action.issue_number)
        stale = reset_retry_stale_reason(
            issue=issue,
            active_runtime=self.has_active_issue_runtime(action.issue_number),
            label_manager=self.label_manager,
        )
        if stale is not None:
            return self._downgrade(action, stale)
        assert issue is not None  # stale check rejects None
        outcome = self.run_reset(action.issue_number, list(issue.labels))
        if not outcome.success:
            logger.error(
                issue_log(
                    action.issue_number,
                    "Tech Lead reset_retry %s FAILED in the reset owner: %s",
                ),
                action.proposal_id,
                outcome.error,
            )
            return ActionResult.fail(
                action,
                f"reset owner failed for issue #{action.issue_number}"
                f" (proposal {action.proposal_id}): {outcome.error}",
                issue_number=action.issue_number,
                proposal_id=action.proposal_id,
            )
        self.events.publish(make_trace_event(EventName.TECH_LEAD_ACTION_EXECUTED, {
            "issue_number": action.anchor_issue_number,
            "action_id": action.proposal_id,
            "proposal_type": "reset_retry",
            "target_number": action.issue_number,
            "finding_ids": list(action.finding_ids),
            "boundary": dict(outcome.details),
        }))
        logger.info(
            issue_log(
                action.issue_number,
                "Tech Lead reset_retry %s executed via the reset owner",
            ),
            action.proposal_id,
        )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )

    def _downgrade(self, action: ResetRetryIssueAction, stale: str) -> ActionResult:
        """Stale precondition: surface as would-have-done, post no mutations."""
        logger.warning(
            issue_log(
                action.issue_number,
                "Tech Lead reset_retry %s downgraded to surfaced proposal: %s",
            ),
            action.proposal_id,
            stale,
        )
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type="reset_retry",
            target_number=action.issue_number,
            target_is_pr=False,
            title="",
            body_preview=action.rationale[:_RATIONALE_PREVIEW_CHARS],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=stale,
        )
        return ActionResult.skip(
            action,
            f"stale precondition: {stale}",
            mode=STALE_DOWNGRADE_MODE,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )


def preserve_reset_retry_eligibility(
    applied: Sequence[ActionResult],
    *,
    make_retryable: Callable[[int], object],
) -> list[int]:
    """Keep reset issues retryable after the completion's own history append.

    The completion pipeline appends the completing session's history entry
    AFTER its actions are applied. For a failure investigation, that entry
    is keyed on the very issue a successful ``reset_retry`` just made
    retryable — and both the planner's eligibility loop and
    ``QueueCache.evaluate_issue`` treat a history entry as "already ran, do
    not relaunch". Without this pass, the reset's relaunch would be
    silently re-blocked by the reset's own tech_lead session. Callers invoke
    this after the append with :meth:`RetryHistoryState.make_retryable` —
    the owner of those gates — and get back the issue numbers re-cleared.
    """
    cleared: list[int] = []
    for result in applied:
        candidate = result.action
        matched = isinstance(candidate, ResetRetryIssueAction) and result.success
        if not matched:
            continue
        make_retryable(candidate.issue_number)
        cleared.append(candidate.issue_number)
    return cleared


def build_required_act_level_failure_actions(
    *,
    issue_number: int,
    needs_human_label: str,
    reset_failures: Sequence[CompletionGateFailure],
    session_id: str,
    runtime_minutes: float,
) -> list[Action]:
    """Durable, crash-safe operator surface for a failed mandated act-level action.

    A failed mandated reset terminalizes the completion as FAILED
    (:func:`~.completion_effect_gate.effective_terminal_status`), but that
    terminal record is in-memory only — a crash between it and the next tick
    would lose the signal. This
    routes the failure to GitHub through the SAME label/comment action owners the
    rest of completion uses (no parallel mechanism): the needs-human blocking
    label plus an explanatory comment, mirroring the invalid-completion-record
    surface ("the orchestrator could not safely apply the agent's requested
    outcome"). Returns an EMPTY list when nothing was handed to it, so the caller
    applies nothing on the success path and the genuine-failure path (whose
    surface the completion handler already planned).

    It takes the reset failures THEMSELVES rather than the whole completion-gate
    outcome, because the gate holds a second member whose failure is not a reset
    (#337 r3 F1): telling an operator that "Reset & Retry did not complete" when
    no reset was ever mandated would be a false report. Selecting the kind is the
    caller's routing decision — see
    :meth:`~.completion_effect_gate.CompletionGateOutcome.failures_of` — and what
    arrives here is only ever this owner's own failures. The other member is
    durably surfaced by the state its failure declines to leave — an open,
    still-claimed issue — so it needs no writes here.
    """
    if not reset_failures:
        return []
    summary = "; ".join(failure.detail for failure in reset_failures)
    comment = (
        "**Reset & Retry Did Not Complete**\n\n"
        "The tech_lead decision mandated a scratch reset for this issue, but the "
        "reset owner failed at apply time. The orchestrator recorded the session "
        "as FAILED instead of accepting the agent's completed intent, so the "
        "issue is not silently left as a partial reset.\n\n"
        f"- Failure: {summary}\n"
        f"- Session: `{session_id}`\n"
        f"- Runtime: {runtime_minutes:.1f} minutes\n\n"
        f"This issue has been marked as `{needs_human_label}` because the "
        "orchestrator could not safely apply the mandated reset.\n"
        "Remove the label after correcting or re-running the reset."
    )
    return [
        AddLabelAction(
            issue_number=issue_number,
            label=needs_human_label,
            reason="mandated reset_retry did not commit; routing to needs-human",
            needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
        ),
        AddCommentAction(
            number=issue_number,
            comment=comment,
            reason="notify operator that the mandated reset failed at apply time",
        ),
    ]
