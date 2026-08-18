"""Flavor-scoped Tech Lead action-kind capabilities (#133).

Two independent axes govern what a tech_lead decision may do::

    flavor/role   -> allowed action kinds     # capability boundary (HERE)
    allowed kind  -> execute | propose        # graduated authority (ADR-0031)

This module owns the FIRST axis, and the table lives here once. Two reads
carry it everywhere it is needed, so nothing restates it:

* :meth:`TechLeadActionCapabilityPolicy.violation` — the JUDGING read, used by
  ``control.tech_lead_decision_contract``, which is the single enforcement
  point. The planner and the reviewer approval gate honour the table
  transitively, through that one validated read; neither imports this module.
* :meth:`TechLeadActionCapabilityPolicy.describe_by_flavor` — the TELLING
  read, used by ``execution.setup_wizard_prompts`` to render the agent-facing
  per-role list, so the prompt cannot advertise a kind the runtime rejects
  (``tests/unit/test_tech_lead_prompt_contract.py`` pins the rendered text to
  this table).

A kind that a role may not propose is a contract violation of the decision
artifact — it cannot be recovered by changing ``tech_lead.authority.*``, by
omitting ``--advise-only``, or by editing a prompt, because none of those
touch this table.

Capability is checked against the ORCHESTRATOR-OWNED launch authority's flavor
(:class:`~.tech_lead_session.TechLeadLaunchAuthority`), never the agent-writable
assignment copy in the worktree, and it is checked at the completion contract
boundary — before authority translation or effect planning — so a single
forbidden action invalidates the whole decision and its siblings produce no
effects.

Where the shipped allowlists come from
--------------------------------------

They are MEASURED from the current completion contract, not guessed, so this
leaf neither narrows nor widens shipped behavior:

* ``FAILURE_INVESTIGATION`` — ``allowed_targets`` and
  ``allowed_act_level_targets`` are both the focus issue, so every kind
  (comment/routing, scope-free, and act-level) has a valid target today.
* ``HEALTH_REVIEW`` — comments/escalations target the anchor and act-level
  proposals target the launch cohort. A storm review's cohort is non-empty, so
  act-level kinds are validly supported by the role; a periodic review simply
  owns no act-level TARGET, which is the target-scope axis's business, not
  this one.
* ``BATCH_REVIEW`` — ``TechLeadLaunchAuthority.allowed_act_level_targets``
  returns the empty set for this flavor UNCONDITIONALLY (manifest entries are
  PRs and the anchor is bookkeeping), so no ``reset_retry`` /
  ``kill_hung_session`` proposal from a batch review has ever been accepted:
  the set of accepted decisions is identical with those kinds excluded here.
  Stating it as a capability says WHY up front ("this role does not do
  recovery") instead of reporting a missing target.

``escalate_to_human`` is deliberately in every shipped role's set: this leaf
adds a capability boundary and must not move the escalation floor.

Expressing a least-authority role
---------------------------------

A future planning role declares its strict set the same way, and the recovery
kinds it omits are then structurally unreachable for it::

    TechLeadActionCapabilityPolicy({
        ...,
        TechLeadSessionFlavor.PLANNING: frozenset(
            ("post_comment", "create_issue", "escalate_to_human")
        ),
    })

No planning flavor is added here (#133 non-goals).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, cast

from .tech_lead_artifacts import VALID_TECH_LEAD_ACTION_TYPES
from .tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from .tech_lead_artifacts import TechLeadDecision


@dataclass(frozen=True, slots=True)
class TechLeadActionCapabilityPolicy:
    """Which action kinds each tech_lead role may propose.

    Construction is total and checked: every :class:`TechLeadSessionFlavor`
    member must declare a non-empty set of known action kinds. A new flavor
    therefore cannot ship without deciding its capability — the alternative
    (an absent key defaulting to "everything" or to "nothing") would let a role
    inherit recovery authority, or lose its voice, by omission.
    """

    allowed_kinds_by_flavor: Mapping[TechLeadSessionFlavor, frozenset[str]]

    def __post_init__(self) -> None:
        declared = dict(self.allowed_kinds_by_flavor)
        missing = [
            flavor.value
            for flavor in TechLeadSessionFlavor
            if flavor not in declared
        ]
        if missing:
            raise ValueError(
                "TechLeadActionCapabilityPolicy must declare allowed action"
                f" kinds for every tech_lead flavor; missing: {sorted(missing)}"
            )
        for flavor, kinds in declared.items():
            # Runtime re-check: annotations carry no runtime guarantee, and a
            # stray key would silently declare a role nothing ever reads.
            key = cast(object, flavor)
            if not isinstance(key, TechLeadSessionFlavor):
                raise ValueError(
                    "TechLeadActionCapabilityPolicy keys must be"
                    f" TechLeadSessionFlavor members, got {key!r}"
                )
            if not kinds:
                raise ValueError(
                    f"tech_lead role {flavor.value} must be allowed at least one"
                    " action kind; a role that can propose nothing cannot"
                    " report what it found"
                )
            unknown = sorted(set(kinds) - VALID_TECH_LEAD_ACTION_TYPES)
            if unknown:
                raise ValueError(
                    f"tech_lead role {flavor.value} declares unknown action"
                    f" kinds: {unknown} (expected a subset of"
                    f" {sorted(VALID_TECH_LEAD_ACTION_TYPES)})"
                )
        object.__setattr__(
            self,
            "allowed_kinds_by_flavor",
            MappingProxyType(
                {flavor: frozenset(kinds) for flavor, kinds in declared.items()}
            ),
        )

    def allowed_kinds(self, flavor: TechLeadSessionFlavor) -> frozenset[str]:
        """The action kinds *flavor* may propose (never empty)."""
        return self.allowed_kinds_by_flavor[flavor]

    def permits(self, flavor: TechLeadSessionFlavor, action_type: str) -> bool:
        """True when a *flavor* session may propose *action_type* at all."""
        return action_type in self.allowed_kinds_by_flavor[flavor]

    def describe_by_flavor(
        self,
    ) -> tuple[tuple[TechLeadSessionFlavor, tuple[str, ...]], ...]:
        """The whole table as an ordered, agent-facing read.

        The rejection path needs only :meth:`violation`; the INSTRUCTION path
        needs the table itself, and without this read the prompt would have to
        restate it by hand — the drift this leaf exists to prevent. Flavors and
        kinds are both sorted so the rendered prompt text is deterministic and
        a contract test can pin it.
        """
        return tuple(
            (flavor, tuple(sorted(self.allowed_kinds(flavor))))
            for flavor in sorted(TechLeadSessionFlavor, key=lambda f: f.value)
        )

    def violation(
        self, decision: "TechLeadDecision", flavor: TechLeadSessionFlavor
    ) -> str | None:
        """Detail for the first forbidden action kind in *decision*, else None.

        The first violation invalidates the WHOLE decision — callers reject the
        completion rather than dropping the offending action — so siblings of a
        forbidden action never reach effect planning.
        """
        for action in decision.proposed_actions:
            if self.permits(flavor, action.action_type):
                continue
            return (
                f"proposed action {action.id} ({action.action_type}) is not an"
                f" action kind a {flavor.value} tech_lead session may propose;"
                " this role's capability is limited to"
                f" {', '.join(sorted(self.allowed_kinds(flavor)))}"
            )
        return None


_COMMENT_AND_ROUTING_KINDS: frozenset[str] = frozenset(
    ("post_comment", "create_issue", "escalate_to_human", "flag_pattern")
)
_RECOVERY_KINDS: frozenset[str] = frozenset(("reset_retry", "kill_hung_session"))

# The shipped table. See the module docstring for how each row was measured
# from the current completion contract.
TECH_LEAD_ACTION_CAPABILITIES = TechLeadActionCapabilityPolicy(
    {
        TechLeadSessionFlavor.BATCH_REVIEW: _COMMENT_AND_ROUTING_KINDS,
        TechLeadSessionFlavor.FAILURE_INVESTIGATION: (
            _COMMENT_AND_ROUTING_KINDS | _RECOVERY_KINDS
        ),
        TechLeadSessionFlavor.HEALTH_REVIEW: (
            _COMMENT_AND_ROUTING_KINDS | _RECOVERY_KINDS
        ),
    }
)
