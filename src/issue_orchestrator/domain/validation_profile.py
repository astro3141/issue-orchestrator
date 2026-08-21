"""Domain vocabulary for named validation profiles (#7059).

Only the *name* of the contract, and *which* of a profile's two contracts is
being run, live here. Both appear in durable run state — run manifests,
validation records, validation retry state — which domain and ports types
describe, so both are domain vocabulary.

Parsing ``validation.profiles`` YAML, resolving role bindings and holding the
resolved commands stay in :mod:`issue_orchestrator.infra.validation_profiles`;
those depend on config models and are infrastructure.
"""

from __future__ import annotations

from enum import Enum

DEFAULT_VALIDATION_PROFILE = "default"
"""The profile named by ``validation.quick`` / ``validation.publish``.

A repository that never mentions profiles runs entirely under this name, so
records and manifests written before profiles existed read back as ``default``
rather than as "unknown".
"""

AGENT_GATE_SUITE = "agent_gate"
"""Suite recorded by the agent-side gate at ``coding-done`` (#25).

A distinct *caller* running the same ``quick`` contract as the orchestrator's
own quick gate. It keeps its own suite label so a record says who produced it,
and :meth:`ValidationGateKind.from_suite` maps it back to ``QUICK`` so both
callers still share one cache location for one contract.
"""


class ValidationGateKind(Enum):
    """Which of a profile's two contracts a gate run executes (#25).

    A profile defines two deliberately different contracts: ``quick`` for
    active coding/review loops, ``publish`` for the authoritative
    pre-publication gate. Before this type existed, a gate was handed a
    free-form command *and* stamped a fixed suite name onto the record, so a
    record could honestly read ``suite=publish_gate`` while the command it ran
    was the quick selector — the suite label proved nothing.

    Making the kind the single input closes that: the suite name and the
    command are now two projections of one value, and neither can be chosen
    without the other.
    """

    QUICK = "quick"
    PUBLISH = "publish"

    @property
    def suite(self) -> str:
        """The suite label a record produced under this contract carries."""
        return f"{self.value}_gate"

    @classmethod
    def from_suite(cls, suite: str) -> "ValidationGateKind":
        """The contract a recorded ``suite`` label was produced under.

        Raises:
            ValueError: for a suite this vocabulary does not define. A record
                whose contract cannot be identified must not be silently
                treated as either one.
        """
        if suite == AGENT_GATE_SUITE:
            return cls.QUICK
        for kind in cls:
            if kind.suite == suite:
                return kind
        raise ValueError(f"Unknown validation suite: {suite!r}")

    @classmethod
    def defines(cls, suite: str) -> bool:
        """Whether this vocabulary owns ``suite`` at all.

        The question :meth:`produced` cannot answer for a *third* lane. A
        record from a lane that is not validation — live-agent assurance
        (#194) — must be unable to claim a validation suite label, and the
        only honest way to check that is to ask the vocabulary that owns the
        labels rather than to compare against literals a second module would
        then have to keep in sync.
        """
        try:
            cls.from_suite(suite)
        except ValueError:
            return False
        return True

    def produced(self, suite: str) -> bool:
        """Whether a record carrying ``suite`` came from this contract.

        The one question cache reuse asks of a stored record. A suite this
        vocabulary does not define answers ``False`` for every contract: an
        unidentifiable record satisfies nothing.
        """
        try:
            return ValidationGateKind.from_suite(suite) is self
        except ValueError:
            return False


__all__ = [
    "AGENT_GATE_SUITE",
    "DEFAULT_VALIDATION_PROFILE",
    "ValidationGateKind",
]
