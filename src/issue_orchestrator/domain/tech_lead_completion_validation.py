"""What a trusted owner concluded about ONE Tech Lead completion (#385).

#370 moved the *publication* validation off the Tech Lead model session. What
it did not move is the completion protocol's own pre-push obligation: the
shipped instructions still told the model to run ``prepush-check --dirty-only
-v`` before ``coding-done``, and that command records its timing under the
repository's shared git common dir
(``<git-common-dir>/issue-orchestrator/validate-timings.jsonl``) — a write
outside a bounded Tech Lead's sandbox write roots. A live bounded run therefore
could not complete at all.

The repair is an ownership move, never a waiver: the model no longer runs the
command, and a trusted owner outside the model sandbox runs the equivalent
check and files THIS value as durable evidence. It is deliberately a value
object with no I/O, so the fact a completion is gated on can be constructed,
persisted, re-read and compared without any layer having to agree on where it
lives.

**Exact binding is part of the evidence, not part of the lookup.** Every
instance names the run, the session and the commit it was taken on, and
:meth:`TechLeadCompletionValidation.binds_to` is the only way to ask whether it
describes the completion in hand. Evidence for another candidate, another run,
or a head that has since moved answers ``False`` — it does not answer "close
enough". :meth:`TechLeadCompletionValidation.refusal_against` is that question
and the status question asked together, as a
:class:`TrustedVerdictRefusal` naming which one said no, so the two lanes gated
on a trusted verdict (#385, #388) cannot come to ask them differently.

**Every not-PASSED status is a distinct recorded fact.** A gate that failed, a
gate that timed out, and a gate whose result could not be produced or read at
all are three different things an operator has to be able to tell apart, and
collapsing them into ``not passed`` would lose the only signal that says which
one to go fix. What they share is the consequence, and that is stated once, on
:attr:`TechLeadCompletionValidationStatus.permits_completion`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "TECH_LEAD_COMPLETION_VALIDATION_SCHEMA",
    "TechLeadCompletionValidation",
    "TechLeadCompletionValidationStatus",
    "TrustedVerdictRefusal",
]

#: Version stamped on the persisted payload. A reader that does not recognise a
#: payload's schema must treat the evidence as unreadable rather than guess at
#: its meaning — the fail-closed direction (#385 R3).
TECH_LEAD_COMPLETION_VALIDATION_SCHEMA = 1


class TechLeadCompletionValidationStatus(Enum):
    """How the trusted completion validation ended.

    ``permits_completion`` is a property of the member rather than a check at
    the call site, so adding a status forces its author to answer whether a
    Tech Lead holding it may settle terminally. Exactly one member answers yes.
    """

    #: The trusted owner ran the check and it passed on this exact candidate.
    PASSED = "passed"
    #: The trusted owner ran the check and it failed (a nonzero gate, a dirty
    #: publishable tree).
    FAILED = "failed"
    #: The trusted owner started the check and it did not finish in time.
    TIMED_OUT = "timed_out"
    #: No result could be produced or read back — the owner is not wired, the
    #: repository state could not be enumerated, or the durable record was
    #: unreadable. Never "assume it would have passed".
    UNAVAILABLE = "unavailable"

    @property
    def permits_completion(self) -> bool:
        """Whether a Tech Lead completion may settle terminally on this."""
        return self is TechLeadCompletionValidationStatus.PASSED


@dataclass(frozen=True, slots=True)
class TrustedVerdictRefusal:
    """Why a trusted verdict may not be settled on, in two named parts.

    ``failure`` is the machine-facing cause; ``detail`` is the sentence an
    operator reads. Kept separate because the callers that ask for a trusted
    verdict surface them differently — the Tech Lead completion gate tags them
    into the error string its terminal-effects path parses back (#385), while
    the review exchange turns them into the round's protocol error (#388) — and
    neither should have to take the other's shape apart.

    A value object with no I/O, beside the evidence it describes, so "this
    evidence is not usable for that completion" can be constructed and compared
    without any layer having to agree on where either lives.
    """

    failure: str
    detail: str


@dataclass(frozen=True, slots=True)
class TechLeadCompletionValidation:
    """Durable, exactly-bound evidence about one Tech Lead run's completion.

    Attributes:
        run_id: The orchestrator-allocated run this evidence belongs to.
        session_name: The session within that run.
        candidate_head_sha: The commit the checkout stood at when the trusted
            owner took the evidence. Required for EVERY status, including the
            failing ones: evidence that cannot name its candidate cannot be
            compared to one, and un-comparable evidence is what F4 exists to
            forbid.
        status: What the trusted owner concluded.
        detail: Why, in the owner's own words. Never empty — an operator
            reading a refused completion is reading this sentence.
        recorded_at: When the evidence was taken (timezone-aware UTC).
    """

    run_id: str
    session_name: str
    candidate_head_sha: str
    status: TechLeadCompletionValidationStatus
    detail: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("run_id", "session_name", "candidate_head_sha", "detail"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(
                    "TechLeadCompletionValidation requires a non-empty "
                    f"{field_name}; evidence that cannot name what it is about "
                    "cannot gate anything"
                )
        if self.recorded_at.tzinfo is None:
            raise ValueError(
                "TechLeadCompletionValidation.recorded_at must be timezone-aware"
            )

    @property
    def permits_completion(self) -> bool:
        """Whether this evidence lets a Tech Lead completion settle."""
        return self.status.permits_completion

    def binds_to(
        self, *, run_id: str, session_name: str, candidate_head_sha: str
    ) -> bool:
        """Whether this evidence describes exactly that run/session/commit."""
        return (
            self.run_id == run_id
            and self.session_name == session_name
            and self.candidate_head_sha == candidate_head_sha
        )

    def refusal_against(
        self, *, run_id: str, session_name: str, candidate_head_sha: str
    ) -> "TrustedVerdictRefusal | None":
        """Why this evidence cannot settle that completion, or ``None``.

        The two questions every caller of a trusted verdict has to ask about
        one, in the order that keeps them distinguishable: does it describe
        THIS candidate at all, and does it permit settling. They stay two
        answers because their remedies differ — a drifted binding means the
        candidate moved, a non-passing status means the check said no — and an
        operator who cannot tell them apart cannot tell which to go fix.

        Owned here, beside :meth:`binds_to` and :attr:`permits_completion`,
        because it is the same rule those two state, asked once instead of
        re-assembled at each caller.
        """
        if not self.binds_to(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
        ):
            return TrustedVerdictRefusal(
                failure="candidate_drift",
                detail=(
                    "the trusted completion validation is bound to"
                    f" {self.run_id}/{self.session_name}@"
                    f"{self.candidate_head_sha}, not to the"
                    f" {run_id}/{session_name}@{candidate_head_sha} this"
                    " completion settles"
                ),
            )
        if self.permits_completion:
            return None
        return TrustedVerdictRefusal(
            failure=f"validation_{self.status.value}",
            detail=(
                f"the trusted completion validation for {run_id}/{session_name}@"
                f"{candidate_head_sha} did not pass: {self.detail}"
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        """The persisted form, schema-stamped."""
        return {
            "schema": TECH_LEAD_COMPLETION_VALIDATION_SCHEMA,
            "run_id": self.run_id,
            "session_name": self.session_name,
            "candidate_head_sha": self.candidate_head_sha,
            "status": self.status.value,
            "detail": self.detail,
            "recorded_at": self.recorded_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TechLeadCompletionValidation":
        """Rehydrate persisted evidence, or raise ``ValueError`` if it is not.

        Total on bad content: a payload that is not an object, carries an
        unknown schema, names a status this build does not have, or omits a
        required field raises rather than yielding a half-built value. The
        caller's fail-closed branch is the one that then runs.
        """
        if not isinstance(payload, dict):
            raise ValueError(
                "tech_lead completion-validation payload must be a JSON object"
            )
        schema = payload.get("schema")
        if schema != TECH_LEAD_COMPLETION_VALIDATION_SCHEMA:
            raise ValueError(
                "unknown tech_lead completion-validation schema "
                f"{schema!r} (expected {TECH_LEAD_COMPLETION_VALIDATION_SCHEMA})"
            )
        try:
            status = TechLeadCompletionValidationStatus(payload["status"])
            recorded_at = datetime.fromisoformat(str(payload["recorded_at"]))
            return cls(
                run_id=str(payload["run_id"]),
                session_name=str(payload["session_name"]),
                candidate_head_sha=str(payload["candidate_head_sha"]),
                status=status,
                detail=str(payload["detail"]),
                recorded_at=recorded_at,
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"tech_lead completion-validation payload is incomplete: {exc}"
            ) from exc

    @classmethod
    def concluded(
        cls,
        *,
        run_id: str,
        session_name: str,
        candidate_head_sha: str,
        status: TechLeadCompletionValidationStatus,
        detail: str,
        recorded_at: datetime | None = None,
    ) -> "TechLeadCompletionValidation":
        """Build evidence, defaulting only the clock."""
        return cls(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
            status=status,
            detail=detail,
            recorded_at=recorded_at or datetime.now(timezone.utc),
        )
