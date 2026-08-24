"""Operator-directed retirement of a claim whose run has ended (#245).

A fourth durable disposition beside the three
:mod:`.pending_work_claim_store` already documents, and it exists because none
of them could express what an operator actually had to say about one preserved
row:

* **deferred** says "waiting to be relaunched", which is the opposite of the
  instruction;
* **consumed** deletes, and the whole point is that the evidence survives;
* **parked** is a QUARANTINE's disposition. It is reached only for a claim
  nothing can read, and it is undone by the release of the quarantine that made
  it (``release_quarantine`` un-parks the row in the same transaction). A
  readable claim is never parked, and a retirement recorded through that bit
  would be silently revoked by an unrelated repair.

**retired** is therefore its own bit and its own evidence row: an operator, with
a named authority and a written reason, has decided that this exact ledger row's
work is abandoned. Nothing schedules it again, no release path undoes it, and
the payload it carried stays on disk to be read.

Three boundaries this deliberately does NOT cross:

* It answers the MECHANISM question only. Whether any particular queued work may
  be abandoned is a human decision recorded elsewhere, and the existence of this
  port is not evidence that one was taken.
* It is inert. Nothing in the scheduler, startup, or any sweep calls it; the
  only caller is an operator command run by hand.
* It is entirely LOCAL. Retirement commits to this machine's ledger and to
  nothing else. Telling anybody about it remotely is a separate, separately
  authorized act, and its failure cannot undo or retry what committed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.pending_work import PendingWorkKind


class ClaimRetirementRefusal(Enum):
    """Why a retirement did not happen — every member a NO-OP outcome.

    Retirement is irreversible by design, so every way of being unsure about
    which row is meant has to fail closed rather than pick one. These are the
    ways: they are refusals, not errors in the operator's shell usage, and each
    one names a different thing for a human to go and check.
    """

    #: No ledger row carries the addressed work identity at all.
    NO_SUCH_CLAIM = "no_such_claim"
    #: More than one row does. Which one was meant is exactly what is unknown,
    #: so nothing is chosen.
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    #: One row was found and it is NOT the claim the operator described. The
    #: address matched; the claim did not.
    IDENTITY_MISMATCH = "identity_mismatch"
    #: The row exists but this build cannot rebuild its payload, so it cannot
    #: confirm that the row is the one described. An unreadable claim already
    #: has a settlement of its own (#210) and must go through it.
    CLAIM_UNREADABLE = "claim_unreadable"
    #: A terminal disposition is already recorded for this row.
    ALREADY_RETIRED = "already_retired"
    #: A quarantine has settled this row where it lies (#210). Two authorities
    #: over one row is the ambiguity this whole boundary exists to avoid: the
    #: quarantine's release would un-park a row an operator had retired, and the
    #: retirement would outlive an escalation that has since been repaired.
    QUARANTINE_SETTLED = "quarantine_settled"


class ClaimRetirementRefused(RuntimeError):
    """A retirement request that changed nothing, and why.

    Carries the typed verdict as well as the sentence, so a caller can act on
    the refusal without reading the message — the same separation
    :class:`~.pending_work_claim_store.ClaimReadability` draws for a decode.
    """

    def __init__(self, refusal: ClaimRetirementRefusal, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class ClaimRetirementTarget:
    """The exact claim an operator means, addressed by the ledger's own identity.

    ``work_key`` is the address: it is what the ledger itself calls a piece of
    work (``PendingWorkClaim.work_key``), it is durable, and it needs no
    reconstruction of a dead run. It is deliberately not assumed unique — the
    column is indexed, not constrained — so more than one row carrying it is a
    refusal rather than a choice.

    The other three fields are not address, they are EXPECTATION. The operator
    states what they believe they are retiring, and a row that disagrees on any
    of them is not retired. ``flavor`` is required and ``None`` is one of its
    real values: "this kind has no variants" is a claim about the row that can
    be wrong, and letting it default would make the one distinction
    ``work_key`` cannot draw optional.
    """

    work_key: str
    issue_number: int
    work_kind: PendingWorkKind
    flavor: str | None

    def __post_init__(self) -> None:
        if not self.work_key.strip():
            raise ValueError("a retirement target needs the ledger's work key")


@dataclass(frozen=True, slots=True)
class ClaimRetirementRequest:
    """One operator's instruction to retire one claim, with its authority.

    ``authority`` and ``reason`` are required and non-empty because an
    unattributable retirement is the thing this port must not be able to
    express. ``recorded_at`` is supplied by the caller rather than read off a
    clock inside the store: the record is the operator's act, and the store's
    job is to commit it exactly, not to narrate it.
    """

    target: ClaimRetirementTarget
    reason: str
    authority: str
    recorded_at: str

    def __post_init__(self) -> None:
        for name, value in (
            ("reason", self.reason),
            ("authority", self.authority),
            ("recorded_at", self.recorded_at),
        ):
            if not value.strip():
                raise ValueError(f"a claim retirement needs a non-empty {name}")


@dataclass(frozen=True, slots=True)
class RetiredClaimRecord:
    """The durable evidence of one retirement — what was retired, and why.

    A copy of the payload lives here rather than only in the ledger row it
    settles. The row is the original and stays exactly where it was, but its
    lifetime is not this record's: ordinary claim operations key on
    ``work_key`` and ``run_key``, and a record of an act must not be reachable
    only through the state that act was performed on.

    ``run_key`` plus ``started_at`` is the identity, for the same reason a
    quarantine key is (#6999 F12): a run root can be reused by a replacement
    run, and a retirement must name the GENERATION it settled.
    """

    run_key: str
    started_at: str
    session_name: str
    issue_number: int
    work_key: str
    work_kind: PendingWorkKind
    flavor: str | None
    payload: str
    reason: str
    authority: str
    recorded_at: str


class PendingWorkClaimRetirementStore(Protocol):
    """The durable side of an operator's retirement decision."""

    def retire_claim(self, request: ClaimRetirementRequest) -> RetiredClaimRecord:
        """Retire the one claim ``request`` describes, or change nothing.

        Verification and mutation are ONE transaction. Every check in
        :class:`ClaimRetirementRefusal` is a refusal raised as
        :class:`ClaimRetirementRefused` with no row written and no bit set — a
        retirement that had partially committed on the way to discovering it was
        wrong would be exactly the DB surgery this replaces.

        Implementations must not touch anything remote. The record commits
        locally or not at all.
        """
        ...

    def rehearse_claim_retirement(
        self, request: ClaimRetirementRequest
    ) -> RetiredClaimRecord:
        """Answer :meth:`retire_claim` for ``request`` without committing it.

        Every check and every write the real call makes, rolled back instead of
        committed, so a rehearsal that succeeds is evidence about the real
        thing rather than about a second implementation of it. Retirement is
        irreversible; being able to find out that the described row is not the
        intended row, before the one-way door, is what keeps "fail closed" from
        depending on the operator's spelling.
        """
        ...

    def list_retired_claims(self) -> tuple[RetiredClaimRecord, ...]:
        """Every retirement this ledger has recorded, newest last.

        Inspection, not policy. It is what makes "the evidence survives" a
        checkable statement rather than an intention.
        """
        ...


__all__ = [
    "ClaimRetirementRefusal",
    "ClaimRetirementRefused",
    "ClaimRetirementRequest",
    "ClaimRetirementTarget",
    "PendingWorkClaimRetirementStore",
    "RetiredClaimRecord",
]
