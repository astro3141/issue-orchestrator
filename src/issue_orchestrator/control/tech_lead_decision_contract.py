"""What makes a tech_lead decision VALID for the run that produced it.

The completion owner (``tech_lead_completion``) decides what a finished
tech_lead session causes; this module decides whether the session's decision
artifact is admissible in the first place, judged against the immutable
orchestrator-owned :class:`TechLeadLaunchAuthority` and never against the
agent-writable worktree copies. Keeping the two apart means the contract can
grow axes (#133 added the role capability one) without the effects planner
growing with it.

The axes, in the order they are judged:

1. **Role capability** — may a session of THIS flavor propose this KIND of
   action at all? Owned by ``domain.tech_lead_capabilities``; no authority
   mode, target, or prompt can recover a forbidden kind (#133).
2. **Target scope** — may it act on THIS issue/PR? Two scopes: comment/routing
   proposals may address the general launch scope (manifest PRs included for a
   batch review), while act-level reset/kill proposals are held to the stricter
   issue-only scope so a manifest PR number never reaches the issue reset owner
   as an ``issue_number`` (#6764 re-review F1).
3. **Candidate scope** — may it render a merge-facing disposition on THIS
   exact candidate, and did it render one for EVERY candidate it was launched
   auditing? A per-candidate verdict binds to a pull request AND a commit, and
   both must match what the run was launched auditing; silence about an
   audited candidate is refused the same way, because that candidate would
   otherwise stay in the batch that re-audits it identically (#345).
4. **Flavor duties** — a failure investigation must publish its diagnosis to
   the originating issue (#6761 F2).
5. **Label truth** — ``create_issue`` proposals may not carry protected
   workflow labels (#6761 F4).

Every one of them returns a human-readable detail rather than raising: the
caller records it as the completion's contract violation, which rejects the
whole decision — siblings of the offending action included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS
from ..domain.tech_lead_capabilities import TECH_LEAD_ACTION_CAPABILITIES
from ..domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor
from .label_manager import LabelManager
from .tech_lead_issue_policy import protected_tech_lead_label_violations

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import TechLeadDecision
    from ..infra.config import Config

# Comment/routing proposals whose target_number must fall inside the general
# launch scope (which, for a batch review, includes the audited manifest PRs).
# create_issue / flag_pattern carry no target and are scope-free. Act-level
# proposals (reset_retry / kill_hung_session) are validated separately against
# the STRICTER issue-only scope — see ``allowed_act_level_targets`` (#6764 rr F1).
_TARGET_SCOPED_ACTION_TYPES = frozenset(("post_comment", "escalate_to_human"))


def _launch_scope_description(
    authority: TechLeadLaunchAuthority, allowed: frozenset[int]
) -> str:
    """Human-readable launch scope for out-of-scope violation messages."""
    if authority.flavor.is_issue_focused:
        return f"the originating issue #{authority.focus_issue_number}"
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        return (
            f"the health-review anchor issue #{authority.anchor_issue_number}"
            " (board-wide comments/escalations belong on the anchor; act-level"
            " proposals instead use the cohort this review owns, published as"
            " problem_cohort in board-snapshot.json)"
        )
    return (
        "the audited manifest PRs and the tracking issue"
        f" ({', '.join(f'#{n}' for n in sorted(allowed))})"
    )


def _act_level_scope_description(authority: TechLeadLaunchAuthority) -> str:
    """Human-readable ISSUE-only scope for an out-of-scope act-level violation.

    The fall-through branch is the independent second guard on the target axis
    (#133): an act-level proposal from a batch review — or from a planning
    investigation (#136) — is rejected one step earlier by the role capability
    gate, and this text stands behind it so the scope rule keeps stating its own
    conclusion. Under the SHIPPED capability table that branch is therefore
    unreachable for both — no test exercises this string, and none should; it
    becomes reachable only if a future table grants one of those roles an
    act-level kind. The invariant it describes is covered directly by
    ``tests/unit/test_tech_lead_authority_store.py::
    test_allowed_act_level_targets_are_issue_only``.
    """
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return f"the originating work issue #{authority.focus_issue_number}"
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        cohort = ", ".join(f"#{n}" for n in authority.problem_issue_numbers)
        return (
            "the health review's immutable problem cohort, published as"
            " problem_cohort in board-snapshot.json"
            f" ({cohort or 'empty — a periodic review owns no act-level target'})"
        )
    return (
        "no work issue is in scope for an act-level reset/kill from this"
        " session — that intent applies only to a failure investigation's"
        " focus issue; batch manifest entries are PRs and tech_lead anchors are"
        " bookkeeping issues, so route board findings through the scope-free"
        " create_issue/flag_pattern proposals instead"
    )


def _capability_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """Forbidden action-KIND detail for this session's role, or None (#133).

    The role comes from the launch authority, so an assignment copy rewritten
    mid-session to claim a wider flavor never selects the allowlist.
    """
    return TECH_LEAD_ACTION_CAPABILITIES.violation(decision, authority.flavor)


def _target_scope_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """Out-of-scope target detail for any targeted proposal, or None.

    Two scopes (#6764 re-review F1): comment/routing proposals may target the
    general launch scope (manifest PRs included for a batch), while act-level
    reset/kill proposals are held to the STRICTER issue-only scope so a
    manifest PR number never reaches the issue reset owner as an ``issue_number``.
    """
    allowed = authority.allowed_targets()
    act_allowed = authority.allowed_act_level_targets()
    for action in decision.proposed_actions:
        if action.action_type in ACT_LEVEL_TECH_LEAD_ACTIONS:
            if action.target_number not in act_allowed:
                return (
                    f"proposed action {action.id} ({action.action_type}) targets"
                    f" #{action.target_number}, outside this session's launch"
                    f" scope for an act-level reset/kill:"
                    f" {_act_level_scope_description(authority)}"
                )
            continue
        if action.action_type not in _TARGET_SCOPED_ACTION_TYPES:
            continue
        if action.target_number not in allowed:
            return (
                f"proposed action {action.id} ({action.action_type}) targets"
                f" #{action.target_number}, outside this session's launch"
                f" scope: {_launch_scope_description(authority, allowed)}"
            )
    return None


def _diagnosis_duty_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """A failure investigation must publish its diagnosis (#6761 F2)."""
    if authority.flavor is not TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return None
    focus = authority.focus_issue_number
    if any(
        action.action_type == "post_comment" and action.target_number == focus
        for action in decision.proposed_actions
    ):
        return None
    return (
        "failure investigation decision must propose at least one"
        f" post_comment targeting the originating issue #{focus}"
        " (the diagnosis has no channel otherwise)"
    )


def _protected_label_violation(
    decision: "TechLeadDecision", *, config: "Config", labels: LabelManager
) -> str | None:
    """``create_issue`` proposals may not touch orchestrator label truth (#6761 F4).

    Checked here rather than in the domain contract so the artifact contract
    stays config-free.
    """
    for action in decision.proposed_actions:
        if action.action_type != "create_issue":
            continue
        violations = protected_tech_lead_label_violations(
            action.labels, config=config, labels=labels
        )
        if violations:
            return (
                f"proposed action {action.id} (create_issue) carries protected"
                f" workflow labels: {', '.join(violations)}; agent-proposed"
                " labels may not touch orchestrator label truth"
            )
    return None


def _candidate_verdict_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """Out-of-scope or misbound per-candidate verdict detail, or None (#345).

    The fifth axis, and the one that decides merge-facing authority. A verdict
    is admissible only when it names a candidate THIS run was launched to audit
    — the pull request AND the exact commit the orchestrator recorded for it.
    Everything else is refused for the whole decision rather than dropped,
    because a decision that renders a disposition on work it never audited is
    not a decision with one bad row in it.

    A run whose authority records no candidates (a legacy row, or a non-batch
    flavor) may carry no verdicts at all: with nothing to bind to, a verdict
    would be a claim about a commit nobody observed.
    """
    for verdict in decision.candidate_verdicts:
        recorded = authority.candidate_for(verdict.pr_number)
        if recorded is None:
            return (
                f"candidate verdict for PR #{verdict.pr_number} names a pull"
                " request this session was not launched to audit; its recorded"
                " candidates are"
                f" {[candidate.pr_number for candidate in authority.manifest_candidates]}"
            )
        if not recorded.covers(verdict.candidate.head_sha):
            return (
                f"candidate verdict for PR #{verdict.pr_number} is rendered"
                f" against {verdict.candidate.short_sha}, which is not the"
                f" candidate this session was launched to audit"
                f" ({recorded.short_sha})"
            )
    return None


def _candidate_coverage_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """A candidate this run was launched to audit and answered nothing for.

    The dual of :func:`_candidate_verdict_violation`, and the same severity.
    That one refuses a verdict about work the run never audited; this one
    refuses SILENCE about work it did. A batch is launched over an exact set
    of candidates, and the set that trips the threshold is the set a review
    has to settle — a run that answers for two of three leaves the third
    counting toward the very threshold that would re-run this identical audit
    over identical evidence, forever, with no operator-visible reason.

    Only BOUND candidates carry the duty. A pull request the orchestrator
    selected without an observable head cannot receive an admissible verdict at
    all — a verdict must name a commit, and no commit was ever observed for
    this one — so demanding one would make such a batch impossible to complete
    rather than making it answer. Those candidates stay in the watch set and
    are re-audited at whatever head can next be read, which is the same
    treatment a moved candidate gets.
    """
    answered = {verdict.pr_number for verdict in decision.candidate_verdicts}
    unanswered = [
        candidate.pr_number
        for candidate in authority.manifest_candidates
        if candidate.is_bound and candidate.pr_number not in answered
    ]
    if not unanswered:
        return None
    return (
        "the decision renders no candidate verdict for pull request(s)"
        f" {unanswered}, which this session WAS launched to audit; every"
        " audited candidate needs a pass/rework/human_a disposition, because a"
        " candidate nobody answered for stays in the batch that will re-audit"
        " it identically"
    )


def validate_decision_for_authority(
    decision: "TechLeadDecision",
    authority: TechLeadLaunchAuthority,
    *,
    config: "Config",
    labels: LabelManager,
) -> str | None:
    """Authority/policy validation beyond the structural artifact contract.

    Returns the first contract-violation detail, or None when the decision is
    admissible. The axes and their order are the module docstring's; each is
    independent, so passing one never satisfies another.
    """
    return (
        _capability_violation(decision, authority)
        or _target_scope_violation(decision, authority)
        or _candidate_verdict_violation(decision, authority)
        or _candidate_coverage_violation(decision, authority)
        or _diagnosis_duty_violation(decision, authority)
        or _protected_label_violation(decision, config=config, labels=labels)
    )
