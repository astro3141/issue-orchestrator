"""Whether a Tech Lead batch review can produce merge authority at all (#345).

A Tech Lead ``pass`` is merge-facing, and #345 made it conditional on a fact the
agent cannot supply: an INDEPENDENT reviewer approval of the exact commit the
tech lead audited. The orchestrator files that fact only where it concludes a
review with a candidate it observed — the review exchange
(``review_exchange_terminals.complete_with_reviewer_decision``, via
``CandidateExecutionIdentityRecorder.record_verdict``).

The classic review lane does not. A standalone review session ends in
``reviewer-done approved``, whose only effect is the ``code-reviewed`` label —
evidence about the pull request, not about a commit. So in a deployment whose
reviews take that lane, every batch ``pass`` is refused for want of an
exact-candidate approval, and ``tech-lead-reviewed`` becomes unreachable.

That is a real configuration, so this module makes it a startable-but-degraded
state the operator is TOLD about rather than one they discover from refusal
comments. The reachability rule has one owner here so doctor and the
documentation cannot describe it differently; the runtime enforcement stays
where it belongs, in the completion-time refusal, because a precondition that
is only checked at startup is one a mid-run configuration change escapes.

Two ways a deployment lands here, and both are reported:

* ``review.exchange.mode: via-draft-pr`` — no exchange runs at all, so nothing
  ever files a candidate verdict;
* an exchange mode that WOULD file one, but a coder agent with no reviewer
  configured, whose reviews fall back to the classic lane per agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

#: Exchange modes that conclude in the orchestrator's own candidate-bound
#: verdict. ``via-draft-pr`` is deliberately absent: it is the spelling for
#: "no exchange", and ``auto`` resolves to one of the two present here.
VERDICT_FILING_EXCHANGE_MODES = frozenset({"auto", "via-mcp", "via-local-loop"})


@dataclass(frozen=True, slots=True)
class TechLeadMergeAuthorityReadiness:
    """Whether this configuration can reach a Tech Lead ``pass``."""

    #: True when a batch review can fire at all — otherwise there is nothing to
    #: report, whatever the review lane looks like.
    active: bool = False
    #: One human-readable reason per way the prerequisite cannot be produced.
    problems: tuple[str, ...] = ()

    @property
    def reachable(self) -> bool:
        return self.active and not self.problems


def tech_lead_merge_authority_readiness(
    config: "Config",
) -> TechLeadMergeAuthorityReadiness:
    """Can this configuration produce an exact-candidate reviewer approval?

    Inactive — and therefore silent — unless tech_lead batch review is both
    enabled and able to fire, because a repository that never runs a batch
    review never asks the question this answers.
    """
    if not config.tech_lead_enabled or config.tech_lead_review_threshold <= 0:
        return TechLeadMergeAuthorityReadiness()

    problems: list[str] = []
    if config.review_exchange_mode not in VERDICT_FILING_EXCHANGE_MODES:
        problems.append(
            f"review.exchange.mode is {config.review_exchange_mode!r}, which"
            " runs no review exchange, so no reviewer verdict is ever bound to"
            " a candidate commit"
        )
    unpaired = sorted(
        label
        for label in config.agents
        if config.get_reviewer_for_agent(label) is None
    )
    if unpaired:
        problems.append(
            "no reviewer is configured for "
            + ", ".join(unpaired)
            + " (set review.code_review_agent, or a per-agent reviewer), so"
            " their pull requests are reviewed outside the exchange"
        )
    return TechLeadMergeAuthorityReadiness(active=True, problems=tuple(problems))


__all__ = [
    "VERDICT_FILING_EXCHANGE_MODES",
    "TechLeadMergeAuthorityReadiness",
    "tech_lead_merge_authority_readiness",
]
