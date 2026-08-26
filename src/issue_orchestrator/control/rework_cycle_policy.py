"""The one owner of the rework-cycle budget and its admission rules (#297).

Two producers now decide "may this open PR take another rework cycle, and which
cycle is it": the ordinary :class:`~.pr_scanner.PRScanner` sweep over
``needs-rework`` PRs, and the continuation handoff that admits ordinary rework
when a PR-backed candidate's control continuation exits to it
(:mod:`.continuation_rework_handoff`). A second copy of the arithmetic would be
a second budget — the two would disagree about which cycle is next the moment
either changed, and the ceiling that bounds the whole correction loop would be
enforced twice with two answers.

So the arithmetic and the ordering of the refusals live here, once, as a pure
policy over facts the callers already hold: the PR's labels, the issue's
labels, and what the engine is already doing about the issue. Nothing in this
module reads GitHub, a store, or a clock, so both callers can be tested against
it exhaustively.

What is deliberately NOT here is scope. "Is this issue mine to work on" is
asked differently by the two callers — the scanner reads a PR it found by label
and must resolve the issue behind it, while the handoff is already holding a
board issue the engine fetched — and folding those into one predicate would
make the shared owner know about repositories.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .label_manager import LabelManager


class ReworkAdmissionVerdict(Enum):
    """What the rework-cycle owner says about one candidate PR."""

    #: A cycle is available and nothing else holds the issue.
    QUEUE = "queue"
    #: The ceiling is passed. Today's escalation path fires; no cycle is spent.
    ESCALATE = "escalate"
    #: Something else already holds the issue, or a blocking label forbids it.
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class ReworkAdmission:
    """One admission decision, with everything its caller must report.

    ``rework_cycle`` is carried on every verdict, including ``SKIP``: the
    number is a fact about the PR's labels rather than a consequence of the
    decision, and a caller logging why it refused should be able to say which
    cycle it refused.
    """

    verdict: ReworkAdmissionVerdict
    issue_number: int
    rework_cycle: int
    blocking_labels: tuple[str, ...] = ()
    reason: str = ""

    @property
    def queues(self) -> bool:
        return self.verdict is ReworkAdmissionVerdict.QUEUE

    @property
    def escalates(self) -> bool:
        return self.verdict is ReworkAdmissionVerdict.ESCALATE


def next_rework_cycle(
    labels: Sequence[str], label_manager: "LabelManager"
) -> int:
    """The cycle a rework launched now would be, per the PR's own labels.

    The durable counter is the ``rework-cycle-N`` label the rework launcher
    writes when a cycle actually starts, so this is a read of a spent budget
    rather than a running total anybody has to keep. An unlabelled PR has spent
    nothing, so the next cycle is the first.
    """
    cycle = label_manager.extract_rework_cycle(labels)
    if cycle is not None:
        return cycle + 1
    return 1


class ReworkCycleBudget:
    """Whether one more rework cycle may be spent on a PR, and which one."""

    def __init__(
        self, label_manager: "LabelManager", *, max_rework_cycles: int
    ) -> None:
        self._lm = label_manager
        self._max_rework_cycles = max_rework_cycles

    @property
    def max_rework_cycles(self) -> int:
        return self._max_rework_cycles

    def next_cycle(self, pr_labels: Sequence[str]) -> int:
        """The cycle a rework launched now would be. See :func:`next_rework_cycle`."""
        return next_rework_cycle(pr_labels, self._lm)

    def already_held(
        self,
        issue_number: int,
        *,
        queued_issue_numbers: Sequence[int] | frozenset[int] | set[int],
        active_issue_numbers: Sequence[int] | frozenset[int] | set[int],
        pr_labels: Sequence[str] | None = None,
        issue_labels: Sequence[str] | None = None,
    ) -> ReworkAdmission | None:
        """Every refusal decidable from what the caller ALREADY holds, or ``None``.

        Split out so a caller that must pay a GitHub read to complete the
        picture can first ask what it can answer for free — and so it asks it
        of THIS owner rather than re-implementing "already queued" beside it.
        :meth:`admit` reaches the same refusals in the same order by delegating
        here, so the split changes when an answer is known, never what it is.

        ``None`` for a label set means "not read", which is deliberately
        distinct from "read and empty": the owner then skips that side's
        refusal rather than answering it from ignorance. Both callers hold one
        side for free — the scanner found the PR by label, the handoff is
        holding a board issue — so passing the free side here is what keeps a
        refusal from costing a read.

        The one thing partial knowledge changes is which side gets NAMED when
        both are blocked: a caller that knows only the issue's labels reports
        ``issue_blocked`` where a caller holding both would have reported
        ``blocking_label``. The verdict is ``SKIP`` either way, and naming the
        side that was actually read is the honest report.
        """
        if issue_number in queued_issue_numbers:
            return self._refuse(issue_number, 0, "already_queued")
        if issue_number in active_issue_numbers:
            return self._refuse(issue_number, 0, "active_session")
        # Unknown PR labels mean an unknown cycle. Reported as 0 for the same
        # reason the two refusals above report 0: it is "not read", not "one".
        cycle = 0 if pr_labels is None else self.next_cycle(pr_labels)
        # One refusal, asked of whichever sides the caller actually read, in
        # the order this owner has always asked them: the PR's own state first,
        # then the issue's. A side that was not read is skipped, not answered.
        for side, reason in ((pr_labels, "blocking_label"), (issue_labels, "issue_blocked")):
            if side is None:
                continue
            blocking = self._lm.get_blocking(side)
            if blocking:
                return self._refuse(issue_number, cycle, reason, blocking=blocking)
        return None

    def admit(
        self,
        *,
        issue_number: int,
        pr_labels: Sequence[str],
        issue_labels: Sequence[str],
        queued_issue_numbers: Sequence[int] | frozenset[int] | set[int],
        active_issue_numbers: Sequence[int] | frozenset[int] | set[int],
    ) -> ReworkAdmission:
        """Decide one candidate PR, in the one order the refusals are checked.

        Order is the policy: what already holds the issue outranks the PR's own
        state, a blocking label on either the PR or the issue outranks the
        budget, and the ceiling is the last word before a cycle is granted. The
        issue's labels are consulted as well as the PR's because a publish
        failure marks the issue blocked while leaving ``needs-rework`` standing
        on the PR.
        """
        held = self.already_held(
            issue_number,
            queued_issue_numbers=queued_issue_numbers,
            active_issue_numbers=active_issue_numbers,
            pr_labels=pr_labels,
            issue_labels=issue_labels,
        )
        if held is not None:
            return held
        cycle = self.next_cycle(pr_labels)
        if cycle > self._max_rework_cycles:
            return ReworkAdmission(
                verdict=ReworkAdmissionVerdict.ESCALATE,
                issue_number=issue_number,
                rework_cycle=cycle,
                reason="max_rework_exceeded",
            )
        return ReworkAdmission(
            verdict=ReworkAdmissionVerdict.QUEUE,
            issue_number=issue_number,
            rework_cycle=cycle,
            reason="queue",
        )

    @staticmethod
    def _refuse(
        issue_number: int,
        cycle: int,
        reason: str,
        *,
        blocking: Sequence[str] = (),
    ) -> ReworkAdmission:
        return ReworkAdmission(
            verdict=ReworkAdmissionVerdict.SKIP,
            issue_number=issue_number,
            rework_cycle=cycle,
            blocking_labels=tuple(blocking),
            reason=reason,
        )


__all__ = [
    "ReworkAdmission",
    "ReworkAdmissionVerdict",
    "ReworkCycleBudget",
    "next_rework_cycle",
]
