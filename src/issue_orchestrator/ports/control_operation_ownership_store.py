"""Durable leases for terminal-less control operations (#146).

The row this port writes is *lease bookkeeping* and nothing else. It records
that a holder reserved one exact-candidate operation; it does NOT record that
the operation is still live. Liveness is supplied by the caller of
:class:`~..control.control_operation_ownership.ControlOperationOwnership`, and
a row nothing declares live is released rather than believed — otherwise a
crash after settlement would leave a durable lease that excludes ordinary work
forever.

Storage lives in the orchestrator-owned durable state directory as its own
typed concern, on the precedent of the publication-refusal latch (#51): its own
table, its own API, its own lifecycle. Explicitly not a ``PendingWorkKind``
(those rows mean a queue request was dequeued, and nothing was dequeued here)
and explicitly not a field on ``Attempt`` (that record is exact-candidate
evidence, not mutable runtime ownership).

Exception contract, as for the tech-lead run ledger: implementations MUST NOT
raise for a backing store they cannot reach or read. They return a typed
``UNAVAILABLE`` so the caller can tell "another holder has it" from "we could
not tell". Both fail closed for scheduling, but only one of them is a reason to
report a conflict to an operator, and a store that raised would make the two
indistinguishable at the one place that must not confuse them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.control_operation import ControlOperationKey


class ControlOperationReservationStatus(str, Enum):
    """What happened when a holder tried to reserve one operation."""

    #: No row existed; this holder now has one.
    GRANTED = "granted"
    #: A row written by THIS holder was already there — the restart case, and
    #: the idempotent re-claim case. Adopting rather than refusing is what lets
    #: a process that lost its in-memory lease re-establish ownership of an
    #: operation the caller still declares live, without a terminal to walk.
    ADOPTED = "adopted"
    #: Another holder reserved it first. Never silently admitted.
    HELD_BY_PEER = "held_by_peer"
    #: The store could not be read or written. Never means free.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ControlOperationReservation:
    """The answer to "may this holder own this operation?"."""

    status: ControlOperationReservationStatus
    holder: str = ""
    detail: str = ""

    @property
    def reserved(self) -> bool:
        return self.status in (
            ControlOperationReservationStatus.GRANTED,
            ControlOperationReservationStatus.ADOPTED,
        )


class ControlOperationReleaseStatus(str, Enum):
    """What happened when a holder tried to hand an operation back."""

    RELEASED = "released"
    #: This holder held nothing, so there was nothing to give back. Success.
    NOT_HELD = "not_held"
    #: The store could not be reached. The row may still be there, so the
    #: caller keeps its projection entry and retries on a later reconciliation.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ControlOperationRelease:
    """The answer to "is this operation definitively handed back?"."""

    status: ControlOperationReleaseStatus
    holder: str = ""
    detail: str = ""

    @property
    def settled(self) -> bool:
        """True only when nothing of this holder's is left in the store."""
        return self.status is not ControlOperationReleaseStatus.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class ControlOperationOwnershipRow:
    """One durable lease, as read back from the store."""

    key: ControlOperationKey
    holder: str


class ControlOperationReadStatus(str, Enum):
    """Whether the durable leases could be read at all."""

    READABLE = "readable"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ControlOperationOwnershipRead:
    """Every durable lease, or a typed refusal to guess.

    An empty tuple and an unreadable store are different facts and must never
    be spelled the same way: the first means no operation is reserved, the
    second means we do not know, and only the first may let ordinary work
    start.
    """

    status: ControlOperationReadStatus
    rows: tuple[ControlOperationOwnershipRow, ...] = ()
    detail: str = ""

    @property
    def readable(self) -> bool:
        return self.status is ControlOperationReadStatus.READABLE


class ControlOperationOwnershipStore(Protocol):
    """Atomic reservation of exact-candidate control operations."""

    def reserve_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationReservation:
        """Reserve ``key`` for ``holder``, atomically.

        Exactly one holder may win a contested operation; the loser is told who
        won. A row this holder already wrote is ``ADOPTED``, not refused.
        """
        ...

    def release_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationRelease:
        """Drop ``holder``'s reservation of ``key``.

        Never removes another holder's row: a release that found someone else's
        reservation reports ``NOT_HELD`` and leaves it alone.
        """
        ...

    def list_control_operation_ownership(self) -> ControlOperationOwnershipRead:
        """Every reservation in the store, or a typed unavailable.

        Reconciliation needs the rows a PREVIOUS process wrote — after a
        restart there is no in-memory lease to enumerate, and a settled
        operation's surviving row must still be found so it can be released.
        """
        ...


__all__ = [
    "ControlOperationOwnershipRead",
    "ControlOperationOwnershipRow",
    "ControlOperationOwnershipStore",
    "ControlOperationReadStatus",
    "ControlOperationRelease",
    "ControlOperationReleaseStatus",
    "ControlOperationReservation",
    "ControlOperationReservationStatus",
]
