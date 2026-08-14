"""The publication-gate verdict for an issue, and what it authorizes (#45).

The ordinary order of an issue's lifecycle is fixed (#21)::

    actor -> validation (publication gate) -> review -> PR -> human merge

So a publication gate that did *not* pass cannot authorize the review step.
The gate already recorded its failure as an issue label, but nothing read it
back: ``needs-code-review`` left on a PR by an earlier candidate stayed
review-eligible, and a review launched against a candidate the gate had just
rejected.

Two halves of one verdict live here, deliberately in one module:

* :class:`PublicationAuthority` writes it. ``revoke`` on a failed gate,
  ``grant`` when a candidate clears every publication precondition. Both are
  needed: a revocation with no matching grant would outlive the candidate that
  earned it and hold a later, genuinely validated candidate out of review
  forever.
* :func:`publication_gate_failed` reads it, and is what
  :mod:`.review_validity` — the one seam the scanner, startup recovery and
  the launcher all consult — asks before treating a review as valid.

The verdict is durable GitHub label state rather than in-process memory
because the orchestrator recovers its state from labels after a restart
(AGENTS.md, "Labels as Source of Truth"). A crash between the gate and the
review must not lose the refusal.

A label write can nonetheless fail, and that is the asymmetry
:class:`UnrecordedRefusals` closes. ``grant`` failing is safe on its own — the
marker survives and review stays blocked — but ``revoke`` failing used to leave
a refused candidate review-eligible, which is exactly the state this module
exists to prevent, reachable through one failed write. A refusal that cannot be
proved recorded is therefore held here instead, and read back beside the label,
so "not provably recorded" withholds review rather than silently allowing it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from .completion_ports import LabelAdapter

if TYPE_CHECKING:
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class UnrecordedRefusals:
    """Publication-gate refusals whose durable write did not commit (#45).

    One instance per orchestrator, shared by the writer
    (:class:`PublicationAuthority`) and by every reader of the verdict, so a
    refusal the label write lost still withholds review from the candidate it
    refused. Held in process rather than on the issue precisely because the
    issue is what could not be written; the durable marker remains the primary
    record and this only ever *adds* refusals, never removes one the label
    already proves.

    The cover is therefore bounded by the process: a refusal lost to a failed
    write and then lost again to a restart cannot be recovered from anywhere,
    because nothing durable ever recorded it. That is a smaller hole than the
    one it closes, and it fails in the same direction as the rest of the
    verdict — a refusal is only ever forgotten when a later candidate is proved
    to have cleared the gate.

    Completions run off the tick thread, so the holder and the readers are
    different threads. No lock: every operation is one atomic set operation,
    and there is no read-modify-write to tear.
    """

    def __init__(self) -> None:
        self._issue_numbers: set[int] = set()

    def hold(self, issue_number: int) -> None:
        """Withhold review from this issue: its refusal is not recorded."""
        self._issue_numbers.add(issue_number)

    def release(self, issue_number: int) -> None:
        """Drop the hold, because the verdict is now settled elsewhere."""
        self._issue_numbers.discard(issue_number)

    def holds(self, issue_number: int) -> bool:
        """Whether an unrecorded refusal still stands against this issue."""
        return issue_number in self._issue_numbers


def publication_gate_failed(
    label_manager: "LabelManager",
    labels: Sequence[str],
    *,
    issue_number: int,
    unrecorded: UnrecordedRefusals,
) -> bool:
    """Whether this issue's publication gate refused its current candidate.

    Reads the resolved (prefix-aware) marker from the label registry, which is
    the same name :class:`PublicationAuthority` writes — the two cannot drift
    apart into "written under a prefix, read without one" — and then the
    refusals that could not be written at all, which are just as much a
    refusal and must read as one.
    """
    if label_manager.validation_failed in labels:
        return True
    return unrecorded.holds(issue_number)


class PublicationAuthority:
    """The one owner of an issue's publication-gate verdict.

    Constructed with the resolved marker label so callers never spell it
    themselves; :attr:`label` is what a failure comment should name.
    """

    def __init__(
        self,
        labels: LabelAdapter,
        marker_label: str,
        unrecorded: UnrecordedRefusals,
    ) -> None:
        self._labels = labels
        self._label = marker_label
        self._unrecorded = unrecorded

    @property
    def label(self) -> str:
        """The label a recorded publication-gate failure wears."""
        return self._label

    def revoke(self, issue_number: int, *, reason: str) -> None:
        """Record that this issue's publication gate refused its candidate.

        Fails CLOSED. A write failure is not raised — the caller still reports
        the gate failure through its result and its issue comment, and losing
        the label must not turn a refused publication into an exception that
        hides the reason for the refusal — but "not raised" is not "not
        recorded": the refusal is held in :class:`UnrecordedRefusals` instead,
        which withholds review from this candidate exactly as the label would.
        Reporting the refusal and enforcing it are two obligations, and this
        method owes both.
        """
        try:
            self._labels.add_label(issue_number, self._label)
        except Exception as exc:
            # Held BEFORE the log line: nothing between here and the reader may
            # observe the issue as unrefused.
            self._unrecorded.hold(issue_number)
            logger.error(
                "Failed to add '%s' label to issue #%d (%s); the refusal is "
                "held in this process instead, so review stays withheld until "
                "a later candidate clears the gate: %s",
                self._label,
                issue_number,
                reason,
                exc,
            )
            return
        # The label now proves the refusal on its own, so any earlier
        # in-process hold for this issue is redundant.
        self._unrecorded.release(issue_number)
        logger.info(
            "Added '%s' label to issue #%d due to validation failure: %s",
            self._label,
            issue_number,
            reason,
        )

    def grant(self, issue_number: int) -> None:
        """Record that a candidate cleared every publication precondition.

        Removal is idempotent, so this is safe on the ordinary path where no
        failure was ever recorded. A write failure leaves the marker in place,
        which fails closed — review stays blocked until the next candidate
        clears the gate — so it is logged rather than raised, and any
        in-process hold is kept for the same reason.
        """
        try:
            self._labels.remove_label(issue_number, self._label)
        except Exception as exc:
            logger.warning(
                "Failed to clear '%s' label from issue #%d; review stays "
                "blocked until a later candidate clears the gate: %s",
                self._label,
                issue_number,
                exc,
            )
            return
        # Only now, with the durable removal committed, is the refusal over.
        self._unrecorded.release(issue_number)
        logger.debug(
            "Cleared '%s' from issue #%d: publication preconditions passed",
            self._label,
            issue_number,
        )


__all__ = [
    "PublicationAuthority",
    "UnrecordedRefusals",
    "publication_gate_failed",
]
