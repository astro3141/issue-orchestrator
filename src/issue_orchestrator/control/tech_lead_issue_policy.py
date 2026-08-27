"""Single policy owner for orchestrator-created tech_lead issues (ADR-0031).

Two paths create GitHub issues on tech_lead's behalf and MUST share one policy:

* the planner's batch-review tracking issue (``_plan_tech_lead_issue_creation``),
* decision-driven follow-up issues (``create_issue`` proposals in a tech_lead
  decision artifact, executed by ``tech_lead_decision_actions``).

This module owns how the ``tech_lead:`` config section (explicit labels,
inherited labels, priority, milestone strategy) turns into concrete issue
labels/milestones/titles, and which agent-proposed labels are acceptable at
all. Agent-proposed labels are untrusted input: anything matching a
workflow/protected family (orchestrator lifecycle labels, ``needs-*``,
``*-reviewed``, ``*-failed``, ``publish-*``, ``blocked*``, ``agent:*``,
``tech_lead:*``) is rejected so a decision artifact can never corrupt label
truth (ADR-0013). Concrete orchestrator-owned names are derived from
config/:class:`LabelManager`, not re-hardcoded here.

GitHub label names are case-insensitive, so every comparison in this module
casefolds — an agent must not bypass protection (or defeat inheritance or
dedup) by case-flipping a name.

This module also owns whether a created issue may project SCHEDULING at all
(#332). :class:`TechLeadIssueAdmission` is the typed decision the caller
constructs; a planning proposal pending Human approval is gated AND unscheduled,
and :func:`is_scheduler_projection_label` is what "unscheduled" means here —
no ``agent:*``/configured-agent label reaches such an issue from any source.

The module is pure policy: the milestone strategy becomes a typed
:class:`TechLeadMilestoneIntent` at planning time (:func:`tech_lead_issue_milestone_intent`),
and the explicit NAME -> number resolution runs ONCE at the create-issue
execution boundary (:func:`resolve_tech_lead_milestone_number`, called by the
action applier with ``RepositoryHost.list_milestones`` passed in) — never at
planning or completion time (#6769 finding 4).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from ..domain.tech_lead_naming import TECH_LEAD_DISPLAY_NAME
from ..domain.tech_lead_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TECH_LEAD_AREA_LABEL_PREFIX,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadCreationOrigin,
)
from .actions import CreateTechLeadIssueAction, TechLeadMilestoneIntent
from .label_manager import LabelManager

if TYPE_CHECKING:
    from ..domain.models import TechLeadFacts
    from ..infra.config import Config


logger = logging.getLogger(__name__)


# Workflow label families that no agent-proposed label may match. These are
# families, not concrete names: concrete orchestrator-owned names (including
# any configured prefix) come from LabelManager/config at call time.
# ``proposed-tech-lead`` (#6778) and ``tech-lead-observation`` (#6781) are doubly
# covered: they are registered LabelManager labels (workflow-reserved) AND
# matched here, so both can only ever be orchestrator-attached — an agent
# proposing either is a contract violation regardless of which owner checks
# first.
_PROTECTED_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"needs-", re.IGNORECASE),
    re.compile(r".*-reviewed\Z", re.IGNORECASE),
    re.compile(r".*-failed\Z", re.IGNORECASE),
    re.compile(r"publish-", re.IGNORECASE),
    re.compile(r"blocked", re.IGNORECASE),
    re.compile(r"agent:", re.IGNORECASE),
    re.compile(r"tech_lead:", re.IGNORECASE),
    re.compile(r"proposed-tech-lead\Z", re.IGNORECASE),
    re.compile(r"tech-lead-observation\Z", re.IGNORECASE),
)


def is_protected_tech_lead_label(
    label: str, *, config: "Config", labels: LabelManager
) -> bool:
    """True when an agent-proposed label would touch workflow label truth.

    Case-insensitive throughout: GitHub treats ``WIP`` and ``wip`` as the
    same label.
    """
    if labels.is_workflow_reserved(label):
        return True
    folded = label.casefold()
    configured = {
        value.casefold()
        for value in (
            config.tech_lead_review_agent,
            config.tech_lead_watch_label,
            config.tech_lead_reviewed_label,
            config.tech_lead_failed_label,
            config.filtering.label,
            config.label_in_progress,
        )
        if value
    }
    if folded in configured:
        return True
    return any(pattern.match(label) for pattern in _PROTECTED_LABEL_PATTERNS)


def protected_tech_lead_label_violations(
    proposed: Iterable[str], *, config: "Config", labels: LabelManager
) -> list[str]:
    """Return the agent-proposed labels that violate the protected set."""
    return [
        label
        for label in proposed
        if is_protected_tech_lead_label(label, config=config, labels=labels)
    ]


# The scheduler/admission label FAMILY, matched case-insensitively like every
# other comparison here. Membership is also derived from config at call time so
# a deployment whose worker labels do not use this prefix is still covered.
_AGENT_LABEL_PATTERN = re.compile(r"agent:", re.IGNORECASE)


def is_scheduler_projection_label(label: str, *, config: "Config") -> bool:
    """True when *label* projects Actor scheduling/admission onto an issue (#332).

    Discovery fetches one query per configured agent label
    (``FactGatherer.fetch_issues``), so an agent label IS the admission surface:
    an issue that carries one is a work item some lane will pick up, and an
    issue that carries none is structurally unreachable by every Actor lane.
    That is a narrower question than :func:`is_protected_tech_lead_label`, which
    also covers scope/lifecycle names the orchestrator legitimately attaches.
    """
    if _AGENT_LABEL_PATTERN.match(label):
        return True
    folded = label.casefold()
    configured = {name.casefold() for name in config.agents}
    if config.tech_lead_review_agent:
        configured.add(config.tech_lead_review_agent.casefold())
    return folded in configured


def pending_proposal_protected_label_violations(
    proposed: Iterable[str], *, config: "Config", labels: LabelManager
) -> list[str]:
    """Protected labels a PENDING-PROPOSAL creation would still project (#332).

    The two predicates in this module answer different questions and both must
    hold on every path: :func:`is_scheduler_projection_label` governs ADMISSION
    ("does this admit an Actor?"), :func:`is_protected_tech_lead_label` governs
    LABEL TRUTH ("does this touch orchestrator-owned state?"). A planning
    proposal is exempt from the label-truth rule only where the creation
    boundary provably withholds the name: :func:`_pending_proposal_labels`
    filters out exactly the scheduler-projection labels, so those cannot reach
    the issue and rejecting the whole decision over one would destroy the single
    bounded proposal the run exists to produce (#261, #295 §4-5).

    Every OTHER protected label is projected verbatim by that boundary, so it
    stays fatal here exactly as it is for every other role — the owner's rule is
    enforced once, on both paths, with no cross-path drift.
    """
    return protected_tech_lead_label_violations(
        [
            label
            for label in proposed
            if not is_scheduler_projection_label(label, config=config)
        ],
        config=config,
        labels=labels,
    )


@dataclass(frozen=True, slots=True)
class TechLeadIssueAdmission:
    """How a tech-lead-created issue is admitted to the execution plane (#332).

    Issue creation used to take a ``destination_agent`` plus a ``gate`` boolean,
    which let one combination exist that must not: gated (pending approval) AND
    routed to a worker (admitted). #323 is the live evidence — a planning-created
    proposal carrying ``proposed-tech-lead`` and ``agent:backend`` at once
    projects "Human approval pending" and "Actor scheduling" simultaneously.

    So admission is a CONSTRUCTED decision, never a pair of fields a caller
    assembles by hand:

    * :meth:`scheduled` — execute-authority create: routed to the orchestrator's
      worker agent now, no gate.
    * :meth:`gated` — propose-authority create for a role that HOLDS scheduling
      authority: gated, but already routed, so removing the gate alone lands
      schedulable work (#6779 R5).
    * :meth:`for_planning_proposal` — a planning proposal (#23 Phase 1.5,
      #295 §4-5): gated AND unscheduled. It projects no scheduler/admission
      label at all, so pending-proposal state and Actor admission can never
      coexist. Scheduling authority belongs to the later Human/Control act,
      not to Planning.
    """

    #: The worker agent label the issue is routed to; "" when unscheduled.
    destination_agent: str
    #: Whether the orchestrator-attached approval gate is projected.
    gate: bool
    #: Whether this creation is a proposal still pending explicit Human approval.
    pending_human_approval: bool

    def __post_init__(self) -> None:
        # Self-validating type: the invariant this class exists for is that
        # "pending" and "admitted" are mutually exclusive states, so the
        # combination cannot be constructed at all.
        if self.pending_human_approval and self.destination_agent:
            raise ValueError(
                "a proposal pending Human approval must not be routed to"
                f" {self.destination_agent!r}; a pending proposal is unscheduled"
                " and the scheduler label is the later Human/Control act (#332)"
            )
        if self.pending_human_approval and not self.gate:
            raise ValueError(
                "a proposal pending Human approval must carry the approval gate;"
                " without it there is nothing for the Human act to remove (#332)"
            )
        if not self.pending_human_approval and not self.destination_agent:
            raise ValueError(
                "a scheduled tech_lead issue creation requires the"
                " orchestrator-owned destination worker agent (#6779 R5)"
            )

    @classmethod
    def scheduled(cls, destination_agent: str) -> "TechLeadIssueAdmission":
        """Execute-authority creation: routed now, ungated."""
        return cls(
            destination_agent=destination_agent,
            gate=False,
            pending_human_approval=False,
        )

    @classmethod
    def gated(cls, destination_agent: str) -> "TechLeadIssueAdmission":
        """Propose-authority creation: gated, and already routed (#6779 R5)."""
        return cls(
            destination_agent=destination_agent,
            gate=True,
            pending_human_approval=False,
        )

    @classmethod
    def for_planning_proposal(cls) -> "TechLeadIssueAdmission":
        """Planning proposal: gated, unscheduled, no scheduler projection."""
        return cls(destination_agent="", gate=True, pending_human_approval=True)


@dataclass(frozen=True, slots=True)
class TechLeadIssueLabelProjection:
    """What a tech-lead issue creation projects onto GitHub, and what it withheld.

    ``withheld`` is never dropped silently: the create boundary renders it into
    the operator-facing body, so a planning model that asked for a scheduler
    label can be seen asking for it without the label ever reaching the issue.
    """

    labels: tuple[str, ...]
    withheld: tuple[str, ...] = ()


def apply_tech_lead_priority_prefix(config: "Config", title: str) -> str:
    """Apply the configured ``tech_lead.priority`` tier as a ``[P?-000]`` prefix."""
    priority = (config.tech_lead.priority or "").strip()
    if not re.fullmatch(r"P\d", priority):
        return title
    if re.search(r"^\[P\d-\d+\]", title):
        return title
    return f"[{priority}-000] {title}"


def resolve_tech_lead_milestone_number(
    intent: TechLeadMilestoneIntent,
    list_milestones: Callable[[str], Sequence[Mapping[str, Any]]],
) -> int | None:
    """Resolve a milestone intent to a concrete number at the execution boundary.

    ``list_milestones`` is ``RepositoryHost.list_milestones`` passed in by the
    action applier so this module stays port-free; it is consulted only for
    an explicit name (one call, made only when an issue is actually being
    created — GitHub API discipline). Raises ValueError when the configured
    name matches no repository milestone — a misconfigured strategy must fail
    the creation loudly, never silently create unmilestoned issues.
    """
    if intent.explicit_name is None:
        return intent.inherited_number
    for milestone in list_milestones("all"):
        if str(milestone.get("title", "")).strip() == intent.explicit_name:
            return int(milestone["number"])
    raise ValueError(
        f"tech_lead.milestone_strategy.explicit={intent.explicit_name!r} does not"
        " match any repository milestone; fix the configured name or remove"
        " the strategy"
    )


def tech_lead_issue_milestone_intent(
    config: "Config",
    source_milestones: Sequence[tuple[int, str]],
) -> TechLeadMilestoneIntent:
    """Compute the milestone INTENT for a tech-lead-created issue per config.

    Pure planning policy: the explicit strategy yields a name for the
    applier to resolve at creation time (#6769 finding 4); the inherit
    strategy yields a number already known from the source issues; otherwise
    no milestone.
    """
    strategy = config.tech_lead.milestone_strategy
    name = (strategy.explicit or "").strip()
    if name:
        return TechLeadMilestoneIntent(explicit_name=name)
    if strategy.inherit_from_issues and source_milestones:
        ordered = sorted(source_milestones, key=lambda m: m[0])
        chosen = ordered[0] if strategy.inherit_from_issues == "earliest" else ordered[-1]
        return TechLeadMilestoneIntent(inherited_number=chosen[0])
    return TechLeadMilestoneIntent()


def case_file_issue_labels(config: "Config", *, area: str | None) -> tuple[str, ...]:
    """Labels for a pattern case-file issue (#6781).

    Mirrors :func:`~.tech_lead_proposals.proposal_issue_labels`: the tech_lead
    agent label keeps the case file inside the fact gatherer's ONE anchor
    scan, the filtering label keeps it inside the active scope, and the
    orchestrator-attached observation label blocks pickup and marks it as an
    evidence ledger. The optional ``area`` becomes an ``area:*`` tag so
    evidence clusters are queryable across signatures (#6781 amendment).
    The observation label is exempt from the agent-label allowlist here and
    ONLY here — an agent proposing it directly is a contract violation.
    """
    return tuple(
        value
        for value in (
            config.tech_lead_review_agent,
            config.filtering.label,
            TECH_LEAD_OBSERVATION_LABEL,
            f"{TECH_LEAD_AREA_LABEL_PREFIX}{area}" if area else None,
        )
        if value
    )


def batch_review_issue_labels(
    config: "Config", *, source_labels: Collection[str]
) -> tuple[str, ...]:
    """Labels for the planner's batch-review tracking issue."""
    base: list[str] = []
    if config.tech_lead_review_agent:
        base.append(config.tech_lead_review_agent)
    if config.filtering.label:
        base.append(config.filtering.label)
    return _with_configured_labels(config, base, source_labels=source_labels)


def plan_batch_review_issue(
    config: "Config", facts: "TechLeadFacts"
) -> CreateTechLeadIssueAction | None:
    """Build the batch-review tracking issue when its threshold is met.

    This policy lives beside the labels, milestone intent, and title prefix it
    stamps.  The planner offers observed facts; this owner decides the complete
    shape of the resulting tech-lead anchor.
    """
    if facts.threshold <= 0:
        # Batch trigger disabled; facts may exist for the health review alone.
        return None
    if facts.pr_count < facts.threshold:
        logger.debug(
            "Planner: tech_lead threshold not met (%d/%d)",
            facts.pr_count,
            facts.threshold,
        )
        return None
    if facts.existing_tech_lead_issue:
        logger.debug(
            "Planner: tech_lead issue #%d already exists",
            facts.existing_tech_lead_issue,
        )
        return None

    pr_list = "\n".join(f"- PR #{number}: {title}" for number, title in facts.prs)
    body = f"""## Tech Lead Batch Review Triggered

{facts.pr_count} PRs have passed code review and are ready for tech_lead review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{facts.watch_label}` to `{config.tech_lead_reviewed_label}` after review.
"""
    title = apply_tech_lead_priority_prefix(
        config,
        f"{TECH_LEAD_DISPLAY_NAME} Batch Review: {facts.pr_count} PRs pending",
    )
    labels = batch_review_issue_labels(config, source_labels=facts.source_labels)
    # Milestone travels as intent; the applier resolves explicit names at the
    # create-issue execution boundary (#6769 finding 4).
    milestone = tech_lead_issue_milestone_intent(config, facts.source_milestones)
    logger.info(
        "Planner: creating tech_lead issue for %d PRs (labels=%s, milestone=%s)",
        facts.pr_count,
        labels,
        milestone,
    )
    return CreateTechLeadIssueAction(
        title=title,
        body=body,
        labels=labels,
        pr_count=facts.pr_count,
        milestone=milestone,
        reason=f"threshold met: {facts.pr_count} >= {facts.threshold}",
        # This IS the anchor: no prior issue to reconcile against (#6957 F6).
        origin=TechLeadCreationOrigin.authors_anchor(),
    )


def health_review_issue_labels(config: "Config") -> tuple[str, ...]:
    """Labels for the periodic health-review anchor issue (ADR-0031 §4).

    Same configured policy batch anchors get — agent label, filtering scope
    label, ``tech_lead.explicit_labels`` — plus the health marker label, which
    is crash-safe truth: the launcher derives the HEALTH_REVIEW flavor from
    it and the fact gatherer deduplicates open anchors by it. Health anchors
    have no source PRs, so ``tech_lead.inherit_labels`` has nothing to inherit.
    """
    base = [
        value
        for value in (
            config.tech_lead_review_agent,
            config.filtering.label,
            HEALTH_REVIEW_MARKER_LABEL,
        )
        if value
    ]
    return _with_configured_labels(config, base, source_labels=())


def tech_lead_follow_up_agent_label(config: "Config") -> str:
    """The orchestrator-owned worker agent a ``create_issue`` proposal routes to.

    A tech_lead decision may propose a NEW issue, but agent-proposed ``agent:*``
    labels are rejected as protected input (they could hijack routing), and
    ``explicit_labels`` defaults empty — so the created issue would carry no
    agent label and normal discovery (which queries per configured worker
    agent) would never fetch it. The orchestrator therefore assigns the
    destination itself.

    The destination is the TYPED, VALIDATED ``review.tech_lead_follow_up_agent``
    setting (#6779 R9), NOT the first key of ``config.agents``: that mapping
    also holds reviewer, tech_lead, and goal-pilot agents, so dict order could
    route new work to an agent that cannot perform it. The
    :class:`ReviewWorkflowValidator` guarantees the configured value names a
    real agent; this fails loudly when it is unset rather than guessing.
    """
    destination = config.tech_lead_follow_up_agent
    if not destination:
        raise ValueError(
            "a tech_lead create_issue proposal needs a destination worker agent;"
            " set review.tech_lead_follow_up_agent to a worker label in `agents`"
            " (#6779 R9)"
        )
    return destination


def decision_issue_labels(
    config: "Config",
    *,
    anchor_labels: Collection[str],
    agent_labels: Iterable[str],
    labels: LabelManager,
    admission: TechLeadIssueAdmission,
    area: str | None = None,
) -> TechLeadIssueLabelProjection:
    """Labels for a decision-driven follow-up issue.

    Config policy first (filtering scope label, explicit labels, labels
    inherited from the tech_lead session's anchor issue), then the agent's
    proposed labels, then whatever ``admission`` authorizes the orchestrator to
    attach itself.

    ``admission`` is the single typed decision (:class:`TechLeadIssueAdmission`)
    that says whether this creation may project scheduling at all:

    * SCHEDULED / GATED — the historical shape. Protected agent labels are a bug
      at this point (the decision must have been rejected as a contract
      violation upstream in ``tech_lead_completion``), so fail loudly instead of
      silently filtering. The orchestrator-owned ``destination_agent`` and the
      ``proposed-tech-lead`` gate are appended AFTER the protection check —
      both are orchestrator-attached, not agent-proposed, so they are exempt
      from the agent-label allowlist here and ONLY here.
    * PENDING HUMAN APPROVAL — a planning proposal (#332). No destination agent
      is attached, and every scheduler-projection label is WITHHELD from the
      composed set no matter which source contributed it (model-proposed,
      ``tech_lead.explicit_labels``, or inherited from the anchor). Those, and
      ONLY those, are sanitized rather than fatal — a planning run's whole
      purpose is to leave exactly one bounded proposal (#261, #295 §4-5), and
      the boundary was always going to withhold the label. Every other protected
      agent label is still a contract violation upstream
      (:func:`pending_proposal_protected_label_violations`), because this path
      projects it verbatim just like the scheduled one.
    """
    if admission.pending_human_approval:
        return _pending_proposal_labels(
            config,
            anchor_labels=anchor_labels,
            agent_labels=agent_labels,
            labels=labels,
            area=area,
        )
    violations = protected_tech_lead_label_violations(
        agent_labels, config=config, labels=labels
    )
    if violations:
        raise ValueError(
            "protected labels must be rejected at decision validation, got: "
            + ", ".join(violations)
        )
    if admission.destination_agent not in config.agents:
        raise ValueError(
            "decision_issue_labels destination_agent must be a configured worker"
            f" agent, got {admission.destination_agent!r} (agents:"
            f" {sorted(config.agents)})"
        )
    composed = _composed_decision_labels(config, anchor_labels=anchor_labels)
    area_labels = _area_labels(area)
    gate_labels = (labels.proposed_tech_lead,) if admission.gate else ()
    return TechLeadIssueLabelProjection(
        labels=_deduped(
            (
                *composed,
                *agent_labels,
                *area_labels,
                admission.destination_agent,
                *gate_labels,
            )
        )
    )


def _pending_proposal_labels(
    config: "Config",
    *,
    anchor_labels: Collection[str],
    agent_labels: Iterable[str],
    labels: LabelManager,
    area: str | None,
) -> TechLeadIssueLabelProjection:
    """Project a planning proposal that is pending explicit Human approval (#332).

    The invariant: pending-proposal state and Actor-admission projection never
    coexist. Enforcement is structural rather than a list of forbidden names —
    the whole composed set is filtered through
    :func:`is_scheduler_projection_label`, so a scheduler label reaches the
    issue from NO source: not the model's requested labels, not
    ``tech_lead.explicit_labels``, not ``tech_lead.inherit_labels`` copying one
    off the tech-lead anchor.

    Informational labels survive untouched; the gate is appended last and is the
    only orchestrator-attached name a pending proposal carries. Any OTHER
    protected label is projected verbatim from here, which is why it stays a
    fatal contract violation upstream rather than being sanitized in this
    function (:func:`pending_proposal_protected_label_violations`).
    """
    composed = _composed_decision_labels(config, anchor_labels=anchor_labels)
    candidates = _deduped((*composed, *agent_labels, *_area_labels(area)))
    projected = tuple(
        label
        for label in candidates
        if not is_scheduler_projection_label(label, config=config)
    )
    withheld = tuple(label for label in candidates if label not in projected)
    if withheld:
        logger.warning(
            "[tech_lead] Withheld scheduler label(s) %s from a planning proposal"
            " pending Human approval; a pending proposal is unscheduled (#332)",
            ", ".join(withheld),
        )
    result = TechLeadIssueLabelProjection(
        labels=_deduped((*projected, labels.proposed_tech_lead)),
        withheld=withheld,
    )
    # Fail loudly rather than file an ungated or schedulable "pending" proposal:
    # every later gate in the system reads these labels as the pending state.
    leaked = [
        label
        for label in result.labels
        if is_scheduler_projection_label(label, config=config)
    ]
    if leaked:
        raise ValueError(
            f"planning proposal would project scheduler label(s) {leaked};"
            " a proposal pending Human approval must be unscheduled (#332)"
        )
    return result


def _composed_decision_labels(
    config: "Config", *, anchor_labels: Collection[str]
) -> tuple[str, ...]:
    """Config policy (scope, explicit, inherited), shared by every creation."""
    base: list[str] = []
    if config.filtering.label:
        base.append(config.filtering.label)
    return _with_configured_labels(config, base, source_labels=anchor_labels)


def _area_labels(area: str | None) -> tuple[str, ...]:
    return (f"{TECH_LEAD_AREA_LABEL_PREFIX}{area}",) if area else ()


def _with_configured_labels(
    config: "Config", base: list[str], *, source_labels: Collection[str]
) -> tuple[str, ...]:
    composed = list(base)
    composed.extend(config.tech_lead.explicit_labels)
    source_folded = {label.casefold() for label in source_labels}
    composed.extend(
        label
        for label in config.tech_lead.inherit_labels
        if label.casefold() in source_folded
    )
    return _deduped(composed)


def _deduped(labels: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving, case-insensitive dedup (first spelling wins)."""
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        folded = label.casefold()
        if label and folded not in seen:
            seen.add(folded)
            result.append(label)
    return tuple(result)
