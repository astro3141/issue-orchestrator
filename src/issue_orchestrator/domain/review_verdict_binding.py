"""Durable exact-SHA review verdict binding.

A review verdict on its own says nothing about *what* was reviewed. The
Foundation validated-work disposition contract
(``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4) admits work only when

    validation.head_sha == review.reviewed_sha == A

Validation already satisfies its half — :class:`~..ports.session_output.ValidationRecord`
names ``head_sha`` and ``passed``. This module is review's equivalent: a verdict
and the exact commit it was rendered against, preserved as **one** record.

Two properties make it an authority artifact rather than a convenience:

* **Neither half is agent-supplied.** The verdict is derived from the terminal
  state the orchestrator recorded for the exchange, and ``reviewed_sha`` is the
  commit the orchestrator itself checked out into the reviewer's worktree for
  that round — not a later reading of where the coder's branch has since got
  to. A reviewer claiming "I looked at X" is a claim; it never reaches this
  record.
* **The pairing is structural.** A payload naming a verdict without a SHA, or a
  SHA without a verdict, does not parse. There is no state in which half of the
  binding exists.

Validity is a standing predicate, not remembered history: :meth:`BoundReviewVerdict.approves`
is re-derived against whatever HEAD is current whenever it matters, so a verdict
whose candidate moved is detectably stale rather than quietly reusable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .commit_sha import normalize_commit_sha

REVIEW_VERDICT_BINDING_FILENAME = "review-verdict.json"
REVIEW_VERDICT_BINDING_SCHEMA_VERSION = 1


class ReviewVerdictOutcome(StrEnum):
    """The verdict the orchestrator recorded for one review.

    Deliberately narrower than the reviewer-authored
    :data:`~.review_artifacts.ReviewVerdict` vocabulary: this enum names what
    the orchestrator concluded, so there is no ``disagree`` member and no
    representation for "the reviewer said approved but policy sent the work
    back to rework".
    """

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


def normalize_reviewed_sha(value: object, *, field_name: str = "reviewed_sha") -> str:
    """Return ``value`` as a canonical full commit SHA, or raise.

    Review's spelling of :func:`~.commit_sha.normalize_commit_sha`. The rule
    itself lives there because §4 is an equality *between* records: the actor
    and reviewer execution identities bound to a candidate
    (:mod:`~.execution_identity`) must decide "same commit" exactly as this
    binding does, and two copies of the rule are two chances to drift.
    """
    return normalize_commit_sha(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class BoundReviewVerdict:
    """One review verdict and the exact commit it was rendered against."""

    verdict: ReviewVerdictOutcome
    reviewed_sha: str
    decided_at: str
    completed_rounds: int
    schema_version: int = REVIEW_VERDICT_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", ReviewVerdictOutcome(self.verdict))
        object.__setattr__(
            self, "reviewed_sha", normalize_reviewed_sha(self.reviewed_sha)
        )
        if not self.decided_at.strip():
            raise ValueError("decided_at must be a non-empty str")
        if type(self.completed_rounds) is not int or self.completed_rounds < 1:
            raise ValueError("completed_rounds must be an int >= 1")
        if (
            type(self.schema_version) is not int
            or self.schema_version != REVIEW_VERDICT_BINDING_SCHEMA_VERSION
        ):
            # Fails closed on purpose. A version this code does not know is a
            # record written by a schema it cannot claim to understand;
            # reading it as if it were v1 would let an admission gate act on
            # fields it may be misreading.
            raise ValueError(
                "review verdict binding schema_version must be "
                f"{REVIEW_VERDICT_BINDING_SCHEMA_VERSION}, got "
                f"{self.schema_version!r}"
            )

    def covers(self, head_sha: str) -> bool:
        """Whether this verdict was rendered against ``head_sha``."""
        return normalize_reviewed_sha(head_sha, field_name="head_sha") == self.reviewed_sha

    def approves(self, head_sha: str) -> bool:
        """Whether this verdict is an approval *of ``head_sha`` itself*.

        The only question an admission gate should ask. An approval bound to a
        different commit answers ``False`` — it is evidence about other work.
        """
        return self.verdict is ReviewVerdictOutcome.APPROVED and self.covers(head_sha)

    def is_stale_for(self, head_sha: str) -> bool:
        """Whether ``head_sha`` has moved away from what was reviewed."""
        return not self.covers(head_sha)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BoundReviewVerdict":
        """Parse a stored binding, rejecting any payload missing either half."""
        raw_verdict = payload.get("verdict")
        if not isinstance(raw_verdict, str) or not raw_verdict:
            raise ValueError("review verdict binding requires a verdict")
        try:
            verdict = ReviewVerdictOutcome(raw_verdict)
        except ValueError as exc:
            raise ValueError(
                f"unknown review verdict binding verdict: {raw_verdict!r}"
            ) from exc
        if "reviewed_sha" not in payload:
            raise ValueError("review verdict binding requires reviewed_sha")
        decided_at = payload.get("decided_at")
        if not isinstance(decided_at, str) or not decided_at.strip():
            raise ValueError("review verdict binding requires decided_at")
        completed_rounds = payload.get("completed_rounds")
        if not isinstance(completed_rounds, int) or isinstance(completed_rounds, bool):
            raise ValueError("review verdict binding requires int completed_rounds")
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("review verdict binding requires int schema_version")
        return cls(
            verdict=verdict,
            reviewed_sha=normalize_reviewed_sha(payload["reviewed_sha"]),
            decided_at=decided_at,
            completed_rounds=completed_rounds,
            schema_version=schema_version,
        )

    def to_payload(self) -> dict[str, Any]:
        """Render the on-disk form. Both halves are always present."""
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "reviewed_sha": self.reviewed_sha,
            "decided_at": self.decided_at,
            "completed_rounds": self.completed_rounds,
        }
