"""What a live-agent security probe proved about one exact artifact (#194).

Publication validation and live-agent assurance are two differently-owned
kinds of evidence, and #109 is what happens when one gate carries both. The
OS-sandbox boundary proof drives a **real** provider CLI, so whether it
executes at all depends on an external model choosing to issue a tool call.
Three times the model simply did not issue probe 3, and because that probe sat
inside the pinned publication gate the result was recorded as a *candidate*
failure — most recently against a candidate whose changes had nothing to do
with sandbox behaviour.

The fix is not to weaken the probe. It is to stop asking one verdict to mean
two things. A publication verdict answers "did this candidate pass the
publication contract"; a live-assurance record answers "was the allow/deny
boundary actually exercised, and did it hold". This module is the second one,
and it is deliberately a *different* vocabulary from
:mod:`.validation_verdict_receipt` rather than a flag added to it.

Three outcomes, and the middle one is the whole point:

* ``PASS`` — the required operation was really issued and the boundary held.
* ``SECURITY_FAIL`` — the boundary was really exercised and an assertion about
  it failed. This is a security result and outranks everything else.
* ``INCONCLUSIVE`` — the provider was unavailable, the run timed out, or the
  model never issued the required operation. It is **neither** a candidate
  failure nor a security pass. The failed observation is preserved in
  ``detail`` rather than being reinterpreted as either.

There is no fourth member and no ``UNKNOWN``: "the lane never ran" is the
absence of a record, exactly as :class:`~.validation_verdict_receipt.
ValidationVerdict` keeps "never gated" outside its own enum.

**Evidence identity, not a flag.** ``suite`` is carried and validated, and
:class:`~.validation_profile.ValidationGateKind` is asked whether it owns the
label. It must not: a record that could name ``publish_gate`` would be a
publication receipt wearing an assurance record's shape, and the discipline
:meth:`~.validation_verdict_receipt.ValidationVerdictReceipt.
certifies_publication` established — an ``agent_gate`` pass is never a
publication pass — has to hold for a third lane too. Both directions are
closed: this record refuses a validation suite on construction *and* on parse,
and a :class:`ValidationVerdictReceipt` carrying
:data:`LIVE_ASSURANCE_SUITE` certifies nothing, because
``ValidationGateKind.PUBLISH.produced`` does not recognise the label.

Evidence only. Nothing here promotes anything;
:mod:`..control.trusted_runtime_promotion` owns the gate that reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .commit_sha import normalize_commit_sha
from .validation_profile import ValidationGateKind

LIVE_ASSURANCE_SCHEMA_VERSION = 1

LIVE_ASSURANCE_SUITE = "live_assurance"
"""The lane's own suite label.

Deliberately not ``*_gate``: the two validation contracts spell their suites
``quick_gate`` and ``publish_gate``, and a third label in that family would
invite a reader to treat all three as members of one vocabulary. They are not.
:meth:`ValidationGateKind.defines` is asserted against this value below so the
separation is checked rather than assumed.
"""


class LiveAssuranceOutcome(StrEnum):
    """The three results a live-assurance lane run can reach."""

    PASS = "pass"
    SECURITY_FAIL = "security_fail"
    INCONCLUSIVE = "inconclusive"

    @classmethod
    def observed(cls, *, breached: bool, incomplete: bool) -> "LiveAssuranceOutcome":
        """The outcome a lane reporting these two observations actually reached.

        ``breached`` outranks ``incomplete``: a run in which one probe proved a
        real boundary violation and another never issued its operation is a
        security result, and reporting it as ``INCONCLUSIVE`` would let a
        proven breach hide behind an unrelated provider hiccup.

        ``incomplete`` outranks a pass for the mirror-image reason: a lane that
        skipped, timed out, or never got the required operation issued has not
        proven the boundary, however green the assertions it did reach were.
        """
        if breached:
            return cls.SECURITY_FAIL
        if incomplete:
            return cls.INCONCLUSIVE
        return cls.PASS


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty str")
    return stripped


@dataclass(frozen=True, slots=True)
class LiveAssuranceRecord:
    """One live-assurance lane run's result for one exact artifact."""

    head_sha: str
    outcome: LiveAssuranceOutcome
    detail: str
    suite: str = LIVE_ASSURANCE_SUITE
    schema_version: int = LIVE_ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "head_sha",
            normalize_commit_sha(self.head_sha, field_name="head_sha"),
        )
        object.__setattr__(self, "outcome", LiveAssuranceOutcome(self.outcome))
        object.__setattr__(
            self, "detail", _required_text(self.detail, field_name="detail")
        )
        suite = _required_text(self.suite, field_name="suite")
        if ValidationGateKind.defines(suite):
            # The crossover guard, in the direction a *writer* could breach it.
            # A record naming a validation suite would satisfy readers that
            # only ever compare labels, so it is refused at construction rather
            # than trusted to be spelled correctly by every call site.
            raise ValueError(
                f"live-assurance suite must not be a validation suite: {suite!r}"
            )
        if suite != LIVE_ASSURANCE_SUITE:
            raise ValueError(
                f"live-assurance suite must be {LIVE_ASSURANCE_SUITE!r}, got {suite!r}"
            )
        object.__setattr__(self, "suite", suite)
        if (
            type(self.schema_version) is not int
            or self.schema_version != LIVE_ASSURANCE_SCHEMA_VERSION
        ):
            # Fails closed, as the verdict receipt does. A version this code
            # does not know is a record written by a schema it cannot claim to
            # understand.
            raise ValueError(
                "live-assurance record schema_version must be "
                f"{LIVE_ASSURANCE_SCHEMA_VERSION}, got {self.schema_version!r}"
            )

    def covers(self, head_sha: str) -> bool:
        """Whether this record is about ``head_sha`` itself."""
        return normalize_commit_sha(head_sha, field_name="head_sha") == self.head_sha

    def assures(self, head_sha: str) -> bool:
        """Whether the boundary was proven to hold for ``head_sha`` exactly.

        Both halves, never one. Drop the outcome check and an ``INCONCLUSIVE``
        run — the model never issued the operation — reads as a proof. Drop
        ``covers`` and a different artifact's proof admits this one, which is
        the exact-artifact requirement the whole record exists to carry.
        """
        return self.outcome is LiveAssuranceOutcome.PASS and self.covers(head_sha)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "LiveAssuranceRecord":
        """Parse a stored record, rejecting anything it cannot read exactly."""
        raw_outcome = payload.get("outcome")
        if not isinstance(raw_outcome, str) or not raw_outcome:
            raise ValueError("live-assurance record requires an outcome")
        try:
            outcome = LiveAssuranceOutcome(raw_outcome)
        except ValueError as exc:
            raise ValueError(f"unknown live-assurance outcome: {raw_outcome!r}") from exc
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("live-assurance record requires int schema_version")
        for field_name in ("suite", "head_sha", "detail"):
            if field_name not in payload:
                raise ValueError(f"live-assurance record requires {field_name}")
        return cls(
            head_sha=normalize_commit_sha(payload["head_sha"], field_name="head_sha"),
            outcome=outcome,
            detail=_required_text(payload.get("detail"), field_name="detail"),
            suite=_required_text(payload.get("suite"), field_name="suite"),
            schema_version=schema_version,
        )

    def to_payload(self) -> dict[str, Any]:
        """Render the on-disk form."""
        return {
            "schema_version": self.schema_version,
            "suite": self.suite,
            "head_sha": self.head_sha,
            "outcome": self.outcome.value,
            "detail": self.detail,
        }


__all__ = [
    "LIVE_ASSURANCE_SCHEMA_VERSION",
    "LIVE_ASSURANCE_SUITE",
    "LiveAssuranceOutcome",
    "LiveAssuranceRecord",
]
