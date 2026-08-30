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

from .completion_types import (
    ERROR_PREFIX_TECH_LEAD_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION,
    ERROR_PREFIX_TECH_LEAD_DECISION,
)

__all__ = [
    "TECH_LEAD_ERROR_PREFIXES",
    "has_tech_lead_decision_errors",
    "split_tech_lead_decision_error",
]

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


def has_tech_lead_decision_errors(processing_errors: list[str] | None) -> bool:
    """True when the errors include a refusal the tech_lead owner issued."""
    return any(
        error.startswith(TECH_LEAD_ERROR_PREFIXES)
        for error in processing_errors or ()
    )


def split_tech_lead_decision_error(processing_errors: list[str]) -> tuple[str, str]:
    """Parse (failure, detail) back out of the recorded processing error."""
    for error in processing_errors:
        for prefix in TECH_LEAD_ERROR_PREFIXES:
            if not error.startswith(prefix):
                continue
            remainder = error[len(prefix):].lstrip(": ")
            failure, sep, detail = remainder.partition(": ")
            return (failure or "unknown", detail if sep else "")
    return ("unknown", "")
