"""A claim written by a NEWER build is not a corrupt claim (#209).

The orchestrator pins trusted runtimes deliberately while ``main`` advances, so
an older runtime meeting durable state written by a newer one is a designed-for
operating condition. The pending-work claim decoder had exactly one way to say
"no", and it said it in the vocabulary of damage: an operator was told an intact
artifact "cannot be recovered".

Two mechanisms had to be distinguished, not one:

* the schema VERSION gate, which fires when the payload's shape changes;
* a persisted enum's VALUE SPACE growing while the shape stays identical.
  ``TechLeadSessionFlavor.PLANNING_INVESTIGATION`` was added in #136 without a
  version bump - correctly, because the gate is an equality check and a bump
  would have made every already-stored claim unreadable - so the old runtime
  passed the version check and then failed at enum coercion, into the generic
  branch.

These tests pin the typed verdict that replaces the single untyped error, and
pin that neither the newer verdict nor a read that reached no verdict at all
ever reaches an operator dressed as corruption. They also pin that a verdict
which CHANGES between sweeps posts the correction, since an announcement
remembered by halves can only be re-asserted.

``monkeypatch.setattr(codec, "TechLeadSessionFlavor", ...)`` below is a
deliberate exception to ``tests/AGENTS.md``'s steer away from module internals.
Standing in for a build whose enum value space is genuinely smaller is what the
proof requires, and no injection seam exists for a type the decoder names
directly.
"""

from __future__ import annotations

import json
import os
import sqlite3
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.claim_quarantine import (
    ClaimQuarantineOwner,
    QuarantineSubject,
)
from issue_orchestrator.control.in_flight_work import QuarantinedSession
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    PendingTechLeadReview,
    PendingValidationRetry,
)
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.execution import pending_work_codec as codec
from issue_orchestrator.execution.pending_work_codec import (
    CLAIM_SCHEMA_VERSION,
    CorruptPendingWorkClaimError,
    NewerPendingWorkClaimError,
    PendingWorkClaimDecodeError,
    decode_claim,
    decode_claim_text,
    encode_claim,
    encode_claim_text,
)
from issue_orchestrator.ports.pending_work_claim_store import (
    AnnouncedStory,
    ClaimReadability,
    QuarantineCause,
    QuarantineLabelState,
    UnreadableClaim,
)

# ---------------------------------------------------------------------------
# Fixtures are SYNTHESIZED. Nothing here reads the live claim store.
# ---------------------------------------------------------------------------


class _OlderTechLeadSessionFlavor(Enum):
    """``TechLeadSessionFlavor`` exactly as a build from before #136 carries it.

    The only faithful way to stand in for a pinned older runtime: the incident
    payload is well-formed for the build that wrote it, and unreadable only
    because the READER's value space is smaller.
    """

    BATCH_REVIEW = "batch_review"
    FAILURE_INVESTIGATION = "failure_investigation"
    HEALTH_REVIEW = "health_review"


def _tech_lead_claim(
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.BATCH_REVIEW,
) -> PendingWorkClaim:
    return PendingWorkClaim(
        PendingWorkKind.TECH_LEAD,
        PendingTechLeadReview(
            issue_number=23,
            title="Tech lead review",
            flavor=flavor,
            # Required for, and only for, a failure investigation.
            failure=(
                DiscoveredFailure(
                    issue_number=23, issue_title="Broke", failure_reason="failed"
                )
                if flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
                else None
            ),
        ),
    )


def _validation_retry_claim() -> PendingWorkClaim:
    return PendingWorkClaim(
        PendingWorkKind.VALIDATION_RETRY,
        PendingValidationRetry(
            issue_number=23,
            issue_title="Retry me",
            agent_label="agent:backend",
            worktree_path="/wt/issue-23",
            branch_name="23-retry",
            original_prompt="do the thing",
            validation_error="pytest failed",
            validation_error_file=None,
            retry_count=2,
            source_task=TaskKind.CODE,
        ),
    )


def _incident_payload() -> dict[str, object]:
    """The #208 payload, verbatim: version 1, tech_lead, planning_investigation."""
    return {
        "schema_version": 1,
        "kind": "tech_lead",
        "request": {
            "issue_number": 23,
            "title": "Tech lead review",
            "flavor": "planning_investigation",
            "failure": None,
            "problem_cohort": [],
            "retryable_launch_failures": 0,
        },
    }


def _readability_of_decode(payload: object) -> ClaimReadability:
    """The decoder's three-way verdict on ``payload``."""
    try:
        decode_claim(payload)
    except PendingWorkClaimDecodeError as exc:
        return exc.readability
    return ClaimReadability.READABLE


# ---------------------------------------------------------------------------
# Proof 1 - an unknown but well-formed enum member is NEWER, not corrupt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, unknown",
    [
        # Every enum this artifact persists. Each one's value space can grow on
        # ``main`` without the payload's shape changing by a single byte, which
        # is precisely the failure the version gate cannot see.
        ("kind", "cross_repo_review"),
        # NOT ``planning_investigation``: this build carries that one now, and
        # the incident member gets its own faithful proof further down.
        ("flavor", "cross_repo_investigation"),
        ("source_task", "documentation"),
    ],
)
def test_an_unknown_but_well_formed_enum_member_reads_as_newer(
    field: str, unknown: str
) -> None:
    """A value this build's enum lacks means the WRITER knew more, not that the
    record is damaged."""
    if field == "kind":
        payload = encode_claim(_tech_lead_claim())
        payload[field] = unknown
    elif field == "flavor":
        payload = encode_claim(_tech_lead_claim())
        payload["request"][field] = unknown
    else:
        payload = encode_claim(_validation_retry_claim())
        payload["request"][field] = unknown

    with pytest.raises(NewerPendingWorkClaimError) as raised:
        decode_claim(payload)

    assert raised.value.readability is ClaimReadability.UNREADABLE_NEWER
    # And it is still catchable as the one thing every existing caller catches.
    assert isinstance(raised.value, PendingWorkClaimDecodeError)


def test_the_newer_verdict_does_not_swallow_a_wrongly_typed_enum_field() -> None:
    """"Unknown member" is a STRING this build does not carry.

    A number, an object or ``null`` in an enum field is a shape the encoder
    never produces, so no future build wrote it - reporting that as "a newer
    build will read this" would promise a recovery that can never happen.
    """
    for broken in (7, None, {"flavor": "batch_review"}, ["batch_review"]):
        payload = encode_claim(_tech_lead_claim())
        payload["request"]["flavor"] = broken
        assert (
            _readability_of_decode(payload) is ClaimReadability.UNREADABLE_CORRUPT
        ), broken


# ---------------------------------------------------------------------------
# Proof 2 - genuinely malformed payloads are still corrupt
# ---------------------------------------------------------------------------


def _without(payload: dict[str, object], key: str) -> dict[str, object]:
    del payload[key]
    return payload


@pytest.mark.parametrize(
    "name, payload",
    [
        ("not an object at all", ["kind", "tech_lead"]),
        ("missing kind", _without(encode_claim(_tech_lead_claim()), "kind")),
        ("kind of the wrong type", {**encode_claim(_tech_lead_claim()), "kind": 4}),
        (
            "missing schema version",
            _without(encode_claim(_tech_lead_claim()), "schema_version"),
        ),
        (
            "schema version that is not a number",
            {**encode_claim(_tech_lead_claim()), "schema_version": "1"},
        ),
        (
            "schema version that is a bool",
            {**encode_claim(_tech_lead_claim()), "schema_version": True},
        ),
        (
            "non-dict request",
            {**encode_claim(_tech_lead_claim()), "request": "tech_lead"},
        ),
        (
            "request missing a required field",
            {
                **encode_claim(_tech_lead_claim()),
                "request": {"flavor": "batch_review"},
            },
        ),
        (
            "request field of the wrong type",
            {
                **encode_claim(_tech_lead_claim()),
                "request": {
                    **encode_claim(_tech_lead_claim())["request"],
                    "issue_number": "twenty-three",
                },
            },
        ),
    ],
)
def test_a_malformed_payload_is_still_classified_corrupt(
    name: str, payload: object
) -> None:
    """Nothing here is a shape any build ever wrote."""
    with pytest.raises(CorruptPendingWorkClaimError) as raised:
        decode_claim(payload)
    assert raised.value.readability is ClaimReadability.UNREADABLE_CORRUPT, name


def test_stored_text_that_is_not_json_is_corrupt() -> None:
    """The JSON framing is part of the artifact, and it is classified too.

    Each store used to unwrap the text itself and invent a verdict for this,
    which is the per-call-site guessing #209 removes.
    """
    for text in ("{not json", "", b"{}", None):
        with pytest.raises(CorruptPendingWorkClaimError) as raised:
            decode_claim_text(text, source="/runs/tech-lead-23")
        assert raised.value.readability is ClaimReadability.UNREADABLE_CORRUPT
        assert "/runs/tech-lead-23" in str(raised.value)


def test_stored_text_round_trips_through_one_canonical_spelling() -> None:
    claim = _tech_lead_claim()

    text = encode_claim_text(claim)

    assert decode_claim_text(text, source="/runs/tech-lead-23") == claim
    # Stable enough to compare as TEXT, which the idempotent re-hold needs.
    assert encode_claim_text(decode_claim_text(text, source="x")) == text


def test_stored_text_carries_the_newer_verdict_out_of_the_framing_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec, "TechLeadSessionFlavor", _OlderTechLeadSessionFlavor)

    with pytest.raises(NewerPendingWorkClaimError) as raised:
        decode_claim_text(json.dumps(_incident_payload()), source="/runs/x")

    assert raised.value.readability is ClaimReadability.UNREADABLE_NEWER


# ---------------------------------------------------------------------------
# Proof 3 - a higher schema version is still newer, unchanged from before
# ---------------------------------------------------------------------------


def test_a_higher_schema_version_still_reads_as_newer() -> None:
    payload = encode_claim(_tech_lead_claim())
    payload["schema_version"] = CLAIM_SCHEMA_VERSION + 1

    with pytest.raises(NewerPendingWorkClaimError) as raised:
        decode_claim(payload)

    assert raised.value.readability is ClaimReadability.UNREADABLE_NEWER
    assert str(CLAIM_SCHEMA_VERSION + 1) in str(raised.value)


def test_the_version_gate_is_not_bumped_when_an_enum_merely_gains_a_member() -> None:
    """The bump-discipline alternative, pinned as the wrong tool (#209).

    The gate is an EQUALITY check, so raising it to cover a value-space growth
    makes every claim already on disk unreadable to the build that raised it -
    turning a forward-compatibility problem into a backward-compatibility one,
    on the only durable record of work already in flight. This test exists so
    that a future author who reaches for the bump sees why it is not the fix.
    """
    stored_by_this_build = encode_claim(_tech_lead_claim())

    assert stored_by_this_build["schema_version"] == CLAIM_SCHEMA_VERSION
    # A bump would make the payload above - written moments ago, perfectly
    # intact - unreadable, and it would be classified NEWER by a build that
    # is in fact OLDER than nothing at all.
    bumped = {**stored_by_this_build, "schema_version": CLAIM_SCHEMA_VERSION + 1}
    assert _readability_of_decode(bumped) is ClaimReadability.UNREADABLE_NEWER
    # ...whereas the value-space growth is caught without touching the version.
    grown = encode_claim(_tech_lead_claim())
    grown["request"]["flavor"] = "a_member_this_build_does_not_have"
    assert grown["schema_version"] == CLAIM_SCHEMA_VERSION
    assert _readability_of_decode(grown) is ClaimReadability.UNREADABLE_NEWER


# ---------------------------------------------------------------------------
# Proof 5 - the exact incident payload, read by a build that lacks the member
# ---------------------------------------------------------------------------


def test_the_incident_payload_reads_as_newer_on_a_build_lacking_the_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``schema_version: 1``, ``kind: tech_lead``, ``flavor:
    planning_investigation`` - the pilot claim of #208, synthesized.

    Decoded by a runtime whose ``TechLeadSessionFlavor`` predates #136, this is
    the state the operator was told could not be recovered.
    """
    monkeypatch.setattr(codec, "TechLeadSessionFlavor", _OlderTechLeadSessionFlavor)
    payload = _incident_payload()

    with pytest.raises(NewerPendingWorkClaimError) as raised:
        decode_claim(payload)

    assert raised.value.readability is ClaimReadability.UNREADABLE_NEWER
    assert "planning_investigation" in str(raised.value)
    # The version gate structurally could not have caught it: the payload's
    # version is exactly the one this build writes.
    assert payload["schema_version"] == CLAIM_SCHEMA_VERSION


def test_the_same_payload_reads_cleanly_on_a_build_that_has_the_member() -> None:
    """The other half of "the artifact is intact": THIS build reads it."""
    claim = decode_claim(_incident_payload())

    assert claim.kind is PendingWorkKind.TECH_LEAD
    assert isinstance(claim.request, PendingTechLeadReview)
    assert claim.request.flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION


# ---------------------------------------------------------------------------
# Proof 6 - a currently supported claim round-trips unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flavor", list(TechLeadSessionFlavor))
def test_every_supported_tech_lead_flavor_round_trips_unchanged(
    flavor: TechLeadSessionFlavor,
) -> None:
    claim = _tech_lead_claim(flavor)

    restored = decode_claim(encode_claim(claim))

    assert restored == claim
    assert encode_claim(restored) == encode_claim(claim)


@pytest.mark.parametrize("source_task", [TaskKind.CODE, TaskKind.REWORK])
def test_a_validation_retry_round_trips_unchanged(source_task: TaskKind) -> None:
    from dataclasses import replace

    claim = PendingWorkClaim(
        PendingWorkKind.VALIDATION_RETRY,
        replace(_validation_retry_claim().request, source_task=source_task),
    )

    restored = decode_claim(encode_claim(claim))

    assert restored == claim


# ---------------------------------------------------------------------------
# The verdict has to survive the trip to the operator, not just the decoder
# ---------------------------------------------------------------------------


def _run_assets(tmp_path: Path):
    from tests.unit.session_run_helpers import make_session_run_assets

    worktree = tmp_path / "wt-tech-lead-23"
    worktree.mkdir(parents=True, exist_ok=True)
    return make_session_run_assets(worktree, session_name="tech-lead-23", run_id="r1")


def _store_with_payload(tmp_path: Path, payload: dict[str, object]):
    """A REAL ledger holding one row whose payload is the synthesized fixture.

    The row is planted by rewriting a claim this build held, which is the only
    way to get a foreign payload into the store without reaching for the live
    database (#209 prohibits touching it).
    """
    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    run = _run_assets(tmp_path)
    store.hold_pending_work_claim(run, _tech_lead_claim(), issue_number=23)
    conn = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
    conn.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        (json.dumps(payload), os.path.normpath(str(run.run_dir))),
    )
    conn.commit()
    conn.close()
    return SqlitePendingWorkClaimStore.for_repo(tmp_path), run


def test_the_ledger_reports_a_newer_row_as_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_unreadable_claims`` carries the decoder's verdict, not a sentence.

    The recovery sweep is where the incident's operator story was written, and
    it had nothing but an error string to write it from.
    """
    store, _ = _store_with_payload(tmp_path, _incident_payload())
    monkeypatch.setattr(codec, "TechLeadSessionFlavor", _OlderTechLeadSessionFlavor)

    unreadable = store.list_unreadable_claims()

    assert len(unreadable) == 1
    assert unreadable[0].readability is ClaimReadability.UNREADABLE_NEWER
    assert store.list_unresolved_claims() == ()


def test_the_ledger_reports_a_damaged_row_as_corrupt(tmp_path: Path) -> None:
    store, _ = _store_with_payload(tmp_path, {"kind": "tech_lead"})

    unreadable = store.list_unreadable_claims()

    assert len(unreadable) == 1
    assert unreadable[0].readability is ClaimReadability.UNREADABLE_CORRUPT


def test_a_live_run_holding_a_newer_claim_is_quarantined_as_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup path classifies too, so a restarted orchestrator's verdict on
    a live terminal is the same one the sweep reaches."""
    store, run = _store_with_payload(tmp_path, _incident_payload())
    monkeypatch.setattr(codec, "TechLeadSessionFlavor", _OlderTechLeadSessionFlavor)

    with pytest.raises(NewerPendingWorkClaimError) as raised:
        store.look_up_pending_work_claim(run)

    assert raised.value.readability is ClaimReadability.UNREADABLE_NEWER


# ---------------------------------------------------------------------------
# Proof 4 - what the operator actually reads
# ---------------------------------------------------------------------------


class _RecordingLabels:
    """The quarantine's typed label ops, recorded rather than applied."""

    def __init__(self) -> None:
        self.comments: list[str] = []

    def acquire_block(self, issue_number: int) -> QuarantineLabelState:
        return QuarantineLabelState.ACQUIRED

    def release_block(self, issue_number: int) -> bool:
        return True

    def announce(self, issue_number: int, comment: str) -> bool:
        self.comments.append(comment)
        return True


def _announced(tmp_path: Path, subject: QuarantineSubject) -> str:
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    labels = _RecordingLabels()
    ClaimQuarantineOwner(
        store=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        labels=labels,
        events=MagicMock(),
    ).quarantine(subject)
    assert len(labels.comments) == 1
    return labels.comments[0]


def _unreadable(readability: ClaimReadability) -> UnreadableClaim:
    return UnreadableClaim(
        run_key="/runs/tech-lead-23",
        session_name="tech-lead-23",
        issue_number=23,
        error="claim payload field 'flavor' is 'planning_investigation'",
        started_at="2026-08-07T00:00:00+00:00",
        readability=readability,
    )


#: Words that assert the artifact is beyond saving. None of them may appear in
#: the story told about a claim that is merely written in a larger vocabulary.
_DAMAGE_CLAIMS = (
    "cannot be recovered",
    "cannot be rebuilt by any build",
    "corrupt",
    "damaged",
)


def test_the_newer_story_never_tells_an_operator_the_claim_is_unrecoverable(
    tmp_path: Path,
) -> None:
    """The sentence at the heart of #209.

    An operator reading this went looking for damage in an artifact that a
    build one commit newer reads without complaint.
    """
    comment = _announced(
        tmp_path,
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNREADABLE_NEWER)
        ),
    )

    lowered = comment.lower()
    for damage in _DAMAGE_CLAIMS:
        assert damage not in lowered, damage
    assert "NEWER build" in comment
    assert "nothing to repair or reconstruct" in comment


def test_the_corrupt_story_still_says_the_work_has_to_be_reconstructed(
    tmp_path: Path,
) -> None:
    """The other verdict must keep its urgency - this one really is lost."""
    comment = _announced(
        tmp_path,
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNREADABLE_CORRUPT)
        ),
    )

    assert "cannot be rebuilt by any build" in comment
    assert "NEWER build" not in comment
    # Still names every queue it could have come from, rather than guessing one.
    assert "review, rework, validation retry or tech-lead investigation" in comment


def test_a_live_run_with_a_newer_claim_gets_the_newer_story(tmp_path: Path) -> None:
    """The verdict reaches the live-terminal escalation as well as the sweep."""
    session = MagicMock()
    session.terminal_id = "tech-lead-23"
    session.issue.number = 23
    subject = QuarantineSubject.live_run_with_unreadable_claim(
        QuarantinedSession(
            session,
            "claim payload field 'flavor' is 'planning_investigation'",
            "/runs/tech-lead-23",
            "/runs/tech-lead-23@t1",
            ClaimReadability.UNREADABLE_NEWER,
        )
    )

    comment = _announced(tmp_path, subject)

    assert subject.readability is ClaimReadability.UNREADABLE_NEWER
    assert "NEWER build" in comment
    for damage in _DAMAGE_CLAIMS:
        assert damage not in comment.lower(), damage


class _RaisingClaims:
    """A claim store whose read always fails, at the port boundary."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def look_up_pending_work_claim(self, run: object) -> object:
        raise self._exc

    def run_key_for(self, run: object) -> str:
        return "/runs/tech-lead-23"

    def quarantine_key_for(self, run: object) -> str:
        return "/runs/tech-lead-23@t1"


def _rehydrate_verdict(exc: BaseException) -> ClaimReadability:
    """What restoration concludes about a live terminal whose read raised."""
    from issue_orchestrator.control.in_flight_work import InFlightWorkLedger

    state = MagicMock()
    state.in_flight_work = []
    state.active_sessions = []
    session = MagicMock()
    session.terminal_id = "tech-lead-23"
    session.issue.number = 23

    restoration = InFlightWorkLedger(state, _RaisingClaims(exc)).rehydrate([session])

    assert len(restoration.quarantined) == 1
    return restoration.quarantined[0].readability


def test_restoration_carries_a_typed_read_failure_through_to_the_verdict() -> None:
    """A store that classified its refusal has that verdict respected."""
    assert (
        _rehydrate_verdict(NewerPendingWorkClaimError("a build that knows more"))
        is ClaimReadability.UNREADABLE_NEWER
    )
    assert (
        _rehydrate_verdict(CorruptPendingWorkClaimError("not JSON"))
        is ClaimReadability.UNREADABLE_CORRUPT
    )


def test_an_unclassified_read_failure_reaches_no_verdict_about_the_artifact(
) -> None:
    """A store fault is a fact about the STORE, not a finding about the row.

    Both wrong answers were available. Calling it NEWER would hand out "this is
    fine, a newer build reads it" on the strength of an exception nobody
    classified. Calling it CORRUPT asserts damage in a record nothing looked at.
    """
    assert (
        _rehydrate_verdict(sqlite3.OperationalError("database is locked"))
        is ClaimReadability.UNEXAMINED
    )
    # ...and it is still not readable, so every conservative decision that asks
    # "may this record be trusted" gets the same answer damage gets.
    assert not ClaimReadability.UNEXAMINED.readable


def test_an_unexamined_record_is_never_declared_beyond_repair(
    tmp_path: Path,
) -> None:
    """#209's own harm, restated with a stronger adjective.

    ``look_up_pending_work_claim`` reads sqlite and does not wrap driver
    errors, so a locked database used to produce a GitHub comment telling an
    operator that a record which was never even examined "cannot be rebuilt by
    any build".
    """
    comment = _announced(
        tmp_path,
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNEXAMINED)
        ),
    )

    for damage in _DAMAGE_CLAIMS:
        assert damage not in comment.lower(), damage
    assert "NEWER build" not in comment  # nor the reassurance, unearned
    assert "could not be read AT ALL on this pass" in comment
    # The queued work is still unknown, so the urgency of not re-queueing by
    # hand survives - only the claim about the artifact is withdrawn.
    assert "review, rework, validation retry or tech-lead investigation" in comment


def test_a_corrected_verdict_reaches_the_operator_who_read_the_wrong_one(
    tmp_path: Path,
) -> None:
    """The announcement must be correctable, not merely re-assertable (#209).

    Sweep 1 hits a transient store fault and comments. Sweep 2 reads the row
    properly and finds it NEWER. The CAUSE has not changed across the two, so a
    quarantine whose durable announcement identity is the cause alone stays
    silent forever and leaves the operator holding the first story, with
    ``needs-human`` on the issue and nothing to correct it.
    """
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    labels = _RecordingLabels()
    owner = ClaimQuarantineOwner(
        store=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        labels=labels,
        events=MagicMock(),
    )

    owner.quarantine(
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNEXAMINED)
        )
    )
    owner.quarantine(
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNREADABLE_NEWER)
        )
    )

    assert len(labels.comments) == 2
    assert "could not be read AT ALL on this pass" in labels.comments[0]
    assert "NEWER build" in labels.comments[1]

    # ...and a THIRD sweep observing the same story again says nothing more.
    owner.quarantine(
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNREADABLE_NEWER)
        )
    )
    assert len(labels.comments) == 2


def test_the_announced_story_is_durable_in_full(tmp_path: Path) -> None:
    """Both halves of what an operator was told survive a restart.

    A story remembered by halves is a story that can only be re-asserted: the
    predicate the escalation is idempotent on compares what was announced
    against what is now observed, and it can only see the difference in the
    part that was kept.
    """
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    subject = QuarantineSubject.ended_run_with_unreadable_claim(
        _unreadable(ClaimReadability.UNREADABLE_NEWER)
    )
    ClaimQuarantineOwner(
        store=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        labels=_RecordingLabels(),
        events=MagicMock(),
    ).quarantine(subject)

    # A fresh handle on the same database - the restart this has to survive.
    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (record,) = reopened.list_quarantines()

    assert record.story == subject.story
    assert record.announces(subject.story)
    for readability in ClaimReadability:
        if readability is subject.readability:
            continue
        other = AnnouncedStory(subject.cause, readability)
        assert not record.announces(other), readability


def test_the_quarantine_event_carries_the_verdict_a_consumer_would_guess(
    tmp_path: Path,
) -> None:
    """Machine consumers must not have to parse ``error`` for the verdict.

    That is the same per-reader guess this boundary removed for humans, and
    this repo's events-vs-logs rule forbids reacting to prose.
    """
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    events = MagicMock()
    ClaimQuarantineOwner(
        store=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        labels=_RecordingLabels(),
        events=events,
    ).quarantine(
        QuarantineSubject.ended_run_with_unreadable_claim(
            _unreadable(ClaimReadability.UNREADABLE_NEWER)
        )
    )

    (published,) = [call.args[0] for call in events.publish.call_args_list]
    assert published.data["readability"] == ClaimReadability.UNREADABLE_NEWER.value
    assert published.data["cause"] == QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN.value


@pytest.mark.parametrize("cause", list(QuarantineCause))
@pytest.mark.parametrize("readability", list(ClaimReadability))
def test_every_cause_and_verdict_pairing_composes_into_whole_sentences(
    cause: QuarantineCause, readability: ClaimReadability, tmp_path: Path
) -> None:
    """The two tables vary independently, so all of their products must read.

    Nothing enforced that before: the READABLE finding ended in a colon and
    relied on the ONE escalation written beside it to continue the sentence, so
    any other cause paired with it emitted "...knows exactly what it is
    carrying:" with nothing after it.
    """
    comment = _announced(
        tmp_path,
        QuarantineSubject(
            quarantine_key="/runs/tech-lead-23@t1",
            run_key="/runs/tech-lead-23",
            session_name="tech-lead-23",
            issue_number=23,
            error="claim payload field 'flavor' is 'planning_investigation'",
            cause=cause,
            readability=readability,
            work_kind=PendingWorkKind.TECH_LEAD if readability.readable else None,
        ),
    )

    assert "{" not in comment and "}" not in comment  # nothing left unformatted
    for paragraph in comment.split("\n\n"):
        assert paragraph.strip(), comment
        # The defect exactly: a clause that hands off to text that never comes.
        assert not paragraph.rstrip().endswith(":"), comment
