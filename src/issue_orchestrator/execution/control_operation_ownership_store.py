"""SQLite leases for terminal-less control operations (#146).

Its own module, its own table, its own API — the separate-typed-concern shape
the publication-refusal latch (#51) established — inside the SAME
orchestrator-owned database as the pending-work ledger. Sharing the file is the
point rather than an accident: that database already sits outside every
agent-writable worktree, and it already carries the startup integrity checks,
pragma enforcement and backups (``infra/sqlite_registry.py``) that a record
nothing else can reconstruct must have. Sharing a boundary is not sharing a
meaning, so the rows stay in a table of their own:

* a **pending-work** row means a queue request was dequeued. Nothing was
  dequeued here — that is the whole premise of a control operation;
* an **attempt** record means exact-candidate evidence. Runtime ownership is
  not evidence, and putting it there would make the evidence mutable;
* a **control-operation** row means one holder reserved one exact-candidate
  operation. It says nothing about whether that operation is still live.

That last line is the invariant the whole design rests on. Liveness is supplied
to :class:`~..control.control_operation_ownership.ControlOperationOwnership` by
its caller, and a row no live operation claims is released by reconciliation.
If a row could vouch for itself, a crash between reservation and settlement
would leave a durable lease that excludes ordinary work forever, with hand
editing of durable state as the only exit.

The address is the whole exact-candidate identity — issue scope, issue stable
id, head SHA, kind — as a composite primary key rather than a composed string.
Nothing has to parse it back out, so no second spelling of the identity can
drift from the first, and ``A`` and ``A'`` are different rows by construction.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.control_operation import ControlOperationKey, ControlOperationKind
from ..domain.issue_key import GitHubIssueKey
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..ports.control_operation_ownership_store import (
    ControlOperationOwnershipRead,
    ControlOperationOwnershipRow,
    ControlOperationReadStatus,
    ControlOperationRelease,
    ControlOperationReleaseStatus,
    ControlOperationReservation,
    ControlOperationReservationStatus,
)
from .pending_work_claim_store import STORE_FILENAME

_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_operation_ownership (
    issue_scope TEXT NOT NULL,
    issue_stable_id TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    kind TEXT NOT NULL,
    holder TEXT NOT NULL,
    PRIMARY KEY (issue_scope, issue_stable_id, head_sha, kind)
);
"""


def _row(row: sqlite3.Row) -> ControlOperationOwnershipRow:
    """Rebuild one lease from its columns, refusing anything that is not one.

    The identity is rebuilt from the four stored values rather than parsed out
    of a composed key, and both halves are re-validated by their own domain
    rules on the way in: an unknown kind, or a SHA that is not a full hex one,
    raises rather than producing an identity that would compare unequal to the
    live operation it is supposed to be.
    """
    return ControlOperationOwnershipRow(
        key=ControlOperationKey(
            GitHubIssueKey(
                repo=str(row["issue_scope"]),
                external_id=str(row["issue_stable_id"]),
            ),
            str(row["head_sha"]),
            ControlOperationKind(row["kind"]),
        ),
        holder=str(row["holder"]),
    )


class SqliteControlOperationOwnershipStore:
    """Durable reservations for one repository's control operations."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqliteControlOperationOwnershipStore":
        """Store handle for a repository's orchestrator state directory.

        The same database file the pending-work ledger uses, for the trust
        boundary and durability guarantees documented above. Called only by the
        composition root (and adapter tests); control code depends on the
        injected port instead.
        """
        return cls(state_dir(repo_root) / STORE_FILENAME)

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SCHEMA)
        conn.commit()

    def reserve_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationReservation:
        """Reserve ``key`` for ``holder``, or report who has it.

        One statement decides it: the insert either creates the row or
        conflicts with an existing one, so there is no read-then-write window a
        second holder could land in. Only the losing path reads, and only to
        say WHO won.
        """
        try:
            with self._write_lock, self._transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO control_operation_ownership "
                    "(issue_scope, issue_stable_id, head_sha, kind, holder) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(issue_scope, issue_stable_id, head_sha, kind) "
                    "DO NOTHING",
                    (*key.durable_parts, holder),
                )
                if cursor.rowcount > 0:
                    return ControlOperationReservation(
                        ControlOperationReservationStatus.GRANTED, holder=holder
                    )
                recorded = self._holder_of(conn, key)
        except sqlite3.Error as exc:
            return ControlOperationReservation(
                ControlOperationReservationStatus.UNAVAILABLE,
                detail=f"control-operation ownership for {key} is unwritable: {exc}",
            )
        if recorded is None:
            # The conflicting row was gone by the time we asked whose it was.
            # Reporting "free" here would admit the operation twice, so the
            # honest answer is that we could not tell.
            return ControlOperationReservation(
                ControlOperationReservationStatus.UNAVAILABLE,
                detail=(
                    f"control-operation ownership for {key} changed while it "
                    "was being reserved"
                ),
            )
        if recorded == holder:
            # This holder's own row, from before a restart or from an earlier
            # claim in this process. Adopting it is what makes a reservation
            # survivable without a terminal to rediscover it from.
            return ControlOperationReservation(
                ControlOperationReservationStatus.ADOPTED, holder=holder
            )
        return ControlOperationReservation(
            ControlOperationReservationStatus.HELD_BY_PEER,
            holder=recorded,
            detail=f"{recorded} reserved {key} first",
        )

    def release_control_operation(
        self, key: ControlOperationKey, *, holder: str
    ) -> ControlOperationRelease:
        """Drop ``holder``'s row for ``key``, never anyone else's."""
        try:
            with self._write_lock, self._transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM control_operation_ownership WHERE "
                    "issue_scope = ? AND issue_stable_id = ? AND head_sha = ? "
                    "AND kind = ? AND holder = ?",
                    (*key.durable_parts, holder),
                )
                if cursor.rowcount > 0:
                    return ControlOperationRelease(
                        ControlOperationReleaseStatus.RELEASED, holder=holder
                    )
                recorded = self._holder_of(conn, key)
        except sqlite3.Error as exc:
            return ControlOperationRelease(
                ControlOperationReleaseStatus.UNAVAILABLE,
                detail=f"control-operation ownership for {key} is unwritable: {exc}",
            )
        return ControlOperationRelease(
            ControlOperationReleaseStatus.NOT_HELD, holder=recorded or ""
        )

    def list_control_operation_ownership(self) -> ControlOperationOwnershipRead:
        """Every reservation, or a typed refusal to guess at an empty one.

        A row this store cannot decode is reported as an unreadable store
        rather than skipped. Only this code writes that table, so an
        undecodable row is corruption — and skipping it would let an operation
        whose lease exists read as free.
        """
        try:
            rows = tuple(
                _row(row)
                for row in self._get_connection().execute(
                    "SELECT issue_scope, issue_stable_id, head_sha, kind, holder "
                    "FROM control_operation_ownership"
                )
            )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            return ControlOperationOwnershipRead(
                ControlOperationReadStatus.UNAVAILABLE,
                detail=f"control-operation ownership is unreadable: {exc}",
            )
        return ControlOperationOwnershipRead(
            ControlOperationReadStatus.READABLE, rows
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _holder_of(
        conn: sqlite3.Connection, key: ControlOperationKey
    ) -> str | None:
        row = conn.execute(
            "SELECT holder FROM control_operation_ownership WHERE "
            "issue_scope = ? AND issue_stable_id = ? AND head_sha = ? AND kind = ?",
            key.durable_parts,
        ).fetchone()
        return str(row["holder"]) if row is not None else None

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._get_connection()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        conn.commit()


__all__ = ["SqliteControlOperationOwnershipStore"]
