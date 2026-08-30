"""How a tech_lead refusal is tagged, recognised, and read back.

Three paths share this vocabulary and must not drift apart: the landed
completion path that WRITES the tagged error
(:mod:`.tech_lead_completion`), the action planner that decides a tagged error
routes to the tech_lead owner rather than to the generic publish-failure lane
(:mod:`.completion_action_planner`), and the terminal-effects path that PARSES
it back into the sentence an operator reads on the anchor issue
(:mod:`.tech_lead_terminal_effects`).

It lives in its own module for the reason the set keeps growing: #385 added a
third refusal cause (a completion the trusted validation owner did not clear),
and a cause that is recognised on one path but not parsed on another produces a
FAILED session with no explanation, or an explained failure the planner routes
to the wrong owner. One tuple, one place, every path.
"""

from __future__ import annotations

from enum import Enum

from .completion_types import (
    ERROR_PREFIX_TECH_LEAD_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION,
    ERROR_PREFIX_TECH_LEAD_DECISION,
)

__all__ = [
    "TECH_LEAD_ERROR_PREFIXES",
    "TechLeadRefusalKind",
    "has_tech_lead_refusal",
    "split_tech_lead_refusal",
    "tech_lead_refusal_kind",
]


class TechLeadRefusalKind(Enum):
    """WHAT the tech_lead owner refused, as the noun an operator reads.

    The three refusals point at three different remedies — a decision artifact
    the session must rewrite, a launch authority the orchestrator must have
    recorded, and a completion validation the trusted owner must be able to run
    and clear — so collapsing them into one surfaced noun sends the operator to
    the wrong one (#385 round 2 N1). This is the same argument
    :class:`TechLeadCompletionValidationStatus` makes for keeping
    ``FAILED``/``TIMED_OUT``/``UNAVAILABLE`` distinct, applied one level up.

    The value is prose because it is read in a sentence; the machine-readable
    axis stays the ``failure`` slug and the prefix that carried it.
    """

    DECISION = "decision"
    LAUNCH_AUTHORITY = "launch authority"
    COMPLETION_VALIDATION = "completion validation"


#: The noun each refusal prefix maps to. Declared here, beside the prefix tuple
#: it must stay exhaustive over, so a fourth refusal cause cannot be added
#: without deciding what an operator is told it was.
_REFUSAL_KIND_BY_PREFIX: dict[str, TechLeadRefusalKind] = {
    ERROR_PREFIX_TECH_LEAD_DECISION: TechLeadRefusalKind.DECISION,
    ERROR_PREFIX_TECH_LEAD_AUTHORITY: TechLeadRefusalKind.LAUNCH_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION: (
        TechLeadRefusalKind.COMPLETION_VALIDATION
    ),
}

#: Every refusal the tech_lead owner itself issues. A rejected/missing decision
#: pair, a missing or tampered launch authority, and — since #385 — a
#: completion whose mandatory trusted validation did not pass on this exact
#: candidate. All three are the run's OWN owner refusing it, which is why they
#: share a routing consequence the publish prefixes do not.
TECH_LEAD_ERROR_PREFIXES = (
    ERROR_PREFIX_TECH_LEAD_DECISION,
    ERROR_PREFIX_TECH_LEAD_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION,
)


def has_tech_lead_refusal(processing_errors: list[str] | None) -> bool:
    """True when the errors include a refusal the tech_lead owner issued.

    Named for the whole set rather than for one member: only one of the three
    prefixes is a *decision* refusal, and the older ``..._decision_errors``
    name read as though a missing launch authority or an uncleared completion
    validation were outside it (#385 round 1 N2).
    """
    return any(
        error.startswith(TECH_LEAD_ERROR_PREFIXES)
        for error in processing_errors or ()
    )


def split_tech_lead_refusal(processing_errors: list[str]) -> tuple[str, str]:
    """Parse (failure, detail) back out of the recorded processing error."""
    for error in processing_errors:
        for prefix in TECH_LEAD_ERROR_PREFIXES:
            if not error.startswith(prefix):
                continue
            remainder = error[len(prefix):].lstrip(": ")
            failure, sep, detail = remainder.partition(": ")
            return (failure or "unknown", detail if sep else "")
    return ("unknown", "")


def tech_lead_refusal_kind(processing_errors: list[str]) -> TechLeadRefusalKind:
    """Which of the three refusals the recorded processing error carries.

    Read from the same first-match scan :func:`split_tech_lead_refusal` uses, so
    the kind an operator is told about and the ``(failure, detail)`` they read
    always come from ONE error rather than from two independent scans that
    could land on different members of the list.

    An unrecognised or absent prefix answers ``DECISION``: this is the surfacing
    path only, so the conservative direction is the noun the surface has always
    used, never a new one that would read as a new kind of refusal.
    """
    for error in processing_errors:
        for prefix in TECH_LEAD_ERROR_PREFIXES:
            if error.startswith(prefix):
                return _REFUSAL_KIND_BY_PREFIX[prefix]
    return TechLeadRefusalKind.DECISION
