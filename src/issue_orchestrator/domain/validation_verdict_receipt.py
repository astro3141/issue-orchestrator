"""What a validation gate decided about one candidate, as a durable fact (#85).

A :class:`~..ports.session_output.ValidationRecord` is the full account of one
gate run, and it lives in the run directory that produced it — inside the coder
worktree. That directory is reaped when the worktree is removed, so after
cleanup the only surviving trace of a publish-gate run was
``Attempt.validation_record_path``: a path to a file that no longer exists, and
in the observed case ``null``. A reader could not tell **"A passed"**, **"A
failed"** and **"A was never gated"** apart.

This is the minimum that has to outlive the worktree for that question to stay
answerable, filed on the record that already survives it — ``Attempt(issue,
A)``, whose sidecar lives in the primary checkout. It is deliberately *not* a
copy of the whole record: no stdout, no stderr, no paths, nothing that points
back into the directory that died.

Four meanings, none of which a reader may lose:

* **Suite identity.** ``suite`` is the label the gate stamped, and
  :meth:`ValidationVerdictReceipt.certifies_publication` asks
  :class:`~.validation_profile.ValidationGateKind` whether that label was
  produced by the *publication* contract. An ``agent_gate`` pass is a pass of
  the quick contract; it must never read as a publication pass.
* **Exact candidate.** ``head_sha`` is the commit the gate validated.
  :class:`~.attempt.Attempt` refuses to hold a receipt naming a different
  commit than the key it is filed under, and :meth:`covers` re-derives the
  match at read time — so A's receipt cannot answer for A′ through either
  door.
* **Executed verdict.** ``verdict`` separates PASS, FAIL and timeout from each
  other. *Absent* — no receipt at all — is how "never gated" reads, and a
  receipt that does not parse raises rather than reading as either; the
  distinction between "not gated" and "the record of the gate is damaged" is
  the same one :func:`~.attempt._optional_execution_identities` keeps.
* **Contract provenance.** ``command`` and ``profile`` are what identifies the
  contract that actually executed. Traced, not assumed: the one predicate in
  this codebase that decides whether a stored gate result may satisfy a
  request — ``ValidationGate._record_matches_request`` — compares suite *and*
  command *and* profile, the last because two profiles may define the same
  command string while naming different contracts ("cache reuse cannot cross
  profiles", #7059). So ``command`` alone does not carry contract identity;
  the receipt carries both, and nothing beyond them.

Evidence only. Nothing here admits, holds, approves or publishes anything;
#45 owns the gate that reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .commit_sha import normalize_commit_sha
from .validation_profile import ValidationGateKind

VALIDATION_VERDICT_RECEIPT_SCHEMA_VERSION = 1


class ValidationVerdict(StrEnum):
    """The three outcomes an *executed* gate run can have.

    There is deliberately no ``UNKNOWN`` member. "No gate ran" is the absence
    of a receipt, not a value inside one: a member for it would let a writer
    file a receipt that says nothing while still occupying the slot a real
    verdict has to be readable from.
    """

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

    @classmethod
    def observed(cls, *, passed: bool, timed_out: bool) -> "ValidationVerdict":
        """The verdict a run reporting these two flags actually reached.

        Timeout outranks ``passed``: a command the orchestrator killed never
        finished the contract, so it must not read as a pass however its
        truncated exit code happened to land.
        """
        if timed_out:
            return cls.TIMED_OUT
        return cls.PASSED if passed else cls.FAILED


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty str")
    return stripped


@dataclass(frozen=True, slots=True)
class ValidationVerdictReceipt:
    """One gate run's verdict for one candidate, small enough to keep forever."""

    suite: str
    head_sha: str
    verdict: ValidationVerdict
    command: str
    profile: str
    schema_version: int = VALIDATION_VERDICT_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "suite", _required_text(self.suite, field_name="suite")
        )
        object.__setattr__(
            self,
            "head_sha",
            normalize_commit_sha(self.head_sha, field_name="head_sha"),
        )
        object.__setattr__(self, "verdict", ValidationVerdict(self.verdict))
        object.__setattr__(
            self, "command", _required_text(self.command, field_name="command")
        )
        object.__setattr__(
            self, "profile", _required_text(self.profile, field_name="profile")
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != VALIDATION_VERDICT_RECEIPT_SCHEMA_VERSION
        ):
            # Fails closed, as the execution-identity record does. A version
            # this code does not know is a record written by a schema it cannot
            # claim to understand; reading it as v1 would let a gate act on
            # fields it may be misreading.
            raise ValueError(
                "validation verdict receipt schema_version must be "
                f"{VALIDATION_VERDICT_RECEIPT_SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )

    def covers(self, head_sha: str) -> bool:
        """Whether this receipt is about ``head_sha`` itself."""
        return (
            normalize_commit_sha(head_sha, field_name="head_sha") == self.head_sha
        )

    @property
    def from_publication_contract(self) -> bool:
        """Whether the suite this receipt carries is the publish contract's.

        Delegated rather than compared against a literal, so this answers the
        same way the validation cache does. A suite the vocabulary does not
        define answers ``False``: an unidentifiable receipt satisfies nothing.
        """
        return ValidationGateKind.PUBLISH.produced(self.suite)

    def certifies_publication(self, head_sha: str) -> bool:
        """Whether ``head_sha`` passed the *publication* contract, per this receipt.

        All three halves, never fewer. Drop the suite check and an
        ``agent_gate`` pass — the quick contract, run by the agent — is read as
        a publication pass. Drop the ``covers`` check and a receipt for an
        earlier candidate admits a later one. Drop the verdict check and a
        recorded failure reads as a success.
        """
        return (
            self.verdict is ValidationVerdict.PASSED
            and self.from_publication_contract
            and self.covers(head_sha)
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ValidationVerdictReceipt":
        """Parse a stored receipt, rejecting anything it cannot read exactly."""
        raw_verdict = payload.get("verdict")
        if not isinstance(raw_verdict, str) or not raw_verdict:
            raise ValueError("validation verdict receipt requires a verdict")
        try:
            verdict = ValidationVerdict(raw_verdict)
        except ValueError as exc:
            raise ValueError(
                f"unknown validation verdict: {raw_verdict!r}"
            ) from exc
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError(
                "validation verdict receipt requires int schema_version"
            )
        for field_name in ("suite", "head_sha", "command", "profile"):
            if field_name not in payload:
                # Absence is named, not inferred from the parse failure the
                # constructor would raise: a receipt missing a field states
                # nothing about the contract, and the message should say which
                # part of the evidence is gone.
                raise ValueError(
                    f"validation verdict receipt requires {field_name}"
                )
        return cls(
            suite=_required_text(payload.get("suite"), field_name="suite"),
            head_sha=normalize_commit_sha(
                payload["head_sha"], field_name="head_sha"
            ),
            verdict=verdict,
            command=_required_text(payload.get("command"), field_name="command"),
            profile=_required_text(payload.get("profile"), field_name="profile"),
            schema_version=schema_version,
        )

    def to_payload(self) -> dict[str, Any]:
        """Render the on-disk form."""
        return {
            "schema_version": self.schema_version,
            "suite": self.suite,
            "head_sha": self.head_sha,
            "verdict": self.verdict.value,
            "command": self.command,
            "profile": self.profile,
        }


__all__ = [
    "VALIDATION_VERDICT_RECEIPT_SCHEMA_VERSION",
    "ValidationVerdict",
    "ValidationVerdictReceipt",
]
