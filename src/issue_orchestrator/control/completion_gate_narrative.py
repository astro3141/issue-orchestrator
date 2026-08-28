"""What ONE gate owner says about its own failure, in the operator's words.

A leaf beside :mod:`.completion_effect_gate` rather than inside it, because the
gate module already imports the ACTIONS its members are
(:class:`~.result_only_completion.ResultOnlyCloseIssueAction`,
``ResetRetryIssueAction``) and those owners must be able to import this type
back. Splitting the vocabulary from the verdict is what lets each gate owner
declare its own account without either side importing the other (#337 r4, N3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateFailureNarrative:
    """One gate owner's account of ITS OWN failure, in the operator's words.

    The words only. Which label is planted, how the failure details are
    formatted and which action owners carry them are ONE policy shared by every
    gate, and it belongs to :mod:`.completion_gate_surfaces`. What differs
    between gates is what actually went wrong and what the operator should do
    about it — and that is all an owner declares here, so a second gate earning
    a durable surface adds a narrative rather than a second branch at the call
    site.
    """

    subject: str
    """The failing thing, named for the action reasons ("mandated reset_retry")."""

    heading: str
    """The comment's bold title line."""

    explanation: str
    """What the orchestrator did, and why the issue is in the state it is in."""

    label_because: str
    """Why the blocking label was planted, as a "because ..." clause."""

    remedy: str
    """The operator action that ends the block."""


__all__ = ["GateFailureNarrative"]
