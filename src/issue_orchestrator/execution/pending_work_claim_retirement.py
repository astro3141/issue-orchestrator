"""The retirement operation on the pending-work ledger (#245).

Its own module for the reason the schema is: it answers a different question
from the rest of the store and changes for different reasons. What it is NOT is
a second owner of the claim table — every function here runs inside a
transaction :class:`~.pending_work_claim_store.SqlitePendingWorkClaimStore`
opened and holds its write lock, exactly as ``_set_claim_parked`` does. The row
still has one owner; this is the operation that owner delegates.

The whole of it is one shape: **check everything, then write everything**. Each
check is a refusal that leaves the ledger untouched, and the two writes — the
bit on the row and the evidence record — happen after the last of them, in the
caller's transaction, so a retirement can never be half-taken.

Every statement below spells its columns out rather than composing them from a
shared constant. They are short, and a literal query is one nothing has to
reason about the construction of.
"""

from __future__ import annotations

import sqlite3

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..ports.pending_work_claim_retirement import (
    ClaimRetirementRefusal,
    ClaimRetirementRefused,
    ClaimRetirementRequest,
    ClaimRetirementTarget,
    RetiredClaimRecord,
)
from .pending_work_codec import (
    PendingWorkClaimDecodeError,
    decode_claim_text,
)

def retire_claim(
    conn: sqlite3.Connection, request: ClaimRetirementRequest
) -> RetiredClaimRecord:
    """Retire the claim ``request`` describes, or raise having written nothing."""
    row = _addressed_row(conn, request.target)
    claim = _readable_claim(row)
    _refuse_unless_described(conn, row, claim, request.target)
    record = RetiredClaimRecord(
        run_key=str(row["run_key"]),
        started_at=str(row["started_at"]),
        session_name=str(row["session_name"]),
        issue_number=int(row["issue_number"]),
        work_key=str(row["work_key"]),
        work_kind=claim.kind,
        flavor=claim.flavor,
        # The payload EXACTLY as the ledger holds it, not a re-encoding of the
        # decoded claim. A record of what was abandoned that had been through
        # this build's encoder would prove what this build believes, which is
        # not the same artifact and not what an audit is asking for.
        payload=str(row["payload"]),
        reason=request.reason,
        authority=request.authority,
        recorded_at=request.recorded_at,
    )
    conn.execute(
        "UPDATE pending_work_claim SET retired = 1 WHERE run_key = ?",
        (record.run_key,),
    )
    conn.execute(
        "INSERT INTO pending_work_claim_retirement "
        "(run_key, started_at, session_name, issue_number, work_key, "
        "work_kind, flavor, payload, reason, authority, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.run_key,
            record.started_at,
            record.session_name,
            record.issue_number,
            record.work_key,
            record.work_kind.value,
            record.flavor,
            record.payload,
            record.reason,
            record.authority,
            record.recorded_at,
        ),
    )
    return record


def retired_claim_records(
    conn: sqlite3.Connection,
) -> tuple[RetiredClaimRecord, ...]:
    """Every recorded retirement, oldest first."""
    return tuple(
        RetiredClaimRecord(
            run_key=str(row["run_key"]),
            started_at=str(row["started_at"]),
            session_name=str(row["session_name"]),
            issue_number=int(row["issue_number"]),
            work_key=str(row["work_key"]),
            work_kind=PendingWorkKind(row["work_kind"]),
            flavor=None if row["flavor"] is None else str(row["flavor"]),
            payload=str(row["payload"]),
            reason=str(row["reason"]),
            authority=str(row["authority"]),
            recorded_at=str(row["recorded_at"]),
        )
        for row in conn.execute(
            "SELECT run_key, started_at, session_name, issue_number, work_key, "
            "work_kind, flavor, payload, reason, authority, recorded_at "
            "FROM pending_work_claim_retirement ORDER BY recorded_at, run_key"
        )
    )


def _addressed_row(
    conn: sqlite3.Connection, target: ClaimRetirementTarget
) -> sqlite3.Row:
    """The ONE ledger row carrying this work identity, or a refusal.

    ``work_key`` is indexed rather than unique, so "the row" is a finding and
    not a given. Two rows sharing it means two runs are recorded against the
    same work — a state the ledger tolerates — and choosing between them here
    would retire whichever one the query happened to return first.
    """
    rows = conn.execute(
        "SELECT run_key, started_at, session_name, issue_number, work_key, "
        "parked, retired, payload FROM pending_work_claim WHERE work_key = ?",
        (target.work_key,),
    ).fetchall()
    if not rows:
        raise ClaimRetirementRefused(
            ClaimRetirementRefusal.NO_SUCH_CLAIM,
            f"no pending-work claim is recorded for {target.work_key!r}",
        )
    if len(rows) > 1:
        raise ClaimRetirementRefused(
            ClaimRetirementRefusal.AMBIGUOUS_IDENTITY,
            f"{len(rows)} pending-work claims are recorded for "
            f"{target.work_key!r} ("
            + ", ".join(sorted(str(row["run_key"]) for row in rows))
            + "); which one is meant cannot be inferred",
        )
    return rows[0]


def _readable_claim(row: sqlite3.Row) -> PendingWorkClaim:
    """The row's claim, or a refusal to retire what cannot be confirmed.

    An unreadable payload is not merely inconvenient here: the operator's
    expectations are checked AGAINST it, so retiring one would be retiring a row
    on the strength of its address alone. It also already has a disposition of
    its own — a quarantine settles it and puts it in front of a human (#210) —
    and a second, unverified path to a terminal state is what would make the two
    disagree.
    """
    try:
        return decode_claim_text(row["payload"], source=str(row["run_key"]))
    except PendingWorkClaimDecodeError as exc:
        raise ClaimRetirementRefused(
            ClaimRetirementRefusal.CLAIM_UNREADABLE,
            f"the claim recorded for {str(row['work_key'])!r} cannot be read by "
            f"this build ({exc.readability.value}), so it cannot be confirmed "
            f"as the one described: {exc}",
        ) from exc


def _refuse_unless_described(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    claim: PendingWorkClaim,
    target: ClaimRetirementTarget,
) -> None:
    """Every remaining reason to change nothing, checked before anything is."""
    if row["retired"] or _already_recorded(conn, row):
        raise ClaimRetirementRefused(
            ClaimRetirementRefusal.ALREADY_RETIRED,
            f"the claim recorded for {str(row['work_key'])!r} has already been "
            "retired; retiring it again would overwrite the evidence of the "
            "first decision",
        )
    if row["parked"]:
        raise ClaimRetirementRefused(
            ClaimRetirementRefusal.QUARANTINE_SETTLED,
            f"the claim recorded for {str(row['work_key'])!r} has been settled "
            "by a quarantine; releasing that quarantine is what un-settles it, "
            "and a retirement recorded underneath it would leave two "
            "authorities over one row",
        )
    # Recorded at hold time by the orchestrator, not derived from the payload:
    # the trusted issue number is the one the ledger wrote down (#6999 F12).
    _refuse_on_mismatch(row, "issue", int(row["issue_number"]), target.issue_number)
    _refuse_on_mismatch(row, "work kind", claim.kind.value, target.work_kind.value)
    _refuse_on_mismatch(row, "flavor", claim.flavor, target.flavor)


def _refuse_on_mismatch(
    row: sqlite3.Row, field: str, recorded: object, described: object
) -> None:
    if recorded != described:
        raise ClaimRetirementRefused(
            ClaimRetirementRefusal.IDENTITY_MISMATCH,
            f"the claim recorded for {str(row['work_key'])!r} has {field} "
            f"{recorded!r}, and the retirement describes {described!r}",
        )


def _already_recorded(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Whether this exact run generation already has a retirement record.

    Asked as well as the bit, because the two can only disagree if something
    outside this module has been editing durable state by hand — and the safe
    reading of that is that a decision was already taken, not that a fresh one
    may overwrite it.
    """
    return (
        conn.execute(
            "SELECT 1 FROM pending_work_claim_retirement "
            "WHERE run_key = ? AND started_at = ?",
            (row["run_key"], row["started_at"]),
        ).fetchone()
        is not None
    )


__all__ = ["retire_claim", "retired_claim_records"]
