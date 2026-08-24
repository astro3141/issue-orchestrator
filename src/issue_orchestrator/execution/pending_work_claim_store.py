"""SQLite-backed, orchestrator-owned pending-work ledger (#6999 F7/F8/F11/F12).

Lives in the repository's orchestrator state directory, NOT in the session
worktree. The worktree run directory is handed to the launched agent and is
writable by it, so a claim kept there could be edited by the very process whose
work it describes: an agent could change which queue its session is holding,
which PR a restored rework rewrites, or which paths a tech-lead investigation
admits as evidence roots. Restoration accepts this record as truth, so it has to
sit on the orchestrator's side of that boundary — the same reason tech-lead
launch authority is stored here rather than in the worktree (ADR-0031).

Two things follow from that boundary and are easy to get wrong:

* The row key is the run root **lexically normalised, never symlink-resolved**
  (#6999 F11). The run directory sits in the agent-writable worktree, so
  resolving it on every access would let an agent retarget the key with a
  symlink: the lookup would land on a different row, return "no claim", and the
  terminal would be admitted as claimless instead of quarantined. The lexical
  path is the one the orchestrator allocated and the terminal registry recorded.
* EVERY recorded identity field is validated on read, ``started_at`` included.
  Identity comes from the worktree manifest, and ``started_at`` later becomes
  trusted tech-lead evidence chronology, so accepting a rewritten one would
  launder agent-controlled data into orchestrator authority.

The quarantine table shares this database because it shares its trust boundary
and its lifetime, but not its lifecycle: a quarantine outlives the claim it
could not read and is cleared by a human, never by a session outcome. The
shared-needs-human causes, the publication-refusal latch (#51) and #146's
control-operation leases (own module, this database) are here on those terms.

The latch was measured against the other durable owners before landing here,
and each was ruled out on what it is allowed to hold rather than on
convenience: ``label_store.sqlite`` is a write-through GitHub mirror that
startup reconciliation prunes back to GitHub truth, so it would erase a latch
whose whole premise is that GitHub does NOT carry the label; ``timeline.sqlite``
is a per-issue ring buffer with a ``delete(issue)``, and a gate cannot rely on
evidence a trimming policy may drop; ``queue_cache.sqlite`` replaces and clears
wholesale; ``state.json`` persists best-effort with save errors swallowed; the
attempt sidecar is keyed by ``(issue, commit)``, which is candidate identity and
therefore #50's question, not this one; ``publish_retry_locators.json`` is
issue-keyed and durable but holds one typed record answering "how do I re-run
publish for this failed issue", which is a different gate and a different
meaning. What was needed was an issue-keyed, additive, never-trimmed,
never-reconciled row in orchestrator-owned storage, which is exactly what this
database already is.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..domain.session_run import SessionRunAssets
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..ports.pending_work_claim_retirement import (
    ClaimRetirementRequest,
    RetiredClaimRecord,
)
from ..ports.pending_work_claim_store import (
    AnnouncedStory,
    ClaimLookup,
    ClaimSettlement,
    ClaimState,
    ConflictingPendingWorkClaimError,
    QuarantineLabelState,
    QuarantineRecord,
    UnreadableClaim,
    UnresolvedClaim,
    quarantine_key_for_run,
)
from .pending_work_claim_retirement import (
    retire_claim as _retire_claim,
    retired_claim_records,
)
from .pending_work_claim_schema import (
    STORE_FILENAME,
    PendingWorkClaimMigrationError,
    initialize_schema,
)
from .pending_work_codec import (
    CorruptPendingWorkClaimError,
    PendingWorkClaimDecodeError,
    decode_claim_text,
    encode_claim_text,
)

logger = logging.getLogger(__name__)


def _quarantine_record(row: sqlite3.Row) -> QuarantineRecord:
    return QuarantineRecord(
        quarantine_key=str(row["quarantine_key"]),
        run_key=str(row["run_key"]),
        session_name=str(row["session_name"]),
        issue_number=int(row["issue_number"]),
        error=str(row["error"]),
        label_state=QuarantineLabelState(row["label_state"]),
        announced=bool(row["announced"]),
        releasing=bool(row["releasing"]),
        story=AnnouncedStory.parse(row["story"]),
        work_kind=PendingWorkKind(row["work_kind"]) if row["work_kind"] else None,
        announce_attempts=int(row["announce_attempts"]),
    )


class SqlitePendingWorkClaimStore:
    """Orchestrator-owned durable state for one repository.

    The claim ledger and its quarantine record, plus the two additive latches
    that only ever withhold: the shared-needs-human causes and the
    publication-refusal latch (#51).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqlitePendingWorkClaimStore":
        """Store handle for a repository's orchestrator state directory.

        Called only by a composition root and by adapter tests; control code
        depends on the injected ports instead. The operator retirement command
        (#245) is a composition root of its own — a separate process with one
        collaborator — and reaches this the same way ``trusted-runtime-promote``
        reaches the live-assurance store.
        """
        return cls(state_dir(repo_root) / STORE_FILENAME)

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        initialize_schema(self._get_connection())

    # -- claim lifecycle ---------------------------------------------------

    def hold_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim, *, issue_number: int
    ) -> None:
        key = self.run_key_for(run)
        payload = encode_claim_text(claim)
        identity = run.identity
        work_key = claim.work_key()
        with self._write_lock, self._transaction() as conn:
            # Relaunching the work is what resolves its earlier deferral, and it
            # resolves it whichever run deferred it. This runs BEFORE the
            # conflict check because run roots are named from a second-resolution
            # timestamp: a relaunch in the same second reuses the directory, and
            # the stale deferred row must not be mistaken for a rival claim.
            # Same transaction as the new hold, so the two can never both be
            # missing (#6999 F8).
            #
            # A RETIRED row is not one of these (#245). "Relaunching resolves
            # the deferral" is a statement about work still waiting to be
            # relaunched, which a retired row by definition is not: it is the
            # evidence of a decision, and a fresh launch of unrelated work that
            # happens to share this work key must not delete it.
            conn.execute(
                "DELETE FROM pending_work_claim "
                "WHERE work_key = ? AND deferred = 1 AND retired = 0",
                (work_key,),
            )
            row = conn.execute(
                "SELECT session_name, run_id, started_at, payload "
                "FROM pending_work_claim WHERE run_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if self._identity_matches(row, identity) and row["payload"] == payload:
                    return
                raise ConflictingPendingWorkClaimError(
                    f"run {key} already holds a different pending-work claim "
                    f"(run {row['run_id']}, session {row['session_name']!r}); "
                    f"refusing to overwrite it with one for run "
                    f"{identity.run_id}, session {identity.session_name!r}"
                )
            conn.execute(
                "INSERT INTO pending_work_claim "
                "(run_key, work_key, deferred, session_name, run_id, started_at, "
                "issue_number, payload) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    key,
                    work_key,
                    identity.session_name,
                    identity.run_id,
                    identity.started_at,
                    issue_number,
                    payload,
                ),
            )

    def defer_pending_work_claim(self, run: SessionRunAssets) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim SET deferred = 1 WHERE run_key = ?",
                (self.run_key_for(run),),
            )

    def consume_pending_work_claim(self, run: SessionRunAssets) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim WHERE run_key = ?",
                (self.run_key_for(run),),
            )

    def look_up_pending_work_claim(self, run: SessionRunAssets) -> ClaimLookup:
        key = self.run_key_for(run)
        row = self._get_connection().execute(
            "SELECT session_name, run_id, started_at, deferred, payload "
            "FROM pending_work_claim WHERE run_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return ClaimLookup(ClaimState.ABSENT)
        identity = run.identity
        if not self._identity_matches(row, identity):
            # The run root matched but the identity recorded against it did not.
            # Identity comes from the worktree manifest, which the agent can
            # write; refusing here is what turns a rewritten manifest into a
            # quarantined terminal instead of a silently claimless one.
            # Corrupt, not merely unfamiliar: it contradicts itself (#209).
            raise CorruptPendingWorkClaimError(
                f"run {key} holds a claim recorded for run {row['run_id']}, "
                f"session {row['session_name']!r}, started {row['started_at']!r}; "
                f"asked for run {identity.run_id}, session "
                f"{identity.session_name!r}, started {identity.started_at!r}"
            )
        claim = decode_claim_text(row["payload"], source=key)
        if row["deferred"]:
            # Deferred work belongs to the queue, not to this run. Answering
            # ABSENT would let a stale terminal be admitted as claimless and
            # settle work the queue already owns (#6999 F8).
            return ClaimLookup(ClaimState.DEFERRED, claim)
        return ClaimLookup(ClaimState.HELD, claim)

    # -- startup recovery --------------------------------------------------

    def list_unresolved_claims(self) -> tuple[UnresolvedClaim, ...]:
        unresolved: list[UnresolvedClaim] = []
        for row in self._all_rows():
            try:
                claim = decode_claim_text(row["payload"], source=row["run_key"])
            except PendingWorkClaimDecodeError:
                continue  # reported by list_unreadable_claims
            unresolved.append(
                UnresolvedClaim(
                    run_key=row["run_key"],
                    session_name=row["session_name"],
                    deferred=bool(row["deferred"]),
                    started_at=str(row["started_at"]),
                    issue_number=int(row["issue_number"]),
                    claim=claim,
                    # Reported, not applied (#210). Whether a settled row may go
                    # back to a queue is the re-admitting caller's question, and
                    # the sweep that escalates live trouble asks the opposite one
                    # of the same rows.
                    parked=bool(row["parked"]),
                    # Reported for the same reason and read by a different rule
                    # (#245): recovery must skip it, and an operator asking what
                    # became of the work must still find the row.
                    retired=bool(row["retired"]),
                )
            )
        return tuple(unresolved)

    def list_unreadable_claims(self) -> tuple[UnreadableClaim, ...]:
        unreadable: list[UnreadableClaim] = []
        for row in self._all_rows():
            try:
                decode_claim_text(row["payload"], source=row["run_key"])
            except PendingWorkClaimDecodeError as exc:
                unreadable.append(
                    UnreadableClaim(
                        run_key=row["run_key"],
                        session_name=row["session_name"],
                        issue_number=int(row["issue_number"]),
                        error=str(exc),
                        started_at=str(row["started_at"]),
                        # The decoder's verdict, carried rather than re-derived
                        # from the message text - that guess IS #209.
                        readability=exc.readability,
                        # Reported, not applied (#210): the escalation sweeps
                        # keep these rows whatever their disposition, and this
                        # is where a settlement on a row nothing can decode is
                        # observable at all.
                        parked=bool(row["parked"]),
                        # A row can be retired while readable and become
                        # unreadable afterwards - a pinned runtime meeting a
                        # payload written in a larger vocabulary is #209's
                        # ordinary operating condition (#245). The escalation
                        # sweep has to be able to see that a decision was
                        # already taken about it.
                        retired=bool(row["retired"]),
                    )
                )
        return tuple(unreadable)

    def retire_deferred_claim(self, work_key: str) -> None:
        """Drop the deferred row for work a launch transaction gave up on.

        An operator-retired row is excluded for the reason it is excluded from
        the relaunch supersession above (#245): this deletes work that a launch
        dropped, and a retired row is no longer work.
        """
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim "
                "WHERE work_key = ? AND deferred = 1 AND retired = 0",
                (work_key,),
            )

    def refresh_deferred_claim(
        self, work_key: str, claim: PendingWorkClaim
    ) -> bool:
        """Rewrite the deferred row's payload; report whether one existed.

        ``rowcount`` is the whole point (#6999 F1 round 2): an UPDATE that
        matches nothing is a successful statement and a failed commitment, and
        the caller spending a bounded retry budget has to be able to tell them
        apart.

        A retired row is not rewritten and does not count as one (#245): its
        payload is the preserved evidence of what was abandoned, and reporting
        an overwrite of it as a commit would be false twice over.
        """
        payload = encode_claim_text(claim)
        with self._write_lock, self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE pending_work_claim SET payload = ? "
                "WHERE work_key = ? AND deferred = 1 AND retired = 0",
                (payload, work_key),
            )
            return cursor.rowcount > 0

    def mark_deferred_by_run_key(self, run_key: str) -> None:
        """Keep the row; only a relaunch of the same work may retire it."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim SET deferred = 1 WHERE run_key = ?",
                (run_key,),
            )

    # -- operator retirement (#245) ----------------------------------------

    def retire_claim(self, request: ClaimRetirementRequest) -> RetiredClaimRecord:
        """Implement ``ports.pending_work_claim_retirement.retire_claim``.

        The transaction and the write lock are the store's, because the claim
        table is the store's; the checking and the two writes belong to the
        operation and live beside each other in
        :mod:`.pending_work_claim_retirement`. Nothing calls this on a schedule.
        """
        with self._write_lock, self._transaction() as conn:
            return _retire_claim(conn, request)

    def rehearse_claim_retirement(
        self, request: ClaimRetirementRequest
    ) -> RetiredClaimRecord:
        """Implement ``...rehearse_claim_retirement``: everything, then undone.

        The SAME call the real thing makes, rolled back rather than committed.
        Re-implementing the checks for a rehearsal would give an operator
        standing in front of an irreversible action a preview of a different
        decision procedure, which is worse than no preview at all.
        """
        with self._write_lock:
            conn = self._get_connection()
            try:
                return _retire_claim(conn, request)
            finally:
                # Unconditional: the refusal paths raise before writing and the
                # success path has just written both rows, and neither may
                # leave an open transaction behind for the next caller to
                # commit by accident.
                conn.rollback()

    def list_retired_claims(self) -> tuple[RetiredClaimRecord, ...]:
        """Implement ``ports.pending_work_claim_retirement.list_retired_claims``."""
        return retired_claim_records(self._get_connection())

    def quarantine_key_for(self, run: SessionRunAssets) -> str:
        """Run root PLUS the start instant the ORCHESTRATOR recorded for it.

        Run roots are named from a second-resolution timestamp and created with
        ``exist_ok``, so a replacement run of one session can land on the same
        directory; the start instant has sub-second precision and tells the
        generations apart (#6999 F12).

        It is read from this store's own row rather than from the run assets,
        because those are rebuilt from the worktree manifest the agent can
        write. Taking it from there would let a rewritten timestamp mint a fresh
        quarantine key on every scan, re-commenting forever. Only when no row
        exists at all - nothing was ever held for this run - does the run's own
        value stand in, and no claim can be lost in that case.
        """
        key = self.run_key_for(run)
        row = self._get_connection().execute(
            "SELECT started_at FROM pending_work_claim WHERE run_key = ?",
            (key,),
        ).fetchone()
        recorded = row["started_at"] if row else run.identity.started_at
        return quarantine_key_for_run(key, str(recorded))

    def run_key_for_path(self, run_dir: Path) -> str:
        """The key for a run root the orchestrator recorded, before any parsing.

        Discovery hands back a run directory long before ``SessionRunAssets``
        can be rebuilt from it, and a run whose assets fail to parse must still
        be recognised as live (#6999 F14).
        """
        return os.path.normpath(str(run_dir))

    def run_key_for(self, run: SessionRunAssets) -> str:
        """The run root, lexically normalised and never symlink-resolved.

        ``Path.resolve()`` would follow symlinks inside the agent-writable
        worktree, letting an agent retarget the key at another run (#6999 F11).
        ``os.path.normpath`` collapses ``.``/``..`` and separators without
        touching the filesystem, so the key is exactly the path the orchestrator
        allocated and the terminal registry recorded.
        """
        return self.run_key_for_path(run.run_dir)

    # -- shared needs-human provenance -------------------------------------

    def record_needs_human_cause(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO needs_human_cause (issue_number, cause, reason) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(issue_number, cause) DO UPDATE SET "
                "reason = excluded.reason",
                (issue_number, cause, reason),
            )

    def restart_needs_human_causes(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        """Replace every cause with this one, in a single transaction."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM needs_human_cause WHERE issue_number = ?",
                (issue_number,),
            )
            conn.execute(
                "INSERT INTO needs_human_cause (issue_number, cause, reason) "
                "VALUES (?, ?, ?)",
                (issue_number, cause, reason),
            )

    def needs_human_causes(self, issue_number: int) -> frozenset[str]:
        return frozenset(
            str(row["cause"])
            for row in self._get_connection().execute(
                "SELECT cause FROM needs_human_cause WHERE issue_number = ?",
                (issue_number,),
            )
        )

    def withdraw_needs_human_cause(self, issue_number: int, cause: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM needs_human_cause "
                "WHERE issue_number = ? AND cause = ?",
                (issue_number, cause),
            )

    def clear_needs_human_causes(self, issue_number: int) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM needs_human_cause WHERE issue_number = ?",
                (issue_number,),
            )

    # -- publication-refusal latch (#51) -----------------------------------

    def latch_publication_refusal(self, issue_number: int) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO publication_refusal_latch (issue_number) VALUES (?) "
                "ON CONFLICT(issue_number) DO NOTHING",
                (issue_number,),
            )

    def release_publication_refusal(self, issue_number: int) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM publication_refusal_latch WHERE issue_number = ?",
                (issue_number,),
            )

    def latched_publication_refusals(self) -> frozenset[int]:
        return frozenset(
            int(row["issue_number"])
            for row in self._get_connection().execute(
                "SELECT issue_number FROM publication_refusal_latch"
            )
        )

    # -- quarantine --------------------------------------------------------

    def record_quarantine(
        self,
        quarantine_key: str,
        *,
        run_key: str,
        session_name: str,
        issue_number: int,
        error: str,
        story: AnnouncedStory,
        work_kind: PendingWorkKind | None,
        settlement: ClaimSettlement,
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO pending_work_claim_quarantine "
                "(quarantine_key, run_key, session_name, issue_number, error, "
                "story, work_kind) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(quarantine_key) DO UPDATE SET "
                "error = excluded.error, releasing = 0, "
                "work_kind = excluded.work_kind, "
                # The announcement belongs to the story that produced it. A
                # changed story invalidates the comment and the event, so the
                # flag is cleared in the same statement that records the new
                # story - the two can never disagree (#6999 F6). An unchanged
                # story keeps it, which is what makes a re-observed quarantine
                # comment exactly once. Comparing ONE token is what lets an
                # input added to AnnouncedStory reach this rule untouched (#209).
                "announced = CASE WHEN pending_work_claim_quarantine.story "
                "IS excluded.story THEN pending_work_claim_quarantine.announced "
                "ELSE 0 END, "
                # The spent delivery budget belongs to it for the same reason
                # (#210). A corrected message is a NEW thing to deliver, so it
                # gets its own bound rather than inheriting an exhausted one -
                # and an unchanged story keeps every attempt it has spent, so
                # re-observation can never refund the bound.
                "announce_attempts = CASE WHEN pending_work_claim_quarantine.story "
                "IS excluded.story "
                "THEN pending_work_claim_quarantine.announce_attempts ELSE 0 END, "
                "story = excluded.story",
                (
                    quarantine_key,
                    run_key,
                    session_name,
                    issue_number,
                    error,
                    story.token,
                    work_kind.value if work_kind is not None else None,
                ),
            )
            # Both arms write. LEAVE is the INVERSE of PARK, not a no-op (#210):
            # a re-observation that now reads the payload cleanly quarantines
            # the same run for being unrestorable alone, and that escalation
            # promises an operator the next sweep re-queues the work once the
            # terminal stops - which a row still parked by the earlier verdict
            # would make false.
            self._set_claim_parked(
                conn,
                run_key,
                quarantine_key,
                parked=settlement is ClaimSettlement.PARK,
            )

    def read_quarantine(self, quarantine_key: str) -> QuarantineRecord | None:
        row = self._get_connection().execute(
            "SELECT quarantine_key, run_key, session_name, issue_number, error, "
            "label_state, announced, releasing, story, work_kind, "
            "announce_attempts "
            "FROM pending_work_claim_quarantine WHERE quarantine_key = ?",
            (quarantine_key,),
        ).fetchone()
        return _quarantine_record(row) if row else None

    def record_quarantine_label_state(
        self, quarantine_key: str, label_state: QuarantineLabelState
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET label_state = ? "
                "WHERE quarantine_key = ?",
                (label_state.value, quarantine_key),
            )

    def spend_quarantine_announcement_attempt(self, quarantine_key: str) -> None:
        """Charge the bound BEFORE the remote write it pays for (#210)."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine "
                "SET announce_attempts = announce_attempts + 1 "
                "WHERE quarantine_key = ?",
                (quarantine_key,),
            )

    def mark_quarantine_announced(self, quarantine_key: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET announced = 1 "
                "WHERE quarantine_key = ?",
                (quarantine_key,),
            )

    def mark_quarantine_releasing(self, quarantine_key: str) -> None:
        """Record that the cause is gone but cleanup has not committed yet."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET releasing = 1 "
                "WHERE quarantine_key = ?",
                (quarantine_key,),
            )

    def release_quarantine(self, quarantine_key: str) -> None:
        with self._write_lock, self._transaction() as conn:
            row = conn.execute(
                "SELECT run_key FROM pending_work_claim_quarantine "
                "WHERE quarantine_key = ?",
                (quarantine_key,),
            ).fetchone()
            conn.execute(
                "DELETE FROM pending_work_claim_quarantine WHERE quarantine_key = ?",
                (quarantine_key,),
            )
            if row is not None:
                # The exact undo of this quarantine's settlement, in the same
                # transaction (#210). A human who repairs the row must get
                # ordinary recovery back with it.
                self._set_claim_parked(
                    conn, str(row["run_key"]), quarantine_key, parked=False
                )

    @staticmethod
    def _set_claim_parked(
        conn: sqlite3.Connection,
        run_key: str,
        quarantine_key: str,
        *,
        parked: bool,
    ) -> None:
        """Settle, or un-settle, the ledger row ONE quarantine was recorded for.

        Runs in the CALLER's transaction (#210): a quarantine record and the
        disposition of the claim it names are one local fact, and a settlement
        that could commit without its record - or the reverse - is exactly the
        split this closes.

        The generation is checked rather than assumed. A run root is reusable,
        so the row sitting under it may belong to a REPLACEMENT run whose claim
        is perfectly live; parking that would take a healthy request out of the
        world on the strength of its predecessor's escalation, and un-parking it
        would release a settlement this quarantine never made. The check BUILDS
        the key the way every other caller does rather than taking the recorded
        one apart (#6999 F12).
        """
        row = conn.execute(
            "SELECT started_at FROM pending_work_claim WHERE run_key = ?",
            (run_key,),
        ).fetchone()
        if row is None:
            return
        if quarantine_key_for_run(run_key, str(row["started_at"])) != quarantine_key:
            return
        conn.execute(
            "UPDATE pending_work_claim SET parked = ? WHERE run_key = ?",
            (1 if parked else 0, run_key),
        )

    def list_quarantines(self) -> tuple[QuarantineRecord, ...]:
        return tuple(
            _quarantine_record(row)
            for row in self._get_connection().execute(
                "SELECT quarantine_key, run_key, session_name, issue_number, "
                "error, label_state, announced, releasing, story, work_kind, "
                "announce_attempts FROM pending_work_claim_quarantine"
            )
        )

    def quarantined_issue_numbers(self) -> frozenset[int]:
        """Issues held open by a quarantine whose cause is still present.

        Rows already being released are excluded: their block is on its way
        out, so nothing should treat them as a live reason to keep it.
        """
        return frozenset(
            int(row["issue_number"])
            for row in self._get_connection().execute(
                "SELECT issue_number FROM pending_work_claim_quarantine "
                "WHERE releasing = 0"
            )
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _identity_matches(row: sqlite3.Row, identity) -> bool:
        """Every recorded identity field, ``started_at`` included (#6999 F11)."""
        return (
            row["session_name"] == identity.session_name
            and row["run_id"] == identity.run_id
            and row["started_at"] == identity.started_at
        )

    def _all_rows(self) -> list[sqlite3.Row]:
        return list(
            self._get_connection().execute(
                "SELECT run_key, session_name, deferred, parked, retired, "
                "issue_number, started_at, payload FROM pending_work_claim"
            )
        )

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


__all__ = [
    "STORE_FILENAME",
    "PendingWorkClaimMigrationError",
    "SqlitePendingWorkClaimStore",
]
