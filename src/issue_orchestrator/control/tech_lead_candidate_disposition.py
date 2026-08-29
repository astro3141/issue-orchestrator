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
3. **whether an independent Reviewer approved that exact commit** — recorded on
   the launch authority before the session spawned, so a PASS the agent renders
   over staged evidence that carried a gap is refused rather than trusted.

Every outcome leaves a receipt on the pull request, including the refusals. A
moved candidate that silently receives nothing is indistinguishable from a
review that was never run, and #345's whole point is that the disposition is
the authority-bearing fact rather than the label.

Where each disposition routes, and why nothing new was built for it:

* PASS -> the existing merge-facing label, and only for a still-current
  candidate.
* REWORK -> the actionable feedback comment lands FIRST, then the existing
  ``needs-rework`` lane picks the pull request up with its existing cycle
  budget and escalation. #295 forbids a bare label with no candidate-bound
  feedback, so the ordering is the contract, not a nicety. The tech-lead watch
  label comes off with it, exactly as the post-publish rework path does, so a
  candidate sent back for work does not immediately re-trip the batch
  threshold it just left.
* HUMAN_A -> the existing tech-lead escalation surface (needs-human label plus
  an explanatory comment), unchanged from what an ``escalate_to_human``
  proposal already reaches. This verdict means only that the already-defined
  boundary was reached; it invents no new human authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_candidate import (
    CandidateStanding,
    TechLeadCandidate,
    TechLeadCandidateDisposition,
    TechLeadCandidateVerdict,
)
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .needs_human_block import NeedsHumanCause

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
    actions: tuple[Action, ...]

    @property
    def projected_reviewed_label(self) -> bool:
        """Whether this candidate received merge-facing tech-lead authority.

        Read off the ACTIONS rather than re-derived from the inputs: the
        planner already weighed standing and the review prerequisite, and a
        second derivation here could disagree with what was actually planned.
        """
        return any(
            isinstance(action, AddLabelAction)
            and self.disposition is TechLeadCandidateDisposition.PASS
            for action in self.actions
        )


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
    planned: list[TechLeadCandidateEffects] = []
    for candidate in authority.manifest_candidates:
        verdict = decision.verdict_for(candidate.pr_number)
        standing, observed = candidate_standing(candidate, heads)
        planned.append(
            _effects_for(
                config,
                candidate,
                verdict,
                standing,
                observed,
                expected,
                labels=labels,
                run_identity=run_identity,
                reviewed=authority.review_established(candidate),
            )
        )
    return planned


def _effects_for(
    config: "Config",
    candidate: TechLeadCandidate,
    verdict: TechLeadCandidateVerdict | None,
    standing: CandidateStanding,
    observed_head: str,
    expected: "ExpectedState",
    *,
    labels: "LabelManager",
    run_identity: str,
    reviewed: bool,
) -> TechLeadCandidateEffects:
    """The effects for ONE candidate, in the order they must be applied."""
    if verdict is None:
        logger.info(
            "[tech_lead] No candidate verdict for PR #%d @ %s; projecting no"
            " tech-lead disposition onto it",
            candidate.pr_number,
            candidate.short_sha,
        )
        return TechLeadCandidateEffects(candidate, standing, None, ())
    if not standing.permits_authority:
        logger.warning(
            "[tech_lead] Refusing the %s disposition for PR #%d @ %s: %s",
            verdict.disposition.value,
            candidate.pr_number,
            candidate.short_sha,
            standing.value,
        )
        return _refused(
            candidate,
            standing,
            verdict,
            expected,
            comment=_stale_receipt(
                candidate, verdict, standing, observed_head, run_identity
            ),
            detail=standing.value,
        )
    if verdict.disposition is TechLeadCandidateDisposition.PASS and not reviewed:
        # The prerequisite the merge contract assumes, checked where the agent
        # cannot reach it (#345). The prompt tells the tech lead not to pass a
        # candidate whose staged evidence carries a gap; this is what makes
        # that hold when it does anyway. REWORK and HUMAN_A are unaffected —
        # neither claims the candidate is mergeable.
        logger.warning(
            "[tech_lead] Refusing PASS for PR #%d @ %s: no independent reviewer"
            " approval of that exact commit was established at launch",
            candidate.pr_number,
            candidate.short_sha,
        )
        return _refused(
            candidate,
            standing,
            verdict,
            expected,
            comment=_unreviewed_receipt(candidate, verdict, run_identity),
            detail="unreviewed candidate",
        )
    if verdict.disposition is TechLeadCandidateDisposition.PASS:
        return TechLeadCandidateEffects(
            candidate,
            standing,
            verdict.disposition,
            (
                AddCommentAction(
                    number=candidate.pr_number,
                    is_pr=True,
                    comment=_pass_receipt(candidate, verdict, run_identity),
                    reason="tech_lead candidate PASS receipt",
                    expected=expected,
                ),
                AddLabelAction(
                    issue_number=candidate.pr_number,
                    label=config.tech_lead_reviewed_label or "tech-lead-reviewed",
                    reason=(
                        "Tech Lead PASS on the exact candidate"
                        f" {candidate.short_sha}"
                    ),
                    expected=expected,
                ),
            ),
        )
    if verdict.disposition is TechLeadCandidateDisposition.REWORK:
        return TechLeadCandidateEffects(
            candidate,
            standing,
            verdict.disposition,
            (
                # The feedback lands BEFORE the projection (#295): a
                # ``needs-rework`` label with nothing actionable behind it is
                # forbidden, and the rework agent reads its instructions here.
                AddCommentAction(
                    number=candidate.pr_number,
                    is_pr=True,
                    comment=_rework_feedback(candidate, verdict, run_identity),
                    reason="tech_lead candidate REWORK feedback",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=candidate.pr_number,
                    label=labels.code_reviewed,
                    reason=(
                        "Tech Lead REWORK on candidate"
                        f" {candidate.short_sha}; the review that approved it"
                        " no longer stands"
                    ),
                    expected=expected,
                ),
                AddLabelAction(
                    issue_number=candidate.pr_number,
                    label=labels.needs_rework,
                    reason=(
                        f"Tech Lead REWORK on candidate {candidate.short_sha}"
                    ),
                    expected=expected,
                ),
            ),
        )
    return TechLeadCandidateEffects(
        candidate,
        standing,
        verdict.disposition,
        (
            AddCommentAction(
                number=candidate.pr_number,
                is_pr=True,
                comment=_human_escalation(candidate, verdict, run_identity),
                reason="tech_lead candidate HUMAN_A escalation",
                expected=expected,
            ),
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
        ),
    )


def _refused(
    candidate: TechLeadCandidate,
    standing: CandidateStanding,
    verdict: TechLeadCandidateVerdict,
    expected: "ExpectedState",
    *,
    comment: str,
    detail: str,
) -> TechLeadCandidateEffects:
    """A disposition that reaches the pull request as a receipt and nothing else.

    Every refusal takes this shape on purpose: a candidate that silently
    receives nothing is indistinguishable from a review that never ran, so the
    refusal is published even though no label follows it.
    """
    return TechLeadCandidateEffects(
        candidate,
        standing,
        verdict.disposition,
        (
            AddCommentAction(
                number=candidate.pr_number,
                is_pr=True,
                comment=comment,
                reason=(
                    f"tech_lead candidate {candidate.pr_number}@"
                    f"{candidate.short_sha} {verdict.disposition.value} refused:"
                    f" {detail}"
                ),
                expected=expected,
            ),
        ),
    )


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


def _unreviewed_receipt(
    candidate: TechLeadCandidate, verdict: TechLeadCandidateVerdict, run_identity: str
) -> str:
    return (
        "## ⛔ Tech Lead PASS refused — the candidate is not independently"
        " reviewed\n\n"
        f"{_identity_lines(candidate, run_identity)}\n\n"
        "The Tech Lead review passed the candidate above, but no independent"
        " Reviewer approval of that exact commit was established when this"
        " review's inputs were staged. A review label on the pull request is"
        " evidence about the pull request, not about this commit, so no"
        " merge-facing Tech Lead authority has been applied.\n\n"
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
