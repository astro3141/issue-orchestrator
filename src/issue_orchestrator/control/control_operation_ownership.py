"""Ownership of control operations that have no terminal and no queue row (#146).

The front half of ``revalidation -> first Reviewer -> PR/rework settlement`` is
work the orchestrator does about one exact candidate with nothing running: no
agent terminal, no dequeued queue request, no issue session. Every existing
owner reserves work by pointing at one of those, so none of them can reserve
this. This one can, and it reserves nothing else.

The lifecycle is :class:`~.tech_lead_run_ownership.TechLeadRunOwnership`'s,
with its Tech Lead-specific policy deliberately left behind — no global
barrier, no ``PROMOTE``, no scope conflict matrix, because an issue-scoped
control operation conflicts with exactly one thing: ordinary work on its own
issue. What IS borrowed is the separation that makes that owner correct::

    caller-owned live operation truth
            -> reconcile(live_operations)
            -> owned/contended/unavailable projection
            -> scheduler exclusion

The ownership layer *consumes* live-operation truth; it never manufactures it.
A durable lease row is bookkeeping, not proof: it says a holder reserved an
operation, not that the operation is still running. If a row could vouch for
itself, a crash after settlement would turn a durable lease into a durable
deadlock, and the only exit would be editing durable state by hand.

The production source of ``live_operations`` is
:class:`~.continuation_live_truth.ContinuationLiveTruth` (#149), which derives
it from the durable descriptor and the exact ``Attempt`` evaluation state. This
owner still consumes an explicit typed live-operation collection and knows
nothing about how one is derived; :meth:`ControlOperationOwnership.reconcile_derived`
is the seam where the two meet, and the lock it holds is what discharges
:meth:`ControlOperationOwnership.reconcile`'s ordering precondition.

**No authority inflation.** Claiming, reconciling and releasing change no
label, no evaluation history, no completion intent, and neither
``PublicationAuthority`` nor ``review_validity``. Ownership prevents
conflicting execution and nothing else; a reader consulting the projection may
only become more conservative.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Collection, Iterable

from ..domain.control_operation import (
    ControlOperationExclusions,
    ControlOperationKey,
    ControlOperationOwnershipEntry,
    ControlOperationOwnershipStatus,
)
from ..ports.control_operation_ownership_store import (
    ControlOperationRelease,
    ControlOperationReleaseStatus,
    ControlOperationReservationStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.models import OrchestratorState
    from ..ports.control_operation_ownership_store import (
        ControlOperationOwnershipStore,
    )

logger = logging.getLogger(__name__)

DEFAULT_CONTROL_OPERATION_HOLDER = "single-instance"
"""The holder name for a deployment with no peer engines.

Stable across restarts on purpose, and for the same reason
:class:`~...ports.run_ledger_store.SingleInstanceRunLedgerStore` names its
claimant rather than deriving one: the durable store is the orchestrator-owned
per-repository state directory, which one engine holds at a time. A row this
engine wrote before a crash must be adoptable by its successor, and a holder
derived from the process (a pid, a boot id) would make the engine's own
pre-crash row read as a peer's and strand it for the life of the lease.

A deployment that really does run named peers over one state directory passes
its own holder and gets typed contention between them.
"""


@dataclass(frozen=True, slots=True)
class _ReservationOutcome:
    """What one store reservation verdict projects, and how loudly it reports.

    Both answers sit on the same row so that adding a store verdict cannot give
    it a projection without also declaring its visibility, and so neither answer
    is re-derived by a branch at a call site.
    """

    projected: ControlOperationOwnershipStatus
    log_level: int


# Every store verdict maps to exactly one outcome, declared once rather than
# re-derived by a branch chain per call site. A new store verdict fails loudly
# at the lookup instead of falling into whichever branch was last.
_RESERVATION_OUTCOMES: dict[
    ControlOperationReservationStatus, _ReservationOutcome
] = {
    ControlOperationReservationStatus.GRANTED: _ReservationOutcome(
        ControlOperationOwnershipStatus.OWNED, logging.DEBUG
    ),
    ControlOperationReservationStatus.ADOPTED: _ReservationOutcome(
        ControlOperationOwnershipStatus.OWNED, logging.DEBUG
    ),
    # Never OWNED and never dropped: another holder is running this operation,
    # so ordinary work on the issue stays excluded even though we may not act.
    ControlOperationReservationStatus.HELD_BY_PEER: _ReservationOutcome(
        ControlOperationOwnershipStatus.CONTENDED, logging.WARNING
    ),
    ControlOperationReservationStatus.UNAVAILABLE: _ReservationOutcome(
        ControlOperationOwnershipStatus.UNAVAILABLE, logging.WARNING
    ),
}


class _ReleaseVerdict(Enum):
    """What a store release proved about THIS holder's exclusion.

    The store answers a narrower question than the owner asks. It reports what
    happened to a row; the owner needs to know whether the exclusion that row
    backed is now ours to drop, and those are not the same question whenever a
    peer's reservation is what the store found.
    """

    #: Nothing of this holder's is left in the store, so the exclusion goes.
    FREED = "freed"
    #: Someone else's reservation stands. Dropping the exclusion here would
    #: turn "another holder is running this operation" into "nothing is", the
    #: one direction a reader of the projection may never be moved.
    HELD_ELSEWHERE = "held elsewhere"
    #: The store could not say. Ignorance keeps the exclusion; only a
    #: reconciliation against live truth may remove it.
    UNKNOWN = "unknown"


# What each store release verdict proves, BEFORE the recorded holder is
# consulted. ``NOT_HELD`` is the load-bearing row: it is a settled answer, and
# reading settlement as "freed" without asking whose row the store found is
# exactly how a loser's ordinary unwind deletes the winner's exclusion.
_RELEASE_VERDICTS: dict[ControlOperationReleaseStatus, _ReleaseVerdict] = {
    ControlOperationReleaseStatus.RELEASED: _ReleaseVerdict.FREED,
    ControlOperationReleaseStatus.NOT_HELD: _ReleaseVerdict.FREED,
    ControlOperationReleaseStatus.UNAVAILABLE: _ReleaseVerdict.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class _ReleaseReport:
    """How one release verdict is reported to an operator."""

    log_level: int
    explanation: str


# What each release verdict means for the holder that asked, declared once.
# Only FREED frees anything; the other two keep the exclusion for different
# reasons, and an operator needs to be able to tell those reasons apart.
_RELEASE_REPORTS: dict[_ReleaseVerdict, _ReleaseReport] = {
    _ReleaseVerdict.FREED: _ReleaseReport(
        logging.INFO, "nothing of this holder's is left in the store"
    ),
    _ReleaseVerdict.HELD_ELSEWHERE: _ReleaseReport(
        logging.WARNING,
        "the reservation belongs to another holder; keeping the exclusion"
        " rather than freeing their operation",
    ),
    _ReleaseVerdict.UNKNOWN: _ReleaseReport(
        logging.WARNING,
        "the store could not be reached; keeping the exclusion so a later"
        " reconciliation asks again",
    ),
}


@dataclass(frozen=True, slots=True)
class _HandBack:
    """One store release, and what it proved about this holder's exclusion."""

    release: ControlOperationRelease
    verdict: _ReleaseVerdict

    @property
    def freed(self) -> bool:
        return self.verdict is _ReleaseVerdict.FREED


def _ordered(keys: Iterable[ControlOperationKey]) -> list[ControlOperationKey]:
    """Keys in one deterministic order, by their own durable spelling."""
    return sorted(keys, key=lambda key: key.durable_parts)


class ControlOperationOwnership:
    """This engine's set of reserved control operations.

    Holds the reconciled projection on ``OrchestratorState`` for the same
    reason :class:`~.in_flight_work.InFlightWorkLedger` holds its claims there:
    the durable store is the authoritative record, and the state field is the
    in-process view every reader consults. The field is written HERE and
    nowhere else, so a scheduler cannot be excluded by anything that did not
    come through reconciliation.
    """

    def __init__(
        self,
        state: "OrchestratorState",
        store: "ControlOperationOwnershipStore",
        *,
        holder: str = DEFAULT_CONTROL_OPERATION_HOLDER,
    ) -> None:
        self._state = state
        self._store = store
        self._holder = holder
        # Reentrant: ``reconcile`` both reserves and releases, and the whole
        # sequence must be one transaction against the store AND the published
        # projection. Completions run off the tick thread, so an unsynchronised
        # projection is two threads disagreeing about what this engine owns.
        self._lock = threading.RLock()

    @property
    def holder(self) -> str:
        """The name this engine's reservations are recorded under."""
        return self._holder

    @property
    def exclusions(self) -> ControlOperationExclusions:
        """The projection standing right now, whatever last published it.

        A read, never a derivation: a caller that could not reconcile needs the
        answer already in force rather than a fresh one it has no evidence for.
        The value is frozen and replaced wholesale, so this hands back one
        complete reconciliation rather than a view that can change underneath.
        """
        with self._lock:
            return self._projection()

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def claim(self, key: ControlOperationKey) -> ControlOperationOwnershipEntry:
        """Atomically reserve ``key``, with no terminal and no queue request.

        The caller claims an operation it has just declared live, so the result
        joins the projection immediately rather than waiting for the next
        reconciliation — the window between the two is exactly where a
        conflicting Actor launch would land. Idempotent for an operation this
        engine already owns.
        """
        with self._lock:
            current = self._projection().entry_for(key)
            if current is not None and current.owned:
                return current
            entry = self._reserve(key)
            self._publish(self._with_entry(entry))
            return entry

    def owns(self, key: ControlOperationKey) -> bool:
        """Whether this engine holds ``key`` in the reconciled projection."""
        with self._lock:
            return self._projection().owns(key)

    def release(self, key: ControlOperationKey) -> ControlOperationRelease:
        """Hand ``key`` back because THIS engine's operation settled.

        Only an exclusion this holder put there may be dropped. A release that
        gave nothing back is not a reason to free the issue: after a
        ``CONTENDED`` claim the ordinary ``try/finally`` unwind reaches here,
        the store answers ``NOT_HELD`` because the row belongs to a peer, and
        dropping the entry on that would convert "another holder is running
        this operation" into "nothing is running it" — the one direction a
        reader of the projection may never move. :meth:`_hand_back` is the
        single place that rule is decided, and the store's own ``holder`` is
        what decides it.

        Typed rather than silent: the store reports an unreachable backend as
        ``UNAVAILABLE`` instead of raising, and a caller that inferred success
        from "no exception" would report a clean settlement while the row was
        still there. On ``UNAVAILABLE`` the projection entry is KEPT, so the
        exclusion survives until a reconciliation can retry the release.
        """
        with self._lock:
            hand_back = self._hand_back(key, because="this holder's operation settled")
            if hand_back.freed:
                self._publish(self._without(key))
            return hand_back.release

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile_derived(
        self,
        derive: Callable[[], Collection[ControlOperationKey]],
    ) -> ControlOperationExclusions:
        """Derive the live set and reconcile against it, as one transaction.

        The discharge of :meth:`reconcile`'s ordering precondition, offered
        here rather than left to each caller to arrange. ``derive`` runs while
        this owner's lock is held, and :meth:`claim` takes the same lock, so
        the falsifying interleaving has nowhere to happen:

        * a claim that lands BEFORE the lock is granted has already written the
          durable fact ``derive`` reads, so the operation is named live and its
          lease is kept;
        * a claim that lands AFTER cannot begin until this reconciliation has
          published, and :meth:`claim` adds its own entry to the projection
          immediately rather than waiting for the next pass.

        No generation counter, no snapshot version, and no new subsystem: the
        serialisation is the lock the owner already holds, which is exactly
        what "serialising the two against the caller's own live-operation
        truth" means.

        ``derive`` must be a pure read of durable truth. It runs under a lock
        that completions also take, so work inside it delays them.
        """
        with self._lock:
            return self.reconcile(derive())

    def reconcile(
        self, live_operations: Collection[ControlOperationKey]
    ) -> ControlOperationExclusions:
        """Align durable leases with the operations the CALLER says are live.

        The caller owns what is live; this owns what that implies for the
        leases and for scheduling. Three things happen, in this order:

        * a lease of ours whose operation is no longer live is RELEASED — the
          stale-restart case, and the reason a surviving row can never exclude
          ordinary work forever;
        * a live operation whose lease we already hold is kept, including one
          this process never claimed because it was written before a restart:
          that is the adoption, and it needs no terminal and no session walk;
        * a live operation with no lease of ours is reserved, which either
          grants it or reports another holder.

        Returns the projection it published, so a caller can act on the typed
        result without re-reading shared state.

        **Ordering precondition.** ``live_operations`` is derived outside this
        owner's lock, while :meth:`claim` is reachable off the tick thread, so
        the caller must derive the live set no EARLIER than the claims it is
        meant to cover. A set derived before a claim does not name that
        operation, and the first bullet above would then release a lease whose
        operation is still running, leaving it unowned. Deriving the live set
        after the claims it should cover — or serialising the two against the
        caller's own live-operation truth — is what makes that impossible.
        """
        live = set(live_operations)
        with self._lock:
            read = self._store.list_control_operation_ownership()
            if not read.readable:
                # Fails closed in both directions: every live operation, and
                # everything already excluded, reads UNAVAILABLE rather than
                # free. An outage is ignorance, never evidence that an
                # operation stopped.
                logger.warning(
                    "[CONTROL_OP] Could not read control-operation ownership"
                    " (%s); holding every known exclusion closed",
                    read.detail,
                )
                return self._publish(
                    ControlOperationExclusions(
                        tuple(
                            ControlOperationOwnershipEntry(
                                key,
                                ControlOperationOwnershipStatus.UNAVAILABLE,
                                detail=read.detail,
                            )
                            for key in _ordered(live | self._projection_keys())
                        )
                    )
                )
            ours = {row.key for row in read.rows if row.holder == self._holder}
            for key in _ordered(ours - live):
                self._release_stale(key)
            entries = tuple(
                self._reconcile_one(key, held=key in ours) for key in _ordered(live)
            )
            return self._publish(ControlOperationExclusions(entries))

    def _reconcile_one(
        self, key: ControlOperationKey, *, held: bool
    ) -> ControlOperationOwnershipEntry:
        if held:
            # The durable row already names this holder, so there is nothing to
            # write: the read that found it IS the adoption after a restart.
            return ControlOperationOwnershipEntry(
                key, ControlOperationOwnershipStatus.OWNED, holder=self._holder
            )
        return self._reserve(key)

    def _release_stale(self, key: ControlOperationKey) -> None:
        """Give back a lease whose operation nothing declares live any more.

        The projection this reconciliation publishes is built from ``live``
        alone, so a lease the store could not drop stops excluding the issue
        either way; the row is retried on the next pass rather than stranded.
        """
        self._hand_back(key, because="no live operation claims it")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _hand_back(self, key: ControlOperationKey, *, because: str) -> _HandBack:
        """Ask the store to drop this holder's row, and say what that proved.

        The one place a store release is turned into an answer about THIS
        holder's exclusion, so both callers get the same rule: a settled
        verdict frees nothing unless the row the store found was ours to give
        up. The store's own recorded ``holder`` decides that — not the
        projection's, which is exactly what a fail-closed entry does not know.
        """
        release = self._store.release_control_operation(key, holder=self._holder)
        ours = release.holder in ("", self._holder)
        verdict = (
            _RELEASE_VERDICTS[release.status] if ours else _ReleaseVerdict.HELD_ELSEWHERE
        )
        report = _RELEASE_REPORTS[verdict]
        logger.log(
            report.log_level,
            "[CONTROL_OP] Release of %s (%s): %s (holder=%s) %s",
            key,
            because,
            report.explanation,
            release.holder or "unrecorded",
            release.detail,
        )
        return _HandBack(release, verdict)

    def _reserve(self, key: ControlOperationKey) -> ControlOperationOwnershipEntry:
        reservation = self._store.reserve_control_operation(key, holder=self._holder)
        outcome = _RESERVATION_OUTCOMES[reservation.status]
        logger.log(
            outcome.log_level,
            "[CONTROL_OP] %s is %s (holder=%s): %s",
            key,
            outcome.projected.value,
            reservation.holder or "unknown",
            reservation.detail,
        )
        return ControlOperationOwnershipEntry(
            key,
            outcome.projected,
            holder=reservation.holder or (self._holder if reservation.reserved else ""),
            detail=reservation.detail,
        )

    def _projection(self) -> ControlOperationExclusions:
        return self._state.control_operation_exclusions

    def _projection_keys(self) -> set[ControlOperationKey]:
        return {entry.key for entry in self._projection().entries}

    def _with_entry(
        self, entry: ControlOperationOwnershipEntry
    ) -> ControlOperationExclusions:
        kept = tuple(e for e in self._projection().entries if e.key != entry.key)
        return ControlOperationExclusions(
            tuple(sorted((*kept, entry), key=lambda e: e.key.durable_parts))
        )

    def _without(self, key: ControlOperationKey) -> ControlOperationExclusions:
        return ControlOperationExclusions(
            tuple(e for e in self._projection().entries if e.key != key)
        )

    def _publish(
        self, exclusions: ControlOperationExclusions
    ) -> ControlOperationExclusions:
        """Replace the projection wholesale; the only writer of that field."""
        self._state.control_operation_exclusions = exclusions
        return exclusions


__all__ = [
    "DEFAULT_CONTROL_OPERATION_HOLDER",
    "ControlOperationOwnership",
]
