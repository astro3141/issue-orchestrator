"""Schema and migrations for the orchestrator-owned pending-work ledger.

Split from the operations that use it (#210) because they answer different
questions and change for different reasons. This module answers "what shape is
this database, and how does one written by an earlier build become that shape";
the store answers "what does the orchestrator do with the rows".

Two mechanisms live here, and mixing them up is how claims get lost:

* an ALL-OR-NOTHING rebuild for the claim table, used when its shape changed in
  a way that cannot be reconciled row by row (#6999 F13). Every row is decoded
  before anything is renamed, and one row that cannot be carried forward aborts
  the whole thing with the legacy table untouched;
* ADDITIVE columns, for changes whose absence is indistinguishable from their
  default - a quarantine that has announced nothing, a claim that has been
  parked by nobody. Nothing has to be rebuilt, so nothing can be lost.
"""

from __future__ import annotations

import logging
import sqlite3

from ..domain.pending_work import PendingWorkClaim
from .pending_work_codec import (
    PendingWorkClaimDecodeError,
    decode_claim_text,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_work_claim (
    run_key TEXT PRIMARY KEY,
    work_key TEXT NOT NULL,
    deferred INTEGER NOT NULL DEFAULT 0,
    session_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    payload TEXT NOT NULL,
    -- Settled by a quarantine (#210). Separate from ``deferred`` because they
    -- are opposite instructions: deferred work is waiting to be relaunched,
    -- parked work must never be relaunched by the orchestrator at all. The row
    -- survives as the evidence the escalation points at, and a human decides
    -- what becomes of it.
    -- NOTE: no semicolons in this comment - the schema is split on them.
    parked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS pending_work_claim_work
    ON pending_work_claim (work_key);
CREATE TABLE IF NOT EXISTS pending_work_claim_quarantine (
    quarantine_key TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    session_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    error TEXT NOT NULL,
    -- Durable state machine (#6999 F12). ``label_state`` records whether THIS
    -- quarantine actually acquired the shared blocking label or found it
    -- already there, so release can only ever remove a label it added.
    -- ``announced`` is separate because AddLabel can land while the comment
    -- fails. ``releasing`` marks a resolved cause whose cleanup has not yet
    -- committed, so the row survives to be retried.
    label_state TEXT NOT NULL DEFAULT 'unknown',
    announced INTEGER NOT NULL DEFAULT 0,
    releasing INTEGER NOT NULL DEFAULT 0,
    -- The story the announcement was written for, and the work it names
    -- (#6999 F6, #209). Durable because ``announced`` is: a quarantine
    -- re-observed telling a DIFFERENT story has to rewrite the operator's
    -- comment, and the only way to know it changed is to have kept the one that
    -- was announced. One opaque token rather than a column per input, so an
    -- input added to AnnouncedStory changes what this compares without changing
    -- this statement. Nullable on purpose - a row from before this column reads
    -- as "no story recorded", which must differ from every observable story so
    -- the next scan re-announces rather than standing on one nothing vouches
    -- for. Databases written by an earlier build keep a vestigial ``cause``
    -- column, which is nullable, never written and never read.
    story TEXT,
    work_kind TEXT,
    -- Remote announcement attempts spent on the story above (#210). Delivery
    -- is its own bounded concern: the local disposition is already committed by
    -- the time this is touched, so an undeliverable comment stops attempting
    -- rather than holding the quarantine open and re-writing forever.
    -- NOTE: no semicolons in this comment - the schema is split on them.
    announce_attempts INTEGER NOT NULL DEFAULT 0
);
-- Durable provenance for every OTHER cause of the shared needs-human block
-- (#6999 F2 round 2). The tech-lead marker label and the quarantine table
-- above already record their own. A session or planner escalation recorded
-- nothing, so a remover saw an owner-less label and took it off. Rows are
-- meaningful only while the label is present and are dropped with it, so a
-- stale one can never strand an issue in needs-human.
-- NOTE: no semicolons in this comment - the schema is split on them.
CREATE TABLE IF NOT EXISTS needs_human_cause (
    issue_number INTEGER NOT NULL,
    cause TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (issue_number, cause)
);
-- Publication-gate refusals whose label write did not commit (#51). The label
-- is the primary record of the verdict, so a row here means only "the gate
-- refused this issue and could not say so remotely" - a fact nothing else
-- durably holds, and one that must outlive the process that observed it or
-- review becomes eligible again for a candidate the gate refused.
-- Latching only ever WITHHOLDS review, so the worst a stale row can do is
-- hold an issue until the next candidate clears the gate and releases it.
-- NOTE: no semicolons in this comment - the schema is split on them.
CREATE TABLE IF NOT EXISTS publication_refusal_latch (
    issue_number INTEGER PRIMARY KEY
);
"""

# Additive columns the quarantine table gained after it shipped. ``CREATE TABLE
# IF NOT EXISTS`` leaves an existing table exactly as it is, so a database
# written by an earlier build keeps the old shape and every later statement
# referencing these columns fails. Unlike the claim table this needs no
# all-or-nothing rebuild: quarantines carry no queued work, so a NULL story is
# recoverable by the next scan re-announcing (#6999 F6).
QUARANTINE_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("story", "TEXT"),
    ("work_kind", "TEXT"),
    ("announce_attempts", "INTEGER NOT NULL DEFAULT 0"),
)

# The same treatment for the CLAIM table's one additive column (#210). It is
# additive on the same terms: an older row simply has not been parked, which is
# what an unquarantined claim's state already was, so nothing has to be rebuilt
# and no claim can be lost by adding it.
CLAIM_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("parked", "INTEGER NOT NULL DEFAULT 0"),
)

STORE_FILENAME = "pending_work_claims.sqlite"


def schema_statements() -> tuple[str, ...]:
    """The schema as individual statements.

    ``executescript`` commits any pending transaction before it runs, so it
    cannot be used inside the migration's single transaction (#6999 F13).
    """
    return tuple(
        statement.strip()
        for statement in _SCHEMA.split(";")
        if statement.strip()
    )

class PendingWorkClaimMigrationError(RuntimeError):
    """An older ledger holds a row that cannot be carried forward (#6999 F13).

    Raised before anything is renamed or dropped, so the legacy table stays
    exactly as it was. Failing startup is the correct outcome: the alternative
    is running with an authoritative record the orchestrator cannot see, which
    is precisely how a live terminal gets admitted as carrying no work.
    """

def _issue_number_of(claim: PendingWorkClaim) -> int:
    """Trusted issue number for a claim being migrated forward."""
    request = claim.request
    resolver = getattr(request, "resolve_issue_number", None)
    if resolver is not None:
        return int(resolver() or 0)
    return int(getattr(request, "issue_number", 0))

def add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    added: tuple[tuple[str, str], ...],
) -> None:
    """Bring an earlier table up to the current column set.

    Only for columns whose absence is indistinguishable from their default,
    which is why the claim table's all-or-nothing rebuild above is a
    separate mechanism: this one must never be reached for a change that
    could lose a row.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, declaration in added:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

def migrate_legacy_claim_table(conn: sqlite3.Connection) -> None:
    """Carry an older table forward WITHOUT losing a single claim.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
    database written against an earlier shape would keep columns the new
    statements do not know about. Dropping it is not an option (#6999 F13):
    these rows are the only authoritative copy of work that has already left
    its queue, and the whole reason this table exists is that terminal
    discovery CANNOT reconstruct a typed queued request. An upgrade with a
    live review, validation retry, rework or failure investigation would
    delete exactly the record restoration is about to need.

    So the migration is all-or-nothing. Every row is decoded FIRST, before
    anything is renamed or created. If even one cannot be rebuilt, nothing
    is touched and initialization raises: the legacy table remains the
    authority, intact and inspectable. Archiving the bad row into a side
    table while startup carried on was worse than the drop it replaced -
    the row became operationally invisible, so a surviving terminal for it
    read as ABSENT and could be admitted claimless.
    """
    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'pending_work_claim_old'"
    ).fetchone()
    if leftover is not None:
        # Belt and braces: the single transaction below makes a surviving
        # ``_old`` table unreachable, but a database that somehow reached
        # that state must never be started against silently - the whole
        # point of F13 is that its rows are still the authority.
        raise PendingWorkClaimMigrationError(
            "pending_work_claim_old is present, so a previous migration did "
            "not complete. It still holds authoritative claims; resolve it "
            "before starting again."
        )
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(pending_work_claim)")
    }
    if not columns or {"work_key", "deferred", "issue_number"} <= columns:
        return
    legacy = list(
        conn.execute(
            "SELECT run_key, session_name, run_id, started_at, payload "
            "FROM pending_work_claim"
        )
    )
    migrated: list[tuple[object, ...]] = []
    for row in legacy:
        try:
            claim = decode_claim_text(row["payload"], source=row["run_key"])
        except PendingWorkClaimDecodeError as exc:
            raise PendingWorkClaimMigrationError(
                f"pending-work claim for run {row['run_key']} cannot be "
                f"migrated to the current schema: {exc}. The existing table "
                "is left untouched and remains authoritative; resolve or "
                "remove that row before starting again."
            ) from exc
        migrated.append(
            (
                row["run_key"],
                claim.work_key(),
                row["session_name"],
                row["run_id"],
                row["started_at"],
                _issue_number_of(claim),
                row["payload"],
            )
        )
    # ONE transaction for rename + create + copy + drop (#6999 F13).
    # ``executescript`` would commit any pending transaction before running,
    # which is exactly how a stop mid-migration could leave a current-shaped
    # table beside the renamed original - after which the column check
    # ABOVE returns early on the next start and the real rows are invisible.
    # The schema statements are therefore executed individually, inside an
    # explicit transaction that rolls back as a unit.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "ALTER TABLE pending_work_claim RENAME TO pending_work_claim_old"
        )
        for statement in schema_statements():
            conn.execute(statement)
        conn.executemany(
            "INSERT OR REPLACE INTO pending_work_claim "
            "(run_key, work_key, deferred, session_name, run_id, started_at, "
            "issue_number, payload) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
            migrated,
        )
        conn.execute("DROP TABLE pending_work_claim_old")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    logger.info(
        "[WORK] Migrated %d pending-work claim(s) to the current schema",
        len(migrated),
    )


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Bring ``conn``'s database to the current shape, losing nothing.

    Migration FIRST, because it decides whether the tables that follow are
    created beside a legacy one or replace it; then the current schema; then the
    additive columns an existing table has not learned yet.
    """
    migrate_legacy_claim_table(conn)
    for statement in schema_statements():
        conn.execute(statement)
    add_missing_columns(
        conn, "pending_work_claim_quarantine", QUARANTINE_ADDED_COLUMNS
    )
    add_missing_columns(conn, "pending_work_claim", CLAIM_ADDED_COLUMNS)
    conn.commit()


__all__ = [
    "PendingWorkClaimMigrationError",
    "initialize_schema",
]
