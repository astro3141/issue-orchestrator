"""SQL for the tech-lead canonical-context provenance ledger (#183).

The row this owns is a SIBLING of ``tech_lead_launch_authority`` — same
``(run_id, session_name)`` key, opposite meaning. The authority row says what a
session may DO; this one says only what canonical text a planning run was
handed, by issue, revision and digest. Keeping its SQL here keeps that
distinction visible in the file layout too, and keeps the authority adapter
about authority.

The other difference is lifetime, and it is why this is not simply another
method beside ``record``/``discard``: authority rows are discarded at the run's
terminal, while a provenance row is never deleted. It has to outlive the
disposable planning worktree it describes, or the question "which sources
governed that run" stops having an answer the moment the run is cleaned up.

Plain functions over a connection, not a class: the owning store already owns
the connection, the write lock, and the transaction boundary, and these must
run inside them (the same arrangement ``tech_lead_pending_intents`` uses).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from ..domain.canonical_context import CanonicalContextSnapshot
from ..ports.tech_lead_authority import TechLeadAuthorityConflictError

logger = logging.getLogger(__name__)


def select(
    conn: sqlite3.Connection, *, run_id: str, session_name: str
) -> CanonicalContextSnapshot | None:
    """The context staged for one run, or None when nothing was staged.

    Malformed stored content raises ValueError loudly — the store is
    orchestrator-owned, so corruption is a bug, never agent input to fail-safe
    around.
    """
    row = conn.execute(
        "SELECT context FROM tech_lead_canonical_context "
        "WHERE run_id = ? AND session_name = ?",
        (run_id, session_name),
    ).fetchone()
    if row is None:
        return None
    return CanonicalContextSnapshot.from_dict(json.loads(row["context"]))


def insert(
    tx: sqlite3.Connection,
    *,
    run_id: str,
    session_name: str,
    snapshot: CanonicalContextSnapshot,
) -> None:
    """Record what governed one run (create-once).

    An identical payload is a no-op (crash-retry safe); a DIFFERENT payload for
    an existing run raises :class:`TechLeadAuthorityConflictError`. A run's
    staged context is a historical fact — a deliberate re-run records its newer
    snapshot under its own run identity rather than rewriting this one.
    """
    row = tx.execute(
        "SELECT context FROM tech_lead_canonical_context "
        "WHERE run_id = ? AND session_name = ?",
        (run_id, session_name),
    ).fetchone()
    if row is not None:
        if CanonicalContextSnapshot.from_dict(json.loads(row[0])) == snapshot:
            return
        raise TechLeadAuthorityConflictError(
            f"canonical context already recorded for run_id={run_id!r} "
            f"session={session_name!r} with a different payload"
        )
    tx.execute(
        "INSERT INTO tech_lead_canonical_context "
        "(run_id, session_name, context, recorded_at) VALUES (?, ?, ?, ?)",
        (
            run_id,
            session_name,
            json.dumps(snapshot.to_dict(), sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    logger.info(
        "[tech_lead] Recorded canonical context: run_id=%s session=%s subject=#%s"
        " sources=%s",
        run_id,
        session_name,
        snapshot.subject_issue_number,
        [source.issue_number for source in snapshot.sources],
    )
