"""Durable ownership for terminal-less control operations (#146).

Twelve failure directions, one per behaviour the primitive exists to hold. They
are grouped below in the order the issue states them, and each class names the
direction it proves.

Everything durable here is the REAL orchestrator-owned ledger over a temporary
directory, and every "restart" is a second store handle plus a fresh
``OrchestratorState`` over the same file — exactly what a new process gets. An
in-process-only reservation passes none of the restart tests. The only double
is the unreadable store, which is mocked at the port boundary because a genuine
sqlite outage cannot be produced deterministically.

No test constructs a Session, a terminal, or a queue request: the whole point
of this owner is that none of those exist while it holds an operation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from issue_orchestrator.control.control_operation_ownership import (
    ControlOperationOwnership,
)
from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.control_operation import (
    ControlOperationKey,
    ControlOperationKind,
    ControlOperationOwnershipStatus,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.execution.control_operation_ownership_store import (
    SqliteControlOperationOwnershipStore,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    STORE_FILENAME,
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.control_operation_ownership_store import (
    ControlOperationOwnershipRead,
    ControlOperationReadStatus,
    ControlOperationRelease,
    ControlOperationReleaseStatus,
    ControlOperationReservation,
    ControlOperationReservationStatus,
)

REPO = "owner/repo"
ISSUE_NUMBER = 146
# Two evaluations of the same issue at two commits: A and A'.
SHA_A = "1" * 40
SHA_A_PRIME = "2" * 40
CONTINUATION = ControlOperationKind.PUBLICATION_REVALIDATION_CONTINUATION


def _ledger(tmp_path: Path) -> SqliteControlOperationOwnershipStore:
    """A fresh handle on the lease store, as a new process gets."""
    return SqliteControlOperationOwnershipStore(tmp_path / STORE_FILENAME)


def _claims(tmp_path: Path) -> SqlitePendingWorkClaimStore:
    """The pending-work ledger sharing that database, for the rows it owns."""
    return SqlitePendingWorkClaimStore(tmp_path / STORE_FILENAME)


def _key(head_sha: str = SHA_A, issue: str = str(ISSUE_NUMBER)) -> ControlOperationKey:
    return ControlOperationKey(
        GitHubIssueKey(repo=REPO, external_id=issue), head_sha, CONTINUATION
    )


def _config() -> Config:
    config = Config(
        repo=REPO,
        repo_root=Path("/tmp/repo"),
        worktree_base=Path("/tmp/worktrees"),
        agents={"agent:backend": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
    )
    config.filtering.label = "agent:backend"
    return config


def _issue(number: int = ISSUE_NUMBER) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        labels=["agent:backend"],
        repo=REPO,
    )


def _eligibility(state: OrchestratorState, number: int = ISSUE_NUMBER):
    """What ordinary Actor/rework scheduling makes of this issue right now."""
    return QueueCache(_config(), state).evaluate_issue(_issue(number))


class UnreadableOwnershipStore:
    """A store that can neither be read nor written.

    Returns the port's typed ``UNAVAILABLE`` rather than raising, exactly as
    the contract requires, so the tests below check what the OWNER does with
    ignorance rather than what an exception does to it.
    """

    def reserve_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationReservation:
        return ControlOperationReservation(
            ControlOperationReservationStatus.UNAVAILABLE, detail="disk on fire"
        )

    def release_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationRelease:
        return ControlOperationRelease(
            ControlOperationReleaseStatus.UNAVAILABLE, detail="disk on fire"
        )

    def list_control_operation_ownership(self) -> ControlOperationOwnershipRead:
        return ControlOperationOwnershipRead(
            ControlOperationReadStatus.UNAVAILABLE, detail="disk on fire"
        )


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState()


class TestTerminalLessAcquisition:
    """Direction 1: ownership exists with zero sessions and zero terminals."""

    def test_an_operation_is_owned_with_nothing_running(self, tmp_path, state):
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))
        key = _key()

        entry = ownership.claim(key)

        assert entry.status is ControlOperationOwnershipStatus.OWNED
        assert ownership.owns(key)
        # Nothing was invented to make that possible.
        assert state.active_sessions == []
        assert state.session_history == []
        assert state.in_flight_work == []

    def test_a_second_claim_of_the_same_operation_is_idempotent(self, tmp_path, state):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)
        key = _key()

        ownership.claim(key)
        second = ownership.claim(key)

        assert second.status is ControlOperationOwnershipStatus.OWNED
        assert len(ledger.list_control_operation_ownership().rows) == 1
        assert len(state.control_operation_exclusions.entries) == 1


class TestQueueLessTruthfulness:
    """Direction 2: nothing reports that a pending request was dequeued."""

    def test_claiming_drains_no_queue_and_records_no_pending_work(
        self, tmp_path, state
    ):
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))

        ownership.claim(_key())
        ownership.reconcile([_key()])

        # The pending-work ledger still means "a queue request was dequeued",
        # and none was — including in the database the leases share with it.
        claims = _claims(tmp_path)
        assert claims.list_unresolved_claims() == ()
        assert claims.list_unreadable_claims() == ()
        assert state.in_flight_work == []
        assert state.pending_reviews == []
        assert state.pending_reworks == []
        assert state.pending_validation_retries == []


class TestTruthAndOwnershipAreSeparate:
    """Direction 3: a lease row can never be its own proof of liveness."""

    def test_a_row_absent_from_live_operations_is_released(self, tmp_path, state):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)
        ownership.claim(_key())

        projection = ownership.reconcile([])

        assert projection.entries == ()
        assert ledger.list_control_operation_ownership().rows == ()
        assert not ownership.owns(_key())

    def test_a_row_alone_excludes_nothing(self, tmp_path, state):
        """The row exists; no caller says the operation is live."""
        ledger = _ledger(tmp_path)
        ledger.reserve_control_operation(_key(), holder="single-instance")

        assert _eligibility(state) is QueueMutationStatus.ACCEPTED
        assert state.control_operation_exclusions.entries == ()


class TestRestartWithZeroTerminals:
    """Direction 4: a still-live operation is adopted with no session walk."""

    def test_a_live_operation_is_re_established_after_a_restart(self, tmp_path):
        before = OrchestratorState()
        ControlOperationOwnership(before, _ledger(tmp_path)).claim(_key())

        after = OrchestratorState()
        ownership = ControlOperationOwnership(after, _ledger(tmp_path))
        projection = ownership.reconcile([_key()])

        assert ownership.owns(_key())
        assert projection.owned == (_key(),)
        # Adoption came from the independently supplied live-operation set, not
        # from anything session-shaped: there is nothing session-shaped here.
        assert after.active_sessions == []
        assert after.session_history == []
        assert _eligibility(after) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_the_projection_is_empty_until_reconciliation_runs(self, tmp_path):
        ControlOperationOwnership(OrchestratorState(), _ledger(tmp_path)).claim(_key())

        after = OrchestratorState()
        ControlOperationOwnership(after, _ledger(tmp_path))

        assert after.control_operation_exclusions.entries == ()
        assert _eligibility(after) is QueueMutationStatus.ACCEPTED


class TestStaleRestart:
    """Direction 5: a settled operation's surviving row cannot block forever."""

    def test_a_row_no_live_operation_claims_is_dropped_after_a_restart(self, tmp_path):
        ControlOperationOwnership(OrchestratorState(), _ledger(tmp_path)).claim(_key())

        after = OrchestratorState()
        ledger = _ledger(tmp_path)
        projection = ControlOperationOwnership(after, ledger).reconcile([])

        assert projection.entries == ()
        assert ledger.list_control_operation_ownership().rows == ()
        assert _eligibility(after) is QueueMutationStatus.ACCEPTED


class TestUnreadableStoreFailsClosed:
    """Direction 6: ignorance never reads as free-to-launch."""

    def test_a_live_operation_is_excluded_when_ownership_cannot_be_read(self, state):
        ownership = ControlOperationOwnership(state, UnreadableOwnershipStore())

        projection = ownership.reconcile([_key()])

        assert projection.unavailable == (_key(),)
        assert not ownership.owns(_key())
        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_a_claim_that_cannot_be_written_is_not_ownership(self, state):
        ownership = ControlOperationOwnership(state, UnreadableOwnershipStore())

        entry = ownership.claim(_key())

        assert entry.status is ControlOperationOwnershipStatus.UNAVAILABLE
        assert not ownership.owns(_key())
        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_an_outage_does_not_drop_an_exclusion_already_held(self, tmp_path, state):
        ControlOperationOwnership(state, _ledger(tmp_path)).claim(_key())

        # The store goes away, and the caller no longer names the operation.
        projection = ControlOperationOwnership(
            state, UnreadableOwnershipStore()
        ).reconcile([])

        assert projection.unavailable == (_key(),)
        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_a_release_that_did_not_commit_keeps_the_exclusion(self, tmp_path, state):
        ControlOperationOwnership(state, _ledger(tmp_path)).claim(_key())

        release = ControlOperationOwnership(
            state, UnreadableOwnershipStore()
        ).release(_key())

        assert release.status is ControlOperationReleaseStatus.UNAVAILABLE
        assert not release.settled
        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_a_recovered_store_does_not_let_a_failed_claim_free_a_peer(self, tmp_path):
        """A release that gave nothing back cannot reopen a fail-closed entry.

        The claim could not be written, so the exclusion is ``UNAVAILABLE``. By
        the time the caller unwinds the store is back — and it reports the
        operation as another holder's. Ignorance may only be resolved by a
        reconciliation against live truth, never by a release of ours that
        released nothing.
        """
        ControlOperationOwnership(
            OrchestratorState(), _ledger(tmp_path), holder="engine-a"
        ).claim(_key())

        loser_state = OrchestratorState()
        ControlOperationOwnership(
            loser_state, UnreadableOwnershipStore(), holder="engine-b"
        ).claim(_key())
        release = ControlOperationOwnership(
            loser_state, _ledger(tmp_path), holder="engine-b"
        ).release(_key())

        assert release.status is ControlOperationReleaseStatus.NOT_HELD
        assert release.holder == "engine-a"
        assert loser_state.control_operation_exclusions.unavailable == (_key(),)
        assert _eligibility(loser_state) is QueueMutationStatus.REJECTED_EXCLUDED


class TestTheDurableLeaseSurface:
    """Direction 6 again, at the real store rather than at a double.

    A lease row this store cannot decode is corruption — nothing else writes
    that table — and the one answer it must never give is "no rows".
    """

    @staticmethod
    def _corrupt_every_row(tmp_path: Path) -> None:
        """Rewrite the stored kind to something no enum member matches."""
        conn = sqlite3.connect(tmp_path / STORE_FILENAME)
        try:
            conn.execute("UPDATE control_operation_ownership SET kind = 'nonsense'")
            conn.commit()
        finally:
            conn.close()

    def test_an_undecodable_row_reads_as_unavailable_not_empty(self, tmp_path):
        _ledger(tmp_path).reserve_control_operation(_key(), holder="single-instance")

        self._corrupt_every_row(tmp_path)
        read = _ledger(tmp_path).list_control_operation_ownership()

        assert read.status is ControlOperationReadStatus.UNAVAILABLE
        assert read.rows == ()

    def test_an_undecodable_row_never_frees_a_live_operation(self, tmp_path):
        _ledger(tmp_path).reserve_control_operation(_key(), holder="single-instance")
        self._corrupt_every_row(tmp_path)

        after = OrchestratorState()
        projection = ControlOperationOwnership(after, _ledger(tmp_path)).reconcile(
            [_key()]
        )

        assert projection.unavailable == (_key(),)
        assert _eligibility(after) is QueueMutationStatus.REJECTED_EXCLUDED


class TestNoDoubleOwnership:
    """Direction 7: a conflicting reservation is typed, never admitted."""

    def test_a_second_holder_is_refused_and_told_who_won(self, tmp_path):
        ledger = _ledger(tmp_path)
        first = ControlOperationOwnership(
            OrchestratorState(), ledger, holder="engine-a"
        )
        first.claim(_key())

        second_state = OrchestratorState()
        second = ControlOperationOwnership(
            second_state, _ledger(tmp_path), holder="engine-b"
        )
        entry = second.claim(_key())

        assert entry.status is ControlOperationOwnershipStatus.CONTENDED
        assert entry.holder == "engine-a"
        assert not second.owns(_key())
        # The winner's row is untouched, and the loser still excludes ordinary
        # work: someone IS running this operation.
        rows = ledger.list_control_operation_ownership().rows
        assert [row.holder for row in rows] == ["engine-a"]
        assert _eligibility(second_state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_a_loser_cannot_release_the_winners_row(self, tmp_path):
        ledger = _ledger(tmp_path)
        ControlOperationOwnership(
            OrchestratorState(), ledger, holder="engine-a"
        ).claim(_key())

        loser = ControlOperationOwnership(
            OrchestratorState(), ledger, holder="engine-b"
        )
        release = loser.release(_key())

        assert release.status is ControlOperationReleaseStatus.NOT_HELD
        assert [row.holder for row in ledger.list_control_operation_ownership().rows] == [
            "engine-a"
        ]

    def test_a_losers_release_does_not_free_the_winners_operation(self, tmp_path):
        """The loser's own ``try/finally`` unwind must free nothing.

        A ``CONTENDED`` claim publishes an exclusion because someone IS running
        the operation. The release that follows deletes nothing — the row is
        the winner's — so treating it as settlement would readmit ordinary work
        on an issue whose control operation is still live under engine-a.
        """
        ledger = _ledger(tmp_path)
        ControlOperationOwnership(
            OrchestratorState(), ledger, holder="engine-a"
        ).claim(_key())

        loser_state = OrchestratorState()
        loser = ControlOperationOwnership(
            loser_state, _ledger(tmp_path), holder="engine-b"
        )
        loser.claim(_key())
        release = loser.release(_key())

        assert release.status is ControlOperationReleaseStatus.NOT_HELD
        assert release.holder == "engine-a"
        assert loser_state.control_operation_exclusions.contended == (_key(),)
        assert _eligibility(loser_state) is QueueMutationStatus.REJECTED_EXCLUDED
        assert [
            row.holder for row in ledger.list_control_operation_ownership().rows
        ] == ["engine-a"]

    def test_reconciliation_reports_contention_rather_than_stealing(self, tmp_path):
        ledger = _ledger(tmp_path)
        ControlOperationOwnership(
            OrchestratorState(), ledger, holder="engine-a"
        ).claim(_key())

        peer_state = OrchestratorState()
        projection = ControlOperationOwnership(
            peer_state, ledger, holder="engine-b"
        ).reconcile([_key()])

        assert projection.contended == (_key(),)
        assert projection.owned == ()
        assert _eligibility(peer_state) is QueueMutationStatus.REJECTED_EXCLUDED


class TestReleaseSettles:
    """Direction 8: settlement leaves no stranded row and no stranded block."""

    def test_release_then_reconcile_restores_ordinary_eligibility(
        self, tmp_path, state
    ):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)
        ownership.claim(_key())

        release = ownership.release(_key())
        ownership.reconcile([])

        assert release.status is ControlOperationReleaseStatus.RELEASED
        assert ledger.list_control_operation_ownership().rows == ()
        assert state.control_operation_exclusions.entries == ()
        assert _eligibility(state) is QueueMutationStatus.ACCEPTED

    def test_release_survives_a_restart(self, tmp_path):
        first = OrchestratorState()
        ControlOperationOwnership(first, _ledger(tmp_path)).claim(_key())
        ControlOperationOwnership(first, _ledger(tmp_path)).release(_key())

        after = OrchestratorState()
        ownership = ControlOperationOwnership(after, _ledger(tmp_path))
        projection = ownership.reconcile([])

        assert projection.entries == ()
        assert _eligibility(after) is QueueMutationStatus.ACCEPTED

    def test_releasing_what_was_never_held_is_success(self, tmp_path, state):
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))

        release = ownership.release(_key())

        assert release.status is ControlOperationReleaseStatus.NOT_HELD
        assert release.settled


class TestExactCandidateBinding:
    """Direction 9: A and A' are different operations."""

    def test_ownership_of_a_is_not_ownership_of_a_prime(self, tmp_path, state):
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))

        ownership.claim(_key(SHA_A))

        assert ownership.owns(_key(SHA_A))
        assert not ownership.owns(_key(SHA_A_PRIME))

    def test_each_candidate_gets_its_own_row(self, tmp_path, state):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)

        ownership.claim(_key(SHA_A))
        ownership.claim(_key(SHA_A_PRIME))

        rows = ledger.list_control_operation_ownership().rows
        assert sorted(row.key.head_sha for row in rows) == sorted(
            [SHA_A, SHA_A_PRIME]
        )

    def test_reconciling_a_prime_releases_a(self, tmp_path, state):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)
        ownership.claim(_key(SHA_A))

        projection = ownership.reconcile([_key(SHA_A_PRIME)])

        assert projection.owned == (_key(SHA_A_PRIME),)
        assert [row.key.head_sha for row in ledger.list_control_operation_ownership().rows] == [
            SHA_A_PRIME
        ]

    def test_the_candidate_identity_is_the_attempt_key(self):
        """The operation is bound to the same ``(issue, commit)`` #139 names."""
        candidate = AttemptKey(GitHubIssueKey(repo=REPO, external_id="146"), SHA_A)

        assert ControlOperationKey.for_candidate(candidate, CONTINUATION) == _key(SHA_A)

    def test_an_inexact_candidate_is_refused(self):
        with pytest.raises(ValueError):
            _key("1" * 7)


class TestSchedulerOrdering:
    """Direction 10: exclusion comes from the reconciled projection only."""

    def test_a_raw_row_does_not_exclude_ordinary_work(self, tmp_path, state):
        """Bypassing reconciliation must not reach the scheduler."""
        ledger = _ledger(tmp_path)
        ledger.reserve_control_operation(_key(), holder="single-instance")

        assert _eligibility(state) is QueueMutationStatus.ACCEPTED

        ControlOperationOwnership(state, ledger).reconcile([_key()])

        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_an_excluded_issue_stays_visible_to_reconciliation(self, tmp_path, state):
        issue = _issue()
        state.cached_scope_issues = [issue]
        ControlOperationOwnership(state, _ledger(tmp_path)).claim(_key())

        cache = QueueCache(_config(), state)

        assert cache.evaluate_issue(issue) is QueueMutationStatus.REJECTED_EXCLUDED
        assert [i.number for i in cache.reconciliation_only_issues()] == [ISSUE_NUMBER]

    def test_only_the_operation_s_own_issue_is_excluded(self, tmp_path, state):
        ControlOperationOwnership(state, _ledger(tmp_path)).claim(_key())

        assert _eligibility(state, ISSUE_NUMBER) is QueueMutationStatus.REJECTED_EXCLUDED
        assert _eligibility(state, ISSUE_NUMBER + 1) is QueueMutationStatus.ACCEPTED

    def test_a_refreshed_issue_is_kept_out_of_the_queue(self, tmp_path, state):
        ControlOperationOwnership(state, _ledger(tmp_path)).claim(_key())

        outcome = QueueCache(_config(), state).upsert_refreshed_issue(_issue())

        assert outcome.status is QueueMutationStatus.REJECTED_EXCLUDED
        assert outcome.in_queue is False
        assert state.cached_queue_issues == []

    def test_a_refresh_readmits_the_issue_once_the_operation_settles(
        self, tmp_path, state
    ):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)
        ownership.claim(_key())
        cache = QueueCache(_config(), state)

        assert cache.replace_from_refresh([_issue()]) == []

        ownership.release(_key())

        assert [i.number for i in cache.replace_from_refresh([_issue()])] == [
            ISSUE_NUMBER
        ]


class TestLayerSeparation:
    """Direction 11: session ownership and operation ownership coexist."""

    def test_a_later_session_does_not_become_the_operation_s_owner(
        self, tmp_path, state
    ):
        ledger = _ledger(tmp_path)
        ownership = ControlOperationOwnership(state, ledger)
        ownership.claim(_key())

        state.session_history.append(
            SessionHistoryEntry(
                issue_number=ISSUE_NUMBER,
                title="Issue 146",
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=1,
            )
        )

        # Two independent exclusions over one issue. The operation is still
        # owned, and the session record did not create ownership.
        assert ownership.owns(_key())
        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED
        assert len(ledger.list_control_operation_ownership().rows) == 1

    def test_releasing_the_operation_leaves_the_session_exclusion_alone(
        self, tmp_path, state
    ):
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))
        ownership.claim(_key())
        state.session_history.append(
            SessionHistoryEntry(
                issue_number=ISSUE_NUMBER,
                title="Issue 146",
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=1,
            )
        )

        ownership.release(_key())

        assert state.control_operation_exclusions.entries == ()
        assert _eligibility(state) is QueueMutationStatus.REJECTED_EXCLUDED

    def test_operation_ownership_is_not_rebuilt_from_session_records(
        self, tmp_path, state
    ):
        state.session_history.append(
            SessionHistoryEntry(
                issue_number=ISSUE_NUMBER,
                title="Issue 146",
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=1,
            )
        )
        ledger = _ledger(tmp_path)

        ControlOperationOwnership(state, ledger).reconcile([])

        assert ledger.list_control_operation_ownership().rows == ()
        assert state.control_operation_exclusions.entries == ()


class TestNoAuthorityEffect:
    """Direction 12: ownership grants nothing and records nothing else."""

    def test_the_whole_lifecycle_touches_no_other_durable_record(
        self, tmp_path, state
    ):
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))

        ownership.claim(_key())
        ownership.reconcile([_key()])
        ownership.release(_key())

        # Nothing in the shared database moved but this concern's own table.
        claims = _claims(tmp_path)
        assert claims.latched_publication_refusals() == frozenset()
        assert claims.needs_human_causes(ISSUE_NUMBER) == frozenset()
        assert claims.list_quarantines() == ()
        assert claims.list_unresolved_claims() == ()

    def test_no_label_or_history_is_written(self, tmp_path, state):
        issue = _issue()
        state.cached_scope_issues = [issue]
        ownership = ControlOperationOwnership(state, _ledger(tmp_path))

        ownership.claim(_key())
        ownership.reconcile([_key()])

        assert issue.labels == ["agent:backend"]
        assert state.session_history == []
        assert state.completed_today == []
        assert state.failed_this_cycle == set()
        assert state.priority_queue == []
