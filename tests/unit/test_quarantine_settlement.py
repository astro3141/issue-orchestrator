"""Quarantining a run settles it locally, whatever the remote does (#210).

Escalating an unreadable claim used to commit two unrelated things through one
boolean. The record was written, and then a GitHub comment decided whether the
run was *finished with*: a comment that did not land left the claim held, active
and re-derivable, so the next sweep found the same trouble and wrote the same
comment again. Nothing bounded that. One undeliverable comment became 96 remote
writes on one issue in fifteen minutes.

The two halves are separated here:

* the LOCAL disposition - the quarantine record, and the settlement of the claim
  it names - commits first, in one transaction, and depends on nothing remote;
* DELIVERY of the operator's comment is its own bounded concern that is allowed
  to fail permanently, because by the time it runs the escalation already exists
  and the shared block is already on the issue.

Everything the earlier boundaries earned has to survive that: the block is still
re-asserted on every pass, it is still only removed by the quarantine that
demonstrably applied it (#6999 F12), a story that CHANGES is still corrected
(#209), and a repaired claim still releases its quarantine and gets its work
back.

The store here is the real SQLite one throughout. The proofs are about what
survives a restart, and a fake that keeps state in a dict cannot fail the way a
missing column can.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.claim_quarantine import (
    ANNOUNCEMENT_ATTEMPT_LIMIT,
    ClaimQuarantineOwner,
    QuarantineSubject,
)
from issue_orchestrator.domain.models import DiscoveredFailure, PendingTechLeadReview
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.pending_work_claim_store import (
    STORE_FILENAME,
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.execution.pending_work_codec import encode_claim
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.pending_work_claim_store import (
    AnnouncedStory,
    AnnouncementDelivery,
    ClaimReadability,
    ClaimSettlement,
    QuarantineCause,
    QuarantineLabelState,
)

# ---------------------------------------------------------------------------
# Fixtures. The incident's own row shape, never the incident's own database:
# #210 prohibits touching the live pilot claim, quarantine row or run directory.
# ---------------------------------------------------------------------------

_ISSUE = 23
_RUN_KEY = "/runs/tech-lead-23"
_STARTED_AT = "2026-08-07T00:00:00.123456+00:00"
_QUARANTINE_KEY = f"{_RUN_KEY}@{_STARTED_AT}"
_ERROR = "claim payload field 'flavor' is 'planning_investigation'"


def _tech_lead_claim() -> PendingWorkClaim:
    return PendingWorkClaim(
        PendingWorkKind.TECH_LEAD,
        PendingTechLeadReview(
            issue_number=_ISSUE,
            title="Tech lead review",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=_ISSUE, issue_title="Broke", failure_reason="failed"
            ),
        ),
    )


def _run_assets(tmp_path: Path):
    from tests.unit.session_run_helpers import make_session_run_assets

    worktree = tmp_path / "wt-tech-lead-23"
    worktree.mkdir(parents=True, exist_ok=True)
    return make_session_run_assets(worktree, session_name="tech-lead-23", run_id="r1")


class _Labels:
    """The quarantine's typed label ops, recorded rather than applied.

    ``announce_succeeds`` is the whole experiment: the incident's comment write
    reported failure on every attempt, and everything downstream of that is what
    #210 is about.
    """

    def __init__(self, *, announce_succeeds: bool = True) -> None:
        self.comments: list[str] = []
        self.acquired: list[int] = []
        self.released: list[int] = []
        self.announce_succeeds = announce_succeeds
        self.acquire_outcome = QuarantineLabelState.ACQUIRED

    def acquire_block(self, issue_number: int) -> QuarantineLabelState:
        self.acquired.append(issue_number)
        return self.acquire_outcome

    def release_block(self, issue_number: int) -> bool:
        self.released.append(issue_number)
        return True

    def announce(self, issue_number: int, comment: str) -> bool:
        self.comments.append(comment)
        return self.announce_succeeds


def _owner(
    tmp_path: Path,
    labels: _Labels,
    events: MagicMock | None = None,
    *,
    limit: int = ANNOUNCEMENT_ATTEMPT_LIMIT,
) -> ClaimQuarantineOwner:
    """An owner over the REAL store, as a restart would rebuild it."""
    return ClaimQuarantineOwner(
        store=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        labels=labels,
        events=events or MagicMock(),
        announcement_attempt_limit=limit,
    )


def _ledger_with_unreadable_claim(tmp_path: Path):
    """A real ledger holding one row this build cannot decode.

    Planted by rewriting a claim this build held, which is how #209's fixtures
    get a foreign payload in without reaching for the live database.
    """
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    run = _run_assets(tmp_path)
    store.hold_pending_work_claim(run, _tech_lead_claim(), issue_number=_ISSUE)
    _rewrite_payload(tmp_path, run, {"kind": "tech_lead"})
    return SqlitePendingWorkClaimStore.for_repo(tmp_path), run


def _rewrite_payload(tmp_path: Path, run, payload: dict[str, object]) -> None:
    conn = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
    conn.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        (json.dumps(payload), os.path.normpath(str(run.run_dir))),
    )
    conn.commit()
    conn.close()


def _quarantine_key_of(store, run) -> str:
    """The generation-anchored key this run's quarantine is recorded under."""
    return store.quarantine_key_for(run)


def _subject_for(store, run) -> QuarantineSubject:
    """The subject the ledger sweep derives from that row, as production does."""
    (unreadable,) = store.list_unreadable_claims()
    return QuarantineSubject.ended_run_with_unreadable_claim(unreadable)


def _bare_subject(
    readability: ClaimReadability = ClaimReadability.UNREADABLE_CORRUPT,
    cause: QuarantineCause = QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
) -> QuarantineSubject:
    """A subject with no ledger row behind it, for the delivery proofs."""
    return QuarantineSubject(
        quarantine_key=_QUARANTINE_KEY,
        run_key=_RUN_KEY,
        session_name="tech-lead-23",
        issue_number=_ISSUE,
        error=_ERROR,
        cause=cause,
        readability=readability,
    )


# ---------------------------------------------------------------------------
# Proof 1 - a permanently undeliverable announcement still settles the claim
# ---------------------------------------------------------------------------


def test_a_claim_settles_durably_when_the_announcement_never_commits(
    tmp_path: Path,
) -> None:
    """The invariant #210 names: local safety does not wait on a remote write.

    The comment fails on every attempt, exactly as it did in the incident. The
    quarantine must still be committed AND the claim must still be settled - and
    the claim must be settled by the time the FIRST announcement is even
    attempted, because that is what stops the sweep re-deriving it.
    """
    store, run = _ledger_with_unreadable_claim(tmp_path)
    labels = _Labels(announce_succeeds=False)

    _owner(tmp_path, labels).quarantine(_subject_for(store, run))

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (record,) = reopened.list_quarantines()
    assert record.issue_number == _ISSUE
    assert not record.announced  # the remote half never landed...
    # ...and the local half is complete anyway: the claim is settled where it
    # lies. The evidence is NOT destroyed to achieve that - the row is still
    # there, still unreadable, still nameable by the escalation an operator
    # will read.
    (still_unreadable,) = reopened.list_unreadable_claims()
    assert still_unreadable.quarantine_key == record.quarantine_key
    assert still_unreadable.parked  # ...and it has stopped being work
    # Asked of the row's own disposition, never of "is it missing from the
    # schedulable enumeration": a row this build cannot decode is missing from
    # that enumeration whether anything settled it or not, so the absence would
    # have proved the decode failure and nothing about #210.


def test_settlement_commits_before_anything_remote_is_attempted(
    tmp_path: Path,
) -> None:
    """Ordering is the property, not just the end state.

    A settlement that happened to run after a successful comment would look
    identical in a passing test and would restore the original defect the moment
    the comment failed. So the claim is asked about from INSIDE the announce
    call, which is the one moment the remote has not answered yet - and it is
    asked for the row's own recorded disposition, from a connection that has
    not seen this process's writes, so an uncommitted settlement would read as
    absent.
    """
    store, run = _ledger_with_unreadable_claim(tmp_path)
    settled_at_announce_time: list[bool] = []

    class _AsksMidFlight(_Labels):
        def announce(self, issue_number: int, comment: str) -> bool:
            probe = SqlitePendingWorkClaimStore.for_repo(tmp_path)
            (row,) = probe.list_unreadable_claims()
            settled_at_announce_time.append(row.parked)
            return super().announce(issue_number, comment)

    _owner(tmp_path, _AsksMidFlight(announce_succeeds=False)).quarantine(
        _subject_for(store, run)
    )

    assert settled_at_announce_time == [True]


def test_a_settled_claim_is_not_re_admitted_by_the_recovery_sweep(
    tmp_path: Path,
) -> None:
    """"No longer schedulable" means the sweep will not hand it back.

    The ledger sweep re-admits every unresolved row whose run is not live. A
    quarantined run's row is exactly that shape, so without settlement the only
    thing keeping the work out of a queue is that this build cannot decode it -
    which is a property of the build, not a decision anybody made.
    """
    from issue_orchestrator.control.in_flight_work import InFlightWorkLedger

    store, run = _ledger_with_unreadable_claim(tmp_path)
    labels = _Labels(announce_succeeds=False)
    owner = _owner(tmp_path, labels)
    owner.quarantine(_subject_for(store, run))
    # A build that CAN read the payload now looks at the same row.
    _rewrite_payload(tmp_path, run, encode_claim(_tech_lead_claim()))
    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    state = MagicMock()
    state.in_flight_work = []
    state.active_sessions = []

    readmitted = InFlightWorkLedger(state, reopened).recover_unresolved(
        MagicMock(), live_quarantine_keys=frozenset({_quarantine_key_of(reopened, run)})
    )

    assert readmitted == 0
    # The row is enumerable to that build - it decodes now - and the sweep
    # still declined it, because the settlement says so rather than because
    # the payload was unreadable.
    (settled,) = reopened.list_unresolved_claims()
    assert not settled.re_admissible


# ---------------------------------------------------------------------------
# Proof 2 - delivery is bounded, and running out of it is a durable, seen state
# ---------------------------------------------------------------------------


def test_remote_attempts_stop_at_the_bound(tmp_path: Path) -> None:
    """The 96 writes, prevented. Sweeps go on; remote mutation does not."""
    labels = _Labels(announce_succeeds=False)
    owner = _owner(tmp_path, labels, limit=3)
    subject = _bare_subject()

    for _ in range(10):
        owner.quarantine(subject)

    assert len(labels.comments) == 3
    # The block, by contrast, IS re-asserted on every pass - it is idempotent,
    # and a quarantined terminal is deliberately absent from active_sessions, so
    # an owner that lifted the label must find it put back (#6999 F3).
    assert labels.acquired == [_ISSUE] * 10


def test_running_out_of_attempts_is_recorded_and_published(tmp_path: Path) -> None:
    """A quarantine nobody can be told about must still be seen somewhere.

    The comment is the channel that failed, so the event is the one left. It is
    published exactly once, on the pass that spends the last attempt: an
    operator-facing signal that repeats every 30 seconds is the same unbounded
    behaviour in a different medium.
    """
    events = MagicMock()
    owner = _owner(tmp_path, _Labels(announce_succeeds=False), events, limit=2)
    subject = _bare_subject()

    for _ in range(5):
        owner.quarantine(subject)

    published = [call.args[0] for call in events.publish.call_args_list]
    exhausted = [
        e for e in published if e.name == EventName.SESSION_QUARANTINE_UNANNOUNCED
    ]
    assert len(exhausted) == 1
    assert exhausted[0].data["issue_number"] == _ISSUE
    assert exhausted[0].data["attempts"] == 2
    assert exhausted[0].data["readability"] == subject.readability.value
    # ...and the escalation event stays withheld: nobody was told (#6999 F6).
    assert not [
        e for e in published if e.name == EventName.SESSION_CLAIM_UNREADABLE
    ]


def test_the_spent_budget_is_durable(tmp_path: Path) -> None:
    """A bound a restart refunds is not a bound."""
    _owner(tmp_path, _Labels(announce_succeeds=False), limit=3).quarantine(
        _bare_subject()
    )

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (record,) = reopened.list_quarantines()

    assert record.announce_attempts == 1
    assert (
        record.delivery(_bare_subject().story, limit=3)
        is AnnouncementDelivery.PENDING
    )
    assert (
        record.delivery(_bare_subject().story, limit=1)
        is AnnouncementDelivery.EXHAUSTED
    )


def test_the_attempt_is_charged_before_the_write_it_pays_for(
    tmp_path: Path,
) -> None:
    """A process that dies mid-write must not refund its own attempt.

    Charging afterwards means a crash loop that always dies inside the GitHub
    call spends nothing, and the bound never arrives - which is the unbounded
    structure again, reachable by a route no counter would notice.
    """
    charged: list[int] = []

    class _DiesMidWrite(_Labels):
        def announce(self, issue_number: int, comment: str) -> bool:
            probe = SqlitePendingWorkClaimStore.for_repo(tmp_path)
            (record,) = probe.list_quarantines()
            charged.append(record.announce_attempts)
            raise RuntimeError("killed inside the remote write")

    with pytest.raises(RuntimeError):
        _owner(tmp_path, _DiesMidWrite(), limit=3).quarantine(_bare_subject())

    assert charged == [1]
    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines()
    assert record.announce_attempts == 1


# ---------------------------------------------------------------------------
# Proof 3 - a delivered announcement behaves exactly as it did
# ---------------------------------------------------------------------------


def test_a_successful_announcement_is_unchanged(tmp_path: Path) -> None:
    events = MagicMock()
    labels = _Labels()
    owner = _owner(tmp_path, labels, events)
    subject = _bare_subject()

    owner.quarantine(subject)

    assert len(labels.comments) == 1
    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines()
    assert record.announces(subject.story)
    assert (
        record.delivery(subject.story, limit=1) is AnnouncementDelivery.DELIVERED
    )
    published = [call.args[0] for call in events.publish.call_args_list]
    assert [e.name for e in published] == [EventName.SESSION_CLAIM_UNREADABLE]


def test_a_changed_story_gets_a_fresh_budget(tmp_path: Path) -> None:
    """A correction is a new thing to say, not a continuation of a failed one.

    Otherwise a story that exhausted its attempts would silence the story that
    replaces it, and the operator would keep the version the orchestrator has
    stopped believing (#209).
    """
    labels = _Labels(announce_succeeds=False)
    owner = _owner(tmp_path, labels, limit=1)

    owner.quarantine(_bare_subject(ClaimReadability.UNEXAMINED))
    owner.quarantine(_bare_subject(ClaimReadability.UNEXAMINED))  # bound spent
    assert len(labels.comments) == 1

    labels.announce_succeeds = True
    owner.quarantine(_bare_subject(ClaimReadability.UNREADABLE_NEWER))

    assert len(labels.comments) == 2
    assert "NEWER build" in labels.comments[1]
    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines()
    assert record.announces(
        AnnouncedStory(
            QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
            ClaimReadability.UNREADABLE_NEWER,
        )
    )


# ---------------------------------------------------------------------------
# Proof 4 - a restart resumes neither the announcing nor the silence
# ---------------------------------------------------------------------------


def test_a_restart_does_not_resume_unbounded_announcing(tmp_path: Path) -> None:
    """Every owner below is a fresh process over the same database."""
    labels = _Labels(announce_succeeds=False)
    subject = _bare_subject()

    for _ in range(6):
        _owner(tmp_path, labels, limit=2).quarantine(subject)

    assert len(labels.comments) == 2


def test_a_restart_keeps_the_operator_signal_a_failed_comment_lost(
    tmp_path: Path,
) -> None:
    """Silence is not an acceptable resting state for an exhausted delivery.

    Three things survive: the block on the issue, the durable record naming the
    run and its story, and the fact that its comment was never delivered.
    """
    labels = _Labels(announce_succeeds=False)
    subject = _bare_subject()
    for _ in range(3):
        _owner(tmp_path, labels, limit=2).quarantine(subject)

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (record,) = reopened.list_quarantines()

    assert reopened.quarantined_issue_numbers() == frozenset({_ISSUE})
    assert record.block_is_ours  # ...so the issue is still blocked
    assert record.story == subject.story
    assert not record.announced
    assert (
        record.delivery(subject.story, limit=2) is AnnouncementDelivery.EXHAUSTED
    )


# ---------------------------------------------------------------------------
# Proof 5 - repeated sweeps over one row: one comment, endless idempotent blocks
# ---------------------------------------------------------------------------


def test_repeated_sweeps_over_one_unreadable_claim_comment_once(
    tmp_path: Path,
) -> None:
    store, run = _ledger_with_unreadable_claim(tmp_path)
    labels = _Labels()
    owner = _owner(tmp_path, labels)

    for _ in range(20):
        owner.quarantine(_subject_for(store, run))

    assert len(labels.comments) == 1
    assert labels.acquired == [_ISSUE] * 20


def test_block_reassertion_stays_idempotent_and_still_records_ownership(
    tmp_path: Path,
) -> None:
    """#6999 F3, unchanged by the delivery bound.

    A block this quarantine found already present, then had removed underneath
    it, and has now re-applied itself, becomes ours to take off. Nothing about
    running out of comment attempts may touch that.
    """
    labels = _Labels(announce_succeeds=False)
    labels.acquire_outcome = QuarantineLabelState.PREEXISTING
    owner = _owner(tmp_path, labels, limit=1)
    subject = _bare_subject()

    owner.quarantine(subject)
    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines()
    assert not record.block_is_ours

    labels.acquire_outcome = QuarantineLabelState.ACQUIRED
    owner.quarantine(subject)  # the bound is spent; the block is still asserted

    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines()
    assert record.block_is_ours
    assert len(labels.comments) == 1


# ---------------------------------------------------------------------------
# Proof 6 - the label ownership contract, and the way out of a quarantine
# ---------------------------------------------------------------------------


def test_a_block_found_already_present_is_never_removed_by_this_quarantine(
    tmp_path: Path,
) -> None:
    labels = _Labels(announce_succeeds=False)
    labels.acquire_outcome = QuarantineLabelState.PREEXISTING
    owner = _owner(tmp_path, labels, limit=1)

    owner.quarantine(_bare_subject())
    owner.reconcile_released(frozenset())

    assert labels.released == []
    assert SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines() == ()


def test_releasing_a_quarantine_gives_its_claim_back_to_recovery(
    tmp_path: Path,
) -> None:
    """Settlement lasts as long as the quarantine does, and not one sweep longer.

    A human repairs the unreadable row; the quarantine releases itself and the
    work must become recoverable again. A settlement that outlived its
    quarantine would convert a resolved escalation into permanently lost work.
    """
    store, run = _ledger_with_unreadable_claim(tmp_path)
    labels = _Labels()
    owner = _owner(tmp_path, labels)
    owner.quarantine(_subject_for(store, run))
    (settled,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_unreadable_claims()
    assert settled.parked

    _rewrite_payload(tmp_path, run, encode_claim(_tech_lead_claim()))
    owner.reconcile_released(frozenset())

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    assert reopened.list_quarantines() == ()
    assert labels.released == [_ISSUE]
    (recovered,) = reopened.list_unresolved_claims()
    assert recovered.claim == _tech_lead_claim()
    assert recovered.re_admissible  # the settlement went with the quarantine


def test_a_parked_row_that_becomes_readable_under_a_live_run_stays_blocked(
    tmp_path: Path,
) -> None:
    """The transition a parked row must survive: readable, but still in trouble.

    A pinned runtime lagging ``main`` is a NORMAL operating condition (#209),
    so a payload no build here could read becoming readable is an ordinary
    event, not an exotic one. When it happens under a run that is STILL live
    and STILL unrestorable, the trouble has not gone anywhere - only its name
    has changed, from "neither trackable nor identifiable" to "unrestorable".

    Settling that row must not make it invisible to the sweep that escalates
    live trouble. It did: the escalation loop and the re-admission loop read
    one enumeration, the parked row was filtered out of both, and a sweep that
    found nothing to raise released the quarantine and took ``needs-human`` off
    an issue whose terminal nobody could track was still running - re-applying
    the label and posting a fresh comment one tick later.
    """
    from issue_orchestrator.control.in_flight_work import (
        ClaimRestoration,
        InFlightWorkLedger,
    )

    store, run = _ledger_with_unreadable_claim(tmp_path)
    labels = _Labels()
    owner = _owner(tmp_path, labels)
    state = MagicMock()
    state.in_flight_work = []
    state.active_sessions = []

    def sweep() -> int:
        """One pass over a run discovery finds but restoration cannot rebuild."""
        ledger = InFlightWorkLedger(
            state, SqlitePendingWorkClaimStore.for_repo(tmp_path)
        )
        accounting = ledger.account_for_discovered(
            [{"run_dir": str(run.run_dir)}], ClaimRestoration((), ()), owner
        )
        return ledger.recover_unresolved(
            owner,
            live_run_keys=accounting.live_run_keys,
            live_quarantine_keys=accounting.live_quarantine_keys,
        )

    sweep()
    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_quarantines()
    assert record.story == AnnouncedStory(
        QuarantineCause.RUN_UNRESTORABLE_CLAIM_UNREADABLE,
        ClaimReadability.UNREADABLE_CORRUPT,
    )

    # The runtime is promoted; the same bytes now decode. Nothing else changed:
    # the terminal is alive and the orchestrator still cannot track it.
    _rewrite_payload(tmp_path, run, encode_claim(_tech_lead_claim()))
    readmitted = sweep()

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    # The block never came off, so nothing re-applied it and no second comment
    # was written for a story the operator had already been told.
    assert labels.released == []
    assert reopened.quarantined_issue_numbers() == frozenset({_ISSUE})
    # ...under the cause that is now true, not the one that was.
    (record,) = reopened.list_quarantines()
    assert record.story == AnnouncedStory(
        QuarantineCause.RUN_UNRESTORABLE, ClaimReadability.READABLE
    )
    # And the settlement moved with it. That escalation promises an operator
    # that stopping the terminal lets the next sweep re-queue the work, which a
    # row still parked by the previous verdict would make false.
    (unparked,) = reopened.list_unresolved_claims()
    assert unparked.re_admissible
    # Not re-queued NOW, though - the terminal is still running it.
    assert readmitted == 0


# ---------------------------------------------------------------------------
# Which claims settle at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "readability",
    [
        ClaimReadability.UNREADABLE_NEWER,
        ClaimReadability.UNREADABLE_CORRUPT,
        ClaimReadability.UNEXAMINED,
    ],
)
def test_every_unreadable_verdict_settles_the_claim(
    readability: ClaimReadability,
) -> None:
    """Newer, damaged, or never examined: none of them is schedulable work.

    #210 must behave correctly for ANY unreadable claim, whatever its cause -
    the typed classification is leaf 1's question, not this one's.
    """
    assert _bare_subject(readability).settlement is ClaimSettlement.PARK


def test_a_readable_claim_behind_an_unrestorable_run_is_left_alone(
    tmp_path: Path,
) -> None:
    """The one quarantine that must NOT settle its claim (#6999 F14).

    Its record reads perfectly and its escalation promises an operator that
    stopping the terminal lets the next sweep re-queue the work automatically.
    Parking it would break that promise and strand recoverable work behind a
    block instead.
    """
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    run = _run_assets(tmp_path)
    store.hold_pending_work_claim(run, _tech_lead_claim(), issue_number=_ISSUE)
    (unresolved,) = store.list_unresolved_claims()
    subject = QuarantineSubject.unrestorable_live_run(unresolved)
    assert subject.settlement is ClaimSettlement.LEAVE

    _owner(tmp_path, _Labels()).quarantine(subject)

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (left_alone,) = reopened.list_unresolved_claims()
    assert left_alone.re_admissible


def test_a_replacement_run_reusing_the_directory_keeps_its_own_claim(
    tmp_path: Path,
) -> None:
    """A settlement is anchored on the generation, not the run root (#6999 F12).

    Run roots are created with ``exist_ok`` and named from a second-resolution
    timestamp, so a replacement run of one session can land on the same
    directory. Its predecessor's quarantine must not park its live claim.
    """
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    run = _run_assets(tmp_path)
    store.hold_pending_work_claim(run, _tech_lead_claim(), issue_number=_ISSUE)
    run_key = store.run_key_for(run)
    stale = QuarantineSubject(
        quarantine_key=f"{run_key}@1999-01-01T00:00:00+00:00",
        run_key=run_key,
        session_name="tech-lead-23",
        issue_number=_ISSUE,
        error=_ERROR,
        cause=QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
        readability=ClaimReadability.UNREADABLE_CORRUPT,
    )

    _owner(tmp_path, _Labels()).quarantine(stale)

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (untouched,) = reopened.list_unresolved_claims()
    assert untouched.re_admissible


# ---------------------------------------------------------------------------
# The database an earlier build wrote
# ---------------------------------------------------------------------------


def test_an_older_ledger_learns_to_settle_without_losing_a_claim(
    tmp_path: Path,
) -> None:
    """The added columns are additive on both tables (#6999 F13, #210).

    A claim table without ``parked`` reads exactly as one whose rows have never
    been parked, which is what an unquarantined claim's state already was - so
    nothing has to be rebuilt and no row may be lost teaching it.
    """
    db_path = state_dir(tmp_path) / STORE_FILENAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE pending_work_claim (
            run_key TEXT PRIMARY KEY,
            work_key TEXT NOT NULL,
            deferred INTEGER NOT NULL DEFAULT 0,
            session_name TEXT NOT NULL,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE pending_work_claim_quarantine (
            quarantine_key TEXT PRIMARY KEY,
            run_key TEXT NOT NULL,
            session_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            error TEXT NOT NULL,
            label_state TEXT NOT NULL DEFAULT 'unknown',
            announced INTEGER NOT NULL DEFAULT 0,
            releasing INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO pending_work_claim_quarantine
            (quarantine_key, run_key, session_name, issue_number, error,
             label_state, announced, releasing)
        VALUES ('/runs/a@t1', '/runs/a', 'issue-7', 7, 'legacy', 'acquired', 1, 0);
        """
    )
    conn.execute(
        "INSERT INTO pending_work_claim (run_key, work_key, deferred, "
        "session_name, run_id, started_at, issue_number, payload) "
        "VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
        (
            "/runs/a",
            _tech_lead_claim().work_key(),
            "issue-7",
            "r1",
            "t1",
            7,
            json.dumps(encode_claim(_tech_lead_claim())),
        ),
    )
    conn.commit()
    conn.close()

    store = SqlitePendingWorkClaimStore(db_path)

    (carried,) = store.list_unresolved_claims()
    assert carried.run_key == "/runs/a"  # the claim survived the upgrade
    (quarantine,) = store.list_quarantines()
    assert quarantine.announce_attempts == 0  # a fresh budget, not a spent one
    assert quarantine.block_is_ours  # ...and the block it owns is undisturbed

    store.record_quarantine(
        "/runs/a@t1",
        run_key="/runs/a",
        session_name="issue-7",
        issue_number=7,
        error="still unreadable",
        story=AnnouncedStory(
            QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
            ClaimReadability.UNREADABLE_CORRUPT,
        ),
        work_kind=None,
        settlement=ClaimSettlement.PARK,
    )

    (settled,) = store.list_unresolved_claims()
    assert not settled.re_admissible
    store.release_quarantine("/runs/a@t1")
    (given_back,) = store.list_unresolved_claims()
    assert given_back.re_admissible
