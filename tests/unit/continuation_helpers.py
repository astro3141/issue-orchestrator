"""Shared doubles for the production control continuation (#149).

Most suites that construct a hydration owner — the startup manager, the queue
projection, the planning cycle — are not about the continuation at all. They
still need a real one, because the ordering it enforces is now part of how the
queue is hydrated, and handing them a mock would let the ordering regress
without a single test noticing.

So :func:`inert_control_continuation` builds the REAL stack over an empty
durable record: it derives no live operation, reconciles to an empty
projection, and excludes nothing. Every collaborator is the production type
except the two storage ports, which are in-memory implementations of the
protocols rather than mocks — an assertion about exclusion still exercises the
real reconciliation path.
"""

from __future__ import annotations

from issue_orchestrator.control.continuation_in_flight import ContinuationsInFlight
from issue_orchestrator.control.continuation_rework_handoff import (
    ContinuationReworkHandoff,
)
from issue_orchestrator.control.continuation_runs import ContinuationRuns
from issue_orchestrator.control.continuation_live_truth import ContinuationLiveTruth
from issue_orchestrator.control.continuation_scheduling import ControlContinuation
from issue_orchestrator.control.control_operation_ownership import (
    ControlOperationOwnership,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.rework_cycle_policy import ReworkCycleBudget
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.control_operation import ControlOperationKey
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.control_operation_ownership_store import (
    ControlOperationOwnershipRead,
    ControlOperationOwnershipRow,
    ControlOperationReadStatus,
    ControlOperationRelease,
    ControlOperationReleaseStatus,
    ControlOperationReservation,
    ControlOperationReservationStatus,
)

PR_PENDING_LABEL = "pr-pending"


class InMemoryControlOperationOwnershipStore:
    """``ControlOperationOwnershipStore`` backed by a dict.

    Keyed by the same ``durable_parts`` tuple the sqlite store uses as its
    composite primary key, so a reservation filed for one exact candidate can
    never be read back for another.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str, str], str] = {}

    def reserve_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationReservation:
        recorded = self._rows.setdefault(key.durable_parts, holder)
        if recorded != holder:
            return ControlOperationReservation(
                status=ControlOperationReservationStatus.HELD_BY_PEER,
                holder=recorded,
                detail="reserved by another holder",
            )
        return ControlOperationReservation(
            status=ControlOperationReservationStatus.GRANTED,
            holder=holder,
            detail="reserved",
        )

    def release_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationRelease:
        recorded = self._rows.get(key.durable_parts)
        if recorded is None:
            return ControlOperationRelease(
                status=ControlOperationReleaseStatus.NOT_HELD, holder="", detail="no row"
            )
        if recorded != holder:
            return ControlOperationRelease(
                status=ControlOperationReleaseStatus.NOT_HELD,
                holder=recorded,
                detail="row belongs to another holder",
            )
        del self._rows[key.durable_parts]
        return ControlOperationRelease(
            status=ControlOperationReleaseStatus.RELEASED,
            holder=holder,
            detail="released",
        )

    def list_control_operation_ownership(self) -> ControlOperationOwnershipRead:
        return ControlOperationOwnershipRead(
            status=ControlOperationReadStatus.READABLE,
            rows=tuple(
                ControlOperationOwnershipRow(key=_key(parts), holder=holder)
                for parts, holder in sorted(self._rows.items())
            ),
            detail="read",
        )


class NoAttempts:
    """An ``AttemptStore`` that holds nothing and refuses every write.

    Refusing rather than accepting: a suite that is not about the continuation
    should never be writing continuation records, and a silent accept would
    hide it if one started.
    """

    def for_key(self, key: object) -> None:
        return None

    def update(self, key: object, mutate: object) -> object:
        raise AssertionError("this suite must not write attempt records")

    def for_issue(self, issue_key: object) -> tuple[()]:
        return ()

    def supersede_issue(self, issue_key: object) -> int:
        return 0


class NoWorktrees:
    """A worktree manager for a composition that never opens a run.

    Nothing here derives a live operation, so no run is ever opened and none can
    be closed. Refusing rather than passing keeps it that way: a suite that
    started materialising checkouts through the inert stack would say so.
    """

    def remove_checkout(self, worktree_path: object, *, force: bool = False) -> None:
        raise AssertionError("this suite must not close continuation runs")


class NoContinuationRunner:
    """A runner with nothing to advance, because nothing is ever live here."""

    def advance(self, reconciliation: object) -> None:
        return None


class NoPullRequests:
    """A PR reader for a composition that derives no rework exit.

    Refusing rather than answering "none": the inert stack holds no attempt at
    all, so no exit can be derived and the handoff can never reach a PR read. A
    suite that started reaching one would say so instead of quietly passing.
    """

    def create_pr(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("this suite must not create pull requests")

    def get_prs_for_issue(
        self, issue_number: int, state: str = "open"
    ) -> list[object]:
        raise AssertionError("this suite must not read pull requests")

    def get_prs_for_branch(
        self, branch: str, state: str = "open"
    ) -> list[object]:
        raise AssertionError("this suite must not read pull requests")


def inert_control_continuation(
    state: OrchestratorState | None = None,
) -> ControlContinuation:
    """The real continuation stack over an empty durable record."""
    engine_state = state if state is not None else OrchestratorState()
    return ControlContinuation(
        ControlOperationOwnership(
            engine_state,
            InMemoryControlOperationOwnershipStore(),
        ),
        ContinuationLiveTruth(
            NoAttempts(),  # type: ignore[arg-type]
            pr_pending_label=PR_PENDING_LABEL,
            in_flight=ContinuationsInFlight(),
            runs=ContinuationRuns(NoWorktrees()),  # type: ignore[arg-type]
        ),
        NoContinuationRunner(),  # type: ignore[arg-type]
        ContinuationReworkHandoff(
            state=engine_state,
            pull_requests=NoPullRequests(),  # type: ignore[arg-type]
            budget=ReworkCycleBudget(
                LabelManager(Config()), max_rework_cycles=Config().max_rework_cycles
            ),
            events=NullEventSink(),
        ),
    )


def _key(parts: tuple[str, str, str, str]) -> ControlOperationKey:
    from issue_orchestrator.domain.attempt import StoredIssueKey
    from issue_orchestrator.domain.control_operation import ControlOperationKind

    scope, stable_id, head_sha, kind = parts
    return ControlOperationKey(
        StoredIssueKey(stable_id, scope), head_sha, ControlOperationKind(kind)
    )


__all__ = [
    "InMemoryControlOperationOwnershipStore",
    "NoAttempts",
    "NoContinuationRunner",
    "NoPullRequests",
    "NoWorktrees",
    "inert_control_continuation",
]
