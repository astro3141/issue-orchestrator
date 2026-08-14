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
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from .completion_ports import LabelAdapter

if TYPE_CHECKING:
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


def publication_gate_failed(
    label_manager: "LabelManager", labels: Sequence[str]
) -> bool:
    """Whether *labels* record a publication gate that did not pass.

    Reads the resolved (prefix-aware) marker from the label registry, which is
    the same name :class:`PublicationAuthority` writes — the two cannot drift
    apart into "written under a prefix, read without one".
    """
    return label_manager.validation_failed in labels


class PublicationAuthority:
    """The one owner of an issue's publication-gate verdict.

    Constructed with the resolved marker label so callers never spell it
    themselves; :attr:`label` is what a failure comment should name.
    """

    def __init__(self, labels: LabelAdapter, marker_label: str) -> None:
        self._labels = labels
        self._label = marker_label

    @property
    def label(self) -> str:
        """The label a recorded publication-gate failure wears."""
        return self._label

    def revoke(self, issue_number: int, *, reason: str) -> None:
        """Record that this issue's publication gate refused its candidate.

        A write failure is logged, not raised: the caller still reports the
        gate failure through its result and its issue comment, and losing the
        label must not turn a refused publication into an exception that hides
        the reason for the refusal.
        """
        try:
            self._labels.add_label(issue_number, self._label)
        except Exception as exc:
            logger.warning(
                "Failed to add '%s' label to issue #%d: %s",
                self._label,
                issue_number,
                exc,
            )
            return
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
        clears the gate — so it is logged rather than raised.
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
        logger.debug(
            "Cleared '%s' from issue #%d: publication preconditions passed",
            self._label,
            issue_number,
        )


__all__ = ["PublicationAuthority", "publication_gate_failed"]
