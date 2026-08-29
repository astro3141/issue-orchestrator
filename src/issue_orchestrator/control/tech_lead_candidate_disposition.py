"""What ONE Tech Lead candidate verdict does to ONE exact candidate (#345).

Before this module a landed batch review projected ``tech-lead-reviewed`` onto
every pull request number in its manifest, whatever the review found and
whatever the pull requests had done since. That projection meant "a tech-lead
session produced a valid artifact over a manifest containing this number" — and
#335 forbids reading such a thing as merge authority.

So the projection is now a consequence of two facts, and both are per candidate:

1. **the disposition the tech lead rendered for THAT candidate** — PASS,
   REWORK, or HUMAN_A (:class:`~..domain.tech_lead_candidate.
   TechLeadCandidateDisposition`), never a session-wide success flag;
2. **whether that candidate is still the candidate** — re-read from the live
   pull-request head at completion, because a verdict is authority only for the
   commit it was rendered against;
3. **whether the candidate holds the prerequisites a merge-facing PASS rests
   on** — an independent Reviewer's approval of that exact commit, and the
   staged bounded contract of the executable issue it implements. Both are
   recorded on the launch authority before the session spawned, so a PASS the
   agent renders over staged inputs that carried a gap is refused rather than
   trusted.

Every outcome leaves a receipt on the pull request, including the refusals. A
moved candidate that silently receives nothing is indistinguishable from a
review that was never run, and #345's whole point is that the disposition is
the authority-bearing fact rather than the label.

Those three facts are folded into ONE
:class:`~..domain.tech_lead_candidate.CandidateOutcome` per candidate, and the
outcome decides both halves of what happens: the receipt this module writes,
and — through :class:`~.tech_lead_candidate_policy.TechLeadCandidatePolicy` —
the labels that settle the candidate's watch-set membership. This module never
spells a watch-set label itself. It did once, and the locally-derived spelling
was wrong for every repository whose watch label is not
``code_reviewed_label``; worse, nothing at all was spelled for the outcomes
that produce no merge authority, so those candidates kept re-tripping the
threshold and re-running an identical audit forever.

Where each outcome routes, and why nothing new was built for it:

* AUTHORITY (PASS on a still-current, independently reviewed candidate) -> the
  existing merge-facing label.
* REWORK -> the actionable feedback comment lands FIRST, then the existing
  ``needs-rework`` lane picks the pull request up with its existing cycle
  budget and escalation. #295 forbids a bare label with no candidate-bound
  feedback, so the ordering is the contract, not a nicety. The watch label and
  the review-approval marker come off with it, exactly as the post-publish
  rework path does, so a candidate sent back for work does not immediately
  re-trip the batch threshold it just left.
* HUMAN (HUMAN_A) -> the existing tech-lead escalation surface (needs-human
  label plus an explanatory comment), unchanged from what an
  ``escalate_to_human`` proposal already reaches. This verdict means only that
  the already-defined boundary was reached; it invents no new human authority.
* UNSETTLED (a PASS the orchestrator refused for want of a staged
  prerequisite) -> the refusal receipt naming which one was missing, and
  nothing merge-facing.
* DEFERRED (the candidate moved, could not be read, or was never bound) -> the
  refusal receipt if a verdict was rendered, and deliberately no label at all:
  this is the one outcome that leaves the candidate in the watch set, because
  it is the one the run could not audit.

HUMAN and UNSETTLED leave ``tech-lead-failed`` behind. That is the same
sentence the failure projection already writes — "this run produced no
tech-lead authority for this pull request" — and it is what stops a stopped or
refused candidate from re-entering the very batch that stopped or refused it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_candidate import (
    CandidateOutcome,
    CandidatePassPrerequisite,
    CandidateStanding,
    TechLeadCandidate,
    TechLeadCandidateDisposition,
    TechLeadCandidateVerdict,
)
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .needs_human_block import NeedsHumanCause
from .tech_lead_candidate_policy import (
    CandidateWatchExit,
    TechLeadCandidatePolicy,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.tech_lead_artifacts import TechLeadDecision
    from ..domain.tech_lead_session import TechLeadLaunchAuthority
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from .label_manager import LabelManager
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

#: Reads the CURRENT head commit of one pull request, or ``None`` when it
#: cannot be observed. ``None`` is "unknown", never "unchanged".
CandidateHeadReader = Callable[[int], str | None]


def repository_candidate_heads(
    repository_host: "RepositoryHost | None",
) -> CandidateHeadReader:
    """The live-head reader over the repository host.

    One targeted read per audited candidate at completion, not a scan: a batch
    is threshold-sized, and the alternative — trusting the head the manifest
    recorded before the session ran — is precisely the staleness this leaf
    exists to remove.

    A transport failure answers ``None`` rather than raising, because "we could
    not look" and "it moved" have the same consequence here (no merge-facing
    authority) and neither should take down completion planning.
    """

    def current_head(pr_number: int) -> str | None:
        if repository_host is None:
            return None
        try:
            pr = repository_host.get_pr(pr_number)
        except Exception as exc:  # pragma: no cover - transport specific
            logger.warning(
                "[tech_lead] Could not re-read the head of PR #%d before"
                " applying a tech-lead disposition: %s",
                pr_number,
                exc,
            )
            return None
        return pr.head_sha if pr is not None else None

    return current_head


def candidate_standing(
    candidate: TechLeadCandidate, heads: CandidateHeadReader
) -> tuple[CandidateStanding, str]:
    """Whether ``candidate`` is still the candidate, and what was observed."""
    if not candidate.is_bound:
        return CandidateStanding.UNBOUND, ""
    observed = heads(candidate.pr_number)
    if not observed:
        return CandidateStanding.UNREADABLE, ""
    if not candidate.covers(observed):
        return CandidateStanding.MOVED, observed
    return CandidateStanding.CURRENT, observed


@dataclass(frozen=True, slots=True)
class TechLeadCandidateEffects:
    """The effects one candidate's disposition produces, and its receipt."""

    candidate: TechLeadCandidate
    standing: CandidateStanding
    disposition: TechLeadCandidateDisposition | None
    outcome: CandidateOutcome
    actions: tuple[Action, ...]

    @property
    def projected_reviewed_label(self) -> bool:
        """Whether this candidate received merge-facing tech-lead authority.

        Read off the OUTCOME, which is the one value the planner and the
        watch-set owner both keyed their answers on. Re-deriving it from the
        disposition would answer a different question — what the agent asked
        for rather than what the orchestrator concluded.
        """
        return self.outcome is CandidateOutcome.AUTHORITY

    @property
    def leaves_watch_set(self) -> bool:
        """Whether this run settled the candidate's watch-set membership."""
        return self.outcome.settles_membership


def plan_candidate_dispositions(
    config: "Config",
    authority: "TechLeadLaunchAuthority",
    decision: "TechLeadDecision",
    expected: "ExpectedState",
    *,
    labels: "LabelManager",
    heads: CandidateHeadReader,
    run_identity: str,
) -> list[Action]:
    """Plan every audited candidate's effects, independently of its siblings.

    Independence is the point of the loop: PASS(A) and REWORK(B) in one batch
    must reach A and B separately, so neither candidate's answer can be read
    off the session's overall outcome.
    """
    actions: list[Action] = []
    for effects in candidate_effects(
        config,
        authority,
        decision,
        expected,
        labels=labels,
        heads=heads,
        run_identity=run_identity,
    ):
        actions.extend(effects.actions)
    return actions


def candidate_effects(
    config: "Config",
    authority: "TechLeadLaunchAuthority",
    decision: "TechLeadDecision",
    expected: "ExpectedState",
    *,
    labels: "LabelManager",
    heads: CandidateHeadReader,
    run_identity: str,
) -> list[TechLeadCandidateEffects]:
    """One :class:`TechLeadCandidateEffects` per candidate this run audited."""
    policy = TechLeadCandidatePolicy.from_config(config)
    planned: list[TechLeadCandidateEffects] = []
    for candidate in authority.manifest_candidates:
        verdict = decision.verdict_for(candidate.pr_number)
        standing, observed = candidate_standing(candidate, heads)
        planned.append(
            _effects_for(
                candidate,
                verdict,
                standing,
                observed,
                expected,
                policy=policy,
                labels=labels,
                run_identity=run_identity,
                unmet=authority.unmet_pass_prerequisites(candidate),
            )
        )
    return planned


def _effects_for(
    candidate: TechLeadCandidate,
    verdict: TechLeadCandidateVerdict | None,
    standing: CandidateStanding,
    observed_head: str,
    expected: "ExpectedState",
    *,
    policy: TechLeadCandidatePolicy,
    labels: "LabelManager",
    run_identity: str,
    unmet: tuple[CandidatePassPrerequisite, ...],
) -> TechLeadCandidateEffects:
    """The effects for ONE candidate, in the order they must be applied.

    Two questions, answered in order and by different owners. What this run
    CONCLUDED about the candidate is
    :meth:`~..domain.tech_lead_candidate.CandidateOutcome.resolve`'s; what that
    conclusion does to the candidate's watch-set membership is the policy's.
    This function owns only the receipt and the lane label — the two things
    that are about THIS pull request rather than about the set it belongs to.
    """
    outcome = CandidateOutcome.resolve(
        disposition=verdict.disposition if verdict is not None else None,
        standing=standing,
        unmet_prerequisites=unmet,
    )
    _log_outcome(candidate, verdict, standing, outcome, unmet)
    # Resolved ONCE and handed to both halves: the receipt describes exactly
    # the labels the same rule is about to apply, so it can never announce a
    # projection that did not happen or stay silent about one that did.
    exit_rule = policy.settle(outcome)
    receipt = _receipt_for(
        outcome,
        candidate,
        verdict,
        standing,
        observed_head,
        expected,
        run_identity=run_identity,
        readmission=exit_rule.readmission,
        unmet=unmet,
    )
    return TechLeadCandidateEffects(
        candidate,
        standing,
        verdict.disposition if verdict is not None else None,
        outcome,
        # The receipt lands FIRST on every path (#295): a projection with
        # nothing actionable behind it is forbidden for rework and pointless
        # for the rest, and a candidate that silently receives nothing is
        # indistinguishable from a review that never ran.
        receipt
        + _watch_set_actions(candidate, exit_rule, outcome, expected)
        + _lane_actions(candidate, outcome, expected, labels=labels),
    )


def _log_outcome(
    candidate: TechLeadCandidate,
    verdict: TechLeadCandidateVerdict | None,
    standing: CandidateStanding,
    outcome: CandidateOutcome,
    unmet: tuple[CandidatePassPrerequisite, ...],
) -> None:
    """Say why a candidate reached a non-merge-facing conclusion."""
    if verdict is None:
        logger.info(
            "[tech_lead] No candidate verdict for PR #%d @ %s; projecting no"
            " tech-lead disposition onto it",
            candidate.pr_number,
            candidate.short_sha,
        )
        return
    if outcome is CandidateOutcome.DEFERRED:
        logger.warning(
            "[tech_lead] Refusing the %s disposition for PR #%d @ %s: %s",
            verdict.disposition.value,
            candidate.pr_number,
            candidate.short_sha,
            standing.value,
        )
    elif outcome is CandidateOutcome.UNSETTLED:
        # The prerequisites the merge contract assumes, checked where the agent
        # cannot reach them (#345). The prompt tells the tech lead not to pass a
        # candidate whose staged inputs carry a gap; this is what makes that
        # hold when it does anyway. REWORK and HUMAN_A are unaffected — neither
        # claims the candidate is mergeable.
        logger.warning(
            "[tech_lead] Refusing PASS for PR #%d @ %s: %s not established at"
            " launch",
            candidate.pr_number,
            candidate.short_sha,
            ", ".join(prerequisite.value for prerequisite in unmet),
        )


def _receipt_for(
    outcome: CandidateOutcome,
    candidate: TechLeadCandidate,
    verdict: TechLeadCandidateVerdict | None,
    standing: CandidateStanding,
    observed_head: str,
    expected: "ExpectedState",
    *,
    run_identity: str,
    readmission: str,
    unmet: tuple[CandidatePassPrerequisite, ...],
) -> tuple[Action, ...]:
    """The one comment this outcome publishes on the pull request, if any.

    ``readmission`` is the watch-set owner's own sentence about the labels this
    outcome applies and how the pull request gets back into the batch set. It
    is appended to every receipt that has one, because a candidate that has
    just been taken out of batch review — permanently, where a terminal label
    was applied — learning that from the pull request is the same standard the
    dispositions themselves are held to here.

    The empty tuple is reachable only through a candidate this run rendered no
    verdict for. That is a decision-contract violation for every candidate the
    run could bind to (``_candidate_coverage_violation``), so in a landed batch
    it survives only for a candidate whose head was never observed — and there
    is no disposition to report the refusal of.
    """
    if verdict is None:
        return ()
    if outcome is CandidateOutcome.DEFERRED:
        comment = _stale_receipt(
            candidate, verdict, standing, observed_head, run_identity
        )
        reason_detail = standing.value
    elif outcome is CandidateOutcome.UNSETTLED:
        comment = _unproven_receipt(candidate, verdict, run_identity, unmet)
        reason_detail = ", ".join(
            f"unmet {prerequisite.value}" for prerequisite in unmet
        )
    else:
        return (
            AddCommentAction(
                number=candidate.pr_number,
                is_pr=True,
                comment=_with_readmission(
                    _SETTLED_RECEIPTS[outcome](candidate, verdict, run_identity),
                    readmission,
                ),
                reason=(
                    f"tech_lead candidate {verdict.disposition.value} receipt"
                ),
                expected=expected,
            ),
        )
    return (
        AddCommentAction(
            number=candidate.pr_number,
            is_pr=True,
            comment=_with_readmission(comment, readmission),
            reason=(
                f"tech_lead candidate {candidate.pr_number}@"
                f"{candidate.short_sha} {verdict.disposition.value} refused:"
                f" {reason_detail}"
            ),
            expected=expected,
        ),
    )


def _with_readmission(comment: str, readmission: str) -> str:
    """Append the watch-set owner's re-admission sentence, when there is one."""
    if not readmission:
        return comment
    return f"{comment}\n\n**Batch review status.** {readmission}"


def _watch_set_actions(
    candidate: TechLeadCandidate,
    exit_rule: CandidateWatchExit,
    outcome: CandidateOutcome,
    expected: "ExpectedState",
) -> tuple[Action, ...]:
    """Turn the watch-set owner's answer into the actions that apply it.

    Removals precede additions so a mid-apply crash cannot leave a candidate
    both terminalized and still selected.
    """
    reason = (
        f"Tech Lead {outcome.value} on candidate {candidate.short_sha}"
    )
    return tuple(
        RemoveLabelAction(
            issue_number=candidate.pr_number,
            label=label,
            reason=f"{reason}; it no longer awaits a tech-lead answer",
            expected=expected,
        )
        for label in exit_rule.remove
    ) + tuple(
        AddLabelAction(
            issue_number=candidate.pr_number,
            label=label,
            reason=reason,
            expected=expected,
        )
        for label in exit_rule.add
    )


def _lane_actions(
    candidate: TechLeadCandidate,
    outcome: CandidateOutcome,
    expected: "ExpectedState",
    *,
    labels: "LabelManager",
) -> tuple[Action, ...]:
    """The lane a settled candidate enters, which is not a watch-set fact.

    ``needs-rework`` and ``needs-human`` say where the pull request goes NEXT;
    the watch-set labels say only that this batch is done with it. Keeping them
    apart is why the policy never learns about lanes and this module never
    learns about the threshold.
    """
    if outcome is CandidateOutcome.REWORK:
        return (
            AddLabelAction(
                issue_number=candidate.pr_number,
                label=labels.needs_rework,
                reason=f"Tech Lead REWORK on candidate {candidate.short_sha}",
                expected=expected,
            ),
        )
    if outcome is CandidateOutcome.HUMAN:
        return (
            AddLabelAction(
                issue_number=candidate.pr_number,
                label=labels.needs_human,
                reason=(
                    "Tech Lead HUMAN_A on candidate"
                    f" {candidate.short_sha}: a new Spec/TD/policy decision is"
                    " required"
                ),
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                expected=expected,
            ),
        )
    return ()


def _findings_line(verdict: TechLeadCandidateVerdict) -> str:
    if not verdict.finding_ids:
        return ""
    return f"\n- Findings: {', '.join(verdict.finding_ids)}"


def _identity_lines(candidate: TechLeadCandidate, run_identity: str) -> str:
    """The receipt's own binding: exactly which commit, decided by which run."""
    return (
        f"- Candidate: `{candidate.head_sha or 'unobserved'}`\n"
        f"- Tech Lead run: `{run_identity}`"
    )


def _pass_receipt(
    candidate: TechLeadCandidate, verdict: TechLeadCandidateVerdict, run_identity: str
) -> str:
    return (
        "## ✅ Tech Lead contract review — PASS\n\n"
        f"{_identity_lines(candidate, run_identity)}\n\n"
        f"{verdict.rationale}"
        f"{_findings_line(verdict)}\n\n"
        "This disposition is authority for the candidate named above and for no"
        " other commit."
    )


def _rework_feedback(
    candidate: TechLeadCandidate, verdict: TechLeadCandidateVerdict, run_identity: str
) -> str:
    return (
        "## 🔁 Tech Lead contract review — REWORK\n\n"
        f"{_identity_lines(candidate, run_identity)}\n\n"
        "A bounded implementation or process defect was found inside"
        " already-settled Spec/TD/policy. Address the following, then this pull"
        " request re-enters the ordinary review lane:\n\n"
        f"{verdict.rationale}"
        f"{_findings_line(verdict)}"
    )


def _human_escalation(
    candidate: TechLeadCandidate, verdict: TechLeadCandidateVerdict, run_identity: str
) -> str:
    return (
        "## ⚠️ Tech Lead contract review — human decision required\n\n"
        f"{_identity_lines(candidate, run_identity)}\n\n"
        "This candidate cannot be settled by implementation work: it needs a new"
        " Spec/TD/policy or authority decision. It is blocked pending that"
        " decision and has received neither merge nor rework authority.\n\n"
        f"{verdict.rationale}"
        f"{_findings_line(verdict)}"
    )


#: The receipt each SETTLED outcome publishes. A map rather than a branch, for
#: the reason ``CandidateOutcome`` is an enum in the first place: a new settled
#: outcome must state what it tells the pull request, and raises here until it
#: does. The two refusal receipts are not here — they need the standing and the
#: head that was observed instead.
_SETTLED_RECEIPTS: dict[
    CandidateOutcome,
    Callable[[TechLeadCandidate, TechLeadCandidateVerdict, str], str],
] = {
    CandidateOutcome.AUTHORITY: lambda candidate, verdict, run: _pass_receipt(
        candidate, verdict, run
    ),
    CandidateOutcome.REWORK: lambda candidate, verdict, run: _rework_feedback(
        candidate, verdict, run
    ),
    CandidateOutcome.HUMAN: lambda candidate, verdict, run: _human_escalation(
        candidate, verdict, run
    ),
}


def _unproven_receipt(
    candidate: TechLeadCandidate,
    verdict: TechLeadCandidateVerdict,
    run_identity: str,
    unmet: tuple[CandidatePassPrerequisite, ...],
) -> str:
    """Which staged prerequisite the refused PASS was missing, in its own words.

    Every unmet member is listed rather than only the first: a candidate that
    is neither independently reviewed nor contract-resolved needs two things
    fixed, and a receipt naming one of them sends the reader back for a second
    round.
    """
    missing = "\n".join(
        f"- **{prerequisite.value}** — {prerequisite.description}."
        for prerequisite in unmet
    )
    return (
        "## ⛔ Tech Lead PASS refused — a merge prerequisite is not"
        " established\n\n"
        f"{_identity_lines(candidate, run_identity)}\n\n"
        "The Tech Lead review passed the candidate above, but the orchestrator"
        " could not show that this exact commit holds everything a merge-facing"
        " PASS rests on, so no merge-facing Tech Lead authority has been"
        f" applied.\n\n{missing}\n\n"
        f"{verdict.rationale}"
        f"{_findings_line(verdict)}"
    )


def _stale_receipt(
    candidate: TechLeadCandidate,
    verdict: TechLeadCandidateVerdict,
    standing: CandidateStanding,
    observed_head: str,
    run_identity: str,
) -> str:
    reason = {
        CandidateStanding.MOVED: (
            "the head of this pull request is now"
            f" `{observed_head or 'unknown'}`, so the review is about work this"
            " pull request no longer proposes"
        ),
        CandidateStanding.UNREADABLE: (
            "the current head of this pull request could not be read, and an"
            " unknown head is not an unchanged one"
        ),
        CandidateStanding.UNBOUND: (
            "no head commit was observed for this pull request when the review"
            " was prepared, so nothing can be shown to have been audited"
        ),
    }[standing]
    return (
        "## ⛔ Tech Lead disposition refused — candidate is no longer current\n\n"
        f"{_identity_lines(candidate, run_identity)}\n"
        f"- Refused disposition: `{verdict.disposition.value}`\n\n"
        f"The Tech Lead review reached `{verdict.disposition.value}` for the"
        f" candidate above, but {reason}. No tech-lead review, rework or"
        " escalation authority has been applied to the current head; the pull"
        " request stays eligible for a fresh review of what it now proposes."
    )


__all__ = [
    "CandidateHeadReader",
    "TechLeadCandidateEffects",
    "candidate_effects",
    "candidate_standing",
    "plan_candidate_dispositions",
    "repository_candidate_heads",
]
