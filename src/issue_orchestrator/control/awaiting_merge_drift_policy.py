"""Classify what an issue's associated PR set says about awaiting-merge state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PRInfo


# What an issue's PR set resolves to under the "latest terminal PR decides"
# precedence. ``missing`` means the issue has no associated PR at all.
PrSetOutcome = Literal["open", "merged", "closed_unmerged", "missing"]


@dataclass(frozen=True)
class PrSetClassification:
    """What an issue's PR set means for awaiting-merge handling.

    ``pr`` is the PR the outcome keys on, or ``None`` for ``open`` (no single
    PR decides — the issue is simply still awaiting a merge) and ``missing``.
    """

    outcome: PrSetOutcome
    pr: PRInfo | None = None

    @property
    def drifting(self) -> bool:
        """True when the set indicates ``blocked:pr-closed`` label drift.

        A merged PR does NOT drift: its issue is reconciled through the
        awaiting-merge terminal path (which owns the close-on-merge and
        stale-``pr-pending`` decisions), not flagged as blocked (#113).
        """
        return self.outcome in ("closed_unmerged", "missing")


def classify_pr_set(prs: list[PRInfo]) -> PrSetClassification:
    """Own the PR-set precedence policy for one issue.

    Single owner of the rule, shared by the awaiting-merge label-drift scan and
    startup's ``pr-pending`` history rehydration, so the two cannot disagree
    about which PR an issue's state keys on.

    Policy:
    - Any open PR means the issue is still legitimately awaiting a merge.
    - No PRs at all means there is nothing backing the ``pr-pending`` claim.
    - Otherwise the latest terminal PR decides: merged routes to the
      awaiting-merge terminal path, closed-unmerged to ``blocked:pr-closed``.
    """
    if any(_normalized_state(pr.state) == "open" for pr in prs):
        return PrSetClassification(outcome="open")
    if not prs:
        return PrSetClassification(outcome="missing")
    latest = max(prs, key=lambda item: item.number)
    if latest.is_closed_unmerged:
        return PrSetClassification(outcome="closed_unmerged", pr=latest)
    return PrSetClassification(outcome="merged", pr=latest)


def _normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()
