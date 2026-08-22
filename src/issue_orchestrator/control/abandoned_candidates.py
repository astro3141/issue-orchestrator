"""How often one run may hand an abandoned candidate back (#195).

Releasing an abandoned candidate retires the only thing that was bounding
relaunch. ``session_history_issue_numbers`` is the sole member of the planner's
launch filter that can hold a ``validation_failed`` issue with no PR and no
queued review, and the scheduler's own gates do not apply: ``in-progress`` is
shed by the release, no session is active, and ``validation-failed`` is a
LIFECYCLE label, so ``is_blocking_any`` does not refuse it. Nothing else counts
fresh coding launches — ``max_validation_retries`` and the reroute counter are
both per-session, ``max_consecutive_publish_failures`` is only reached from
``COMPLETED + critical_errors``, and ``AttemptStore`` gates reviews.

So a deterministically-failing validation command would relaunch every few
ticks for the life of the process. ``completion_processor`` already names this
hazard in its own words -- "without a budget, a permanently-failing validation
forms an infinite loop" -- and this module is that budget for the relaunch
path.

The verdict travels on the snapshot rather than being recomputed by the
planner, because counting is a question about ``session_history`` and the
planner is pure. ``QueueCache`` owns both halves it needs (the history and the
configured ceiling), so it is the producer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.issue import Issue


@dataclass(frozen=True)
class AbandonedCandidate:
    """One issue whose last session left no owner, and its release verdict.

    ``releases_granted`` counts how many times THIS run has already handed the
    issue back after an abandoned completion, read off the history entries the
    release marked. ``max_releases`` is the configured ceiling
    (``retry.max_abandoned_releases``).
    """

    issue: "Issue"
    releases_granted: int
    max_releases: int

    @property
    def exhausted(self) -> bool:
        """True when this run has spent its automatic relaunches for the issue.

        The exhausting release still happens -- what changes is that it carries
        a blocking label with it, so the issue is handed back to a human rather
        than to the scheduler. Withholding the release instead would strand the
        issue silently with a stale ``in-progress`` label, which is the failure
        #195 exists to remove.
        """
        return self.releases_granted >= self.max_releases


@dataclass(frozen=True)
class AbandonedCandidates:
    """Every abandoned candidate this tick, each with its release verdict.

    The planner reads verdicts from here instead of asking "is this issue in
    the abandoned set?", so the fact (nothing owns the issue) and the policy
    (may this run relaunch it again?) cannot drift apart across call sites.
    """

    candidates: tuple[AbandonedCandidate, ...] = ()

    @property
    def issues(self) -> tuple["Issue", ...]:
        """The issues themselves, for callers that only need the fact.

        Staleness detection is one: "does this label still describe reality?"
        must be asked of an exhausted candidate too, or its stale
        ``in-progress`` label would never be shed and the escalation would
        never be planned.
        """
        return tuple(candidate.issue for candidate in self.candidates)

    def verdict(self, issue_number: int) -> AbandonedCandidate | None:
        """The verdict for one issue, or None when it is not abandoned."""
        for candidate in self.candidates:
            if candidate.issue.number == issue_number:
                return candidate
        return None
