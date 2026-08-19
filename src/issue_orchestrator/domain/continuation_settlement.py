"""What one continuation run produced, recorded so it never runs again (#149).

The descriptor says what a candidate's continuation should *do*; this says it
has been done. Without it the continuation has a live-truth owner and a runner
and no terminal: every phase that means "still owes work" is derived from facts
that a completed run does not change, so a run that created the pull request
its intent asked for derives exactly the same phase on the next pass and runs
again — a full reviewer exchange per reconciliation, forever, with the issue's
lane held throughout.

Settlement is recorded from the fact the run itself produced rather than from a
board signal, for a reason the ``pr-pending`` label makes concrete: that label
is written when a *Session* completes with a PR, when a scan finds one carrying
the code-review label, or when the publish-retry route finalizes itself. The
continuation creates no session, and drives a reviewer-first exchange whose PR
carries no code-review label, so it goes through none of those. Waiting on the
board would be waiting on a signal that by construction never arrives.

Two settlements exist, and they are the two ways a replayed intent can be
fully discharged:

===============================  =============================================
``PULL_REQUEST_OPENED``          the intent asked for a PR and the run made one
``NOTHING_FURTHER_REQUESTED``    the intent asked for no PR, and the run ended
===============================  =============================================

A run that asked for a pull request and produced none settles NOTHING. That is
deliberate: the intent is undischarged, the facts are unchanged, and the next
reconciliation derives the same phase and tries again — the runner's own
failure rule, which a settlement recorded on a failure would silently defeat.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

CONTINUATION_SETTLEMENT_SCHEMA_VERSION = 1


class ContinuationSettlementKind(Enum):
    """How a continuation's recorded intent was discharged."""

    #: The run created the pull request the intent asked for.
    PULL_REQUEST_OPENED = "pull_request_opened"
    #: The intent asked for no pull request, so the run's end is the end.
    NOTHING_FURTHER_REQUESTED = "nothing_further_requested"


@dataclass(frozen=True, slots=True)
class ContinuationSettlement:
    """The terminal outcome of one candidate's continuation.

    Not bound to a commit by a field of its own, for the reason
    :class:`~.continuation_descriptor.ContinuationDescriptor` is not: it is
    stored on :class:`~.attempt.Attempt`, whose key already *is* the
    ``(issue, commit)`` pair, so there is no second spelling of the binding
    that could disagree with the record it is filed under.
    """

    kind: ContinuationSettlementKind
    settled_at: str
    #: The pull request the run opened. Required by
    #: :attr:`ContinuationSettlementKind.PULL_REQUEST_OPENED` and forbidden
    #: otherwise, so a settlement cannot claim a PR it has no evidence of.
    pr_url: str | None = None
    schema_version: int = CONTINUATION_SETTLEMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.settled_at, "settled_at")
        opened = self.kind is ContinuationSettlementKind.PULL_REQUEST_OPENED
        has_url = isinstance(self.pr_url, str) and bool(self.pr_url.strip())
        if opened and not has_url:
            raise ValueError(
                "ContinuationSettlement.pr_url is required when a pull request "
                "was opened"
            )
        if not opened and self.pr_url is not None:
            raise ValueError(
                "ContinuationSettlement.pr_url must be absent unless a pull "
                f"request was opened: kind={self.kind.value}"
            )
        if (
            type(self.schema_version) is not int
            or self.schema_version != CONTINUATION_SETTLEMENT_SCHEMA_VERSION
        ):
            # Fails closed exactly as the descriptor does: a version this code
            # does not know is a record written by a schema it cannot claim to
            # understand, and settlement decides whether work runs again.
            raise ValueError(
                "continuation settlement schema_version must be "
                f"{CONTINUATION_SETTLEMENT_SCHEMA_VERSION}, got "
                f"{self.schema_version!r}"
            )

    @property
    def opened_pull_request(self) -> bool:
        """Whether this settlement is the pull request the intent asked for."""
        return self.kind is ContinuationSettlementKind.PULL_REQUEST_OPENED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "settled_at": self.settled_at,
            "pr_url": self.pr_url,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ContinuationSettlement":
        """Parse a stored settlement, refusing anything it cannot read exactly.

        Damage raises rather than reading as absence, for the reason the
        descriptor's parser gives: "this continuation never settled" and "the
        record of how it settled is corrupt" demand different answers, and only
        the first may put an operation back on the runner.
        """
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("continuation settlement requires int schema_version")
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise ValueError("continuation settlement kind must be a str")
        try:
            parsed = ContinuationSettlementKind(kind)
        except ValueError as exc:
            raise ValueError(
                f"unknown continuation settlement kind: {kind!r}"
            ) from exc
        settled_at = payload.get("settled_at")
        if not isinstance(settled_at, str):
            raise ValueError("continuation settlement settled_at must be a str")
        pr_url = payload.get("pr_url")
        if pr_url is not None and not isinstance(pr_url, str):
            raise ValueError("continuation settlement pr_url must be a str or null")
        return cls(
            kind=parsed,
            settled_at=settled_at,
            pr_url=pr_url,
            schema_version=schema_version,
        )


def _require_text(value: object, field_name: str) -> str:
    """The non-empty string ``value`` is, or a refusal naming the field.

    Takes ``object`` rather than ``str`` so the type check is real work rather
    than a redundancy the checker elides: a settlement is also constructed from
    a stored payload, where the annotation is a claim about the record and not
    a guarantee about it.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"ContinuationSettlement.{field_name} must be a non-empty str"
        )
    return value


__all__ = [
    "CONTINUATION_SETTLEMENT_SCHEMA_VERSION",
    "ContinuationSettlement",
    "ContinuationSettlementKind",
]
