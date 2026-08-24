"""Retiring a readable claim whose run has ended, on an operator's authority (#245).

A pinned runtime met a preserved ledger row it could read perfectly: a
``tech_lead:23`` planning investigation whose run had ended long before. Nothing
in the orchestrator could express what had to happen to it. Deferral says
"waiting to be relaunched", which was the opposite of the instruction; consuming
deletes the evidence; parking belongs to a quarantine, is reached only for
records nothing can read, and is UNDONE by that quarantine's release. So the
only remaining move was hand-editing durable state, which is not a move.

These tests pin the disposition that replaces it, and they pin it as a decision
rather than a mechanism that could be reached by accident:

* it is addressed by the ledger's OWN identity, so no dead run has to be
  fabricated to reach the row;
* every way of being unsure which row is meant changes nothing at all;
* the payload survives, and so does a record of who decided and why;
* no sweep, startup path or scheduler can re-admit, requeue or escalate it
  afterwards, across a restart and across the schema catching up;
* nothing remote is touched on any path.

Every fixture here is SYNTHESIZED. The preserved Pilot-3 database, its run
directory and its quarantine row are never opened, read or written by this file
- the ledgers below are built from scratch under ``tmp_path``, exactly as #209
and #210's tests build theirs. The store is the real SQLite one throughout,
because the proofs are about what survives a restart and a fake that keeps state
in a dict cannot fail the way a missing column can.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.in_flight_work import InFlightWorkLedger
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.entrypoints.cli_tools import claim_retire
from issue_orchestrator.execution.pending_work_claim_schema import (
    STORE_FILENAME,
    initialize_schema,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.execution.pending_work_codec import encode_claim
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.infra.sqlite_connection import open_sqlite
from issue_orchestrator.ports.pending_work_claim_retirement import (
    ClaimRetirementRefusal,
    ClaimRetirementRefused,
    ClaimRetirementRequest,
    ClaimRetirementTarget,
)

# ---------------------------------------------------------------------------
# The incident's SHAPE, never the incident's database.
# ---------------------------------------------------------------------------

_ISSUE = 23
_WORK_KEY = "tech_lead:23"
_FLAVOR = TechLeadSessionFlavor.PLANNING_INVESTIGATION.value
_AUTHORITY = "issue #245 human-A decision"
_REASON = "Pilot-3 planning investigation abandoned, not resumed"
_RECORDED_AT = "2026-08-24T09:00:00+00:00"


def _planning_claim() -> PendingWorkClaim:
    return PendingWorkClaim(
        PendingWorkKind.TECH_LEAD,
        PendingTechLeadReview(
            issue_number=_ISSUE,
            title="Prepare the next issue",
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
        ),
    )


def _failure_claim() -> PendingWorkClaim:
    """Same kind, same work key, DIFFERENT variant - what must not be retired."""
    return PendingWorkClaim(
        PendingWorkKind.TECH_LEAD,
        PendingTechLeadReview(
            issue_number=_ISSUE,
            title="Diagnose the failure",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=_ISSUE, issue_title="Broke", failure_reason="failed"
            ),
        ),
    )


def _run_assets(tmp_path: Path, *, run_id: str = "r1"):
    from tests.unit.session_run_helpers import make_session_run_assets

    worktree = tmp_path / f"wt-tech-lead-23-{run_id}"
    worktree.mkdir(parents=True, exist_ok=True)
    return make_session_run_assets(
        worktree, session_name="tech-lead-23", run_id=run_id
    )


def _ledger(tmp_path: Path, claim: PendingWorkClaim | None = None):
    """A real ledger holding one claim, whose run is then LEFT to end.

    Nothing here keeps the run alive: the assets exist only long enough to place
    the row, which is the state #244 measured - a durable claim whose run is
    gone and whose settlement APIs therefore cannot be reached.
    """
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    run = _run_assets(tmp_path)
    store.hold_pending_work_claim(run, claim or _planning_claim(), issue_number=_ISSUE)
    return store, run


def _target(**overrides) -> ClaimRetirementTarget:
    described = {
        "work_key": _WORK_KEY,
        "issue_number": _ISSUE,
        "work_kind": PendingWorkKind.TECH_LEAD,
        "flavor": _FLAVOR,
    }
    described.update(overrides)
    return ClaimRetirementTarget(**described)


def _request(**overrides) -> ClaimRetirementRequest:
    described = {
        "target": _target(),
        "reason": _REASON,
        "authority": _AUTHORITY,
        "recorded_at": _RECORDED_AT,
    }
    described.update(overrides)
    return ClaimRetirementRequest(**described)


def _connect(tmp_path: Path) -> sqlite3.Connection:
    return open_sqlite(
        state_dir(tmp_path) / STORE_FILENAME, row_factory=sqlite3.Row
    )


def _sweep(tmp_path: Path, quarantine: MagicMock) -> tuple[int, MagicMock]:
    """One ordinary recovery pass, as a restarted orchestrator runs it."""
    state = MagicMock()
    state.in_flight_work = []
    state.active_sessions = []
    state.pending_tech_lead_reviews = []
    ledger = InFlightWorkLedger(state, SqlitePendingWorkClaimStore.for_repo(tmp_path))
    return ledger.recover_unresolved(quarantine), state


# ---------------------------------------------------------------------------
# Acceptance 1 - exact addressing, with no run object to fabricate
# ---------------------------------------------------------------------------


def test_a_claim_is_retired_by_its_ledger_identity_alone(tmp_path: Path) -> None:
    """The whole point: the run is gone, and the row is still addressable.

    Every other settlement on this ledger takes ``SessionRunAssets``, which is
    rebuilt from a worktree manifest that no longer describes anything. This one
    takes the work key the ledger itself wrote down.
    """
    store, _ = _ledger(tmp_path)

    record = store.retire_claim(_request())

    assert record.work_key == _WORK_KEY
    assert record.issue_number == _ISSUE
    assert record.work_kind is PendingWorkKind.TECH_LEAD
    assert record.flavor == _FLAVOR
    assert record.authority == _AUTHORITY
    assert record.reason == _REASON
    assert record.recorded_at == _RECORDED_AT


def test_the_retired_row_keeps_the_generation_it_settled(tmp_path: Path) -> None:
    """A run root is reusable, so a retirement names run key AND start instant."""
    store, run = _ledger(tmp_path)

    record = store.retire_claim(_request())

    (unresolved,) = store.list_unresolved_claims()
    assert record.run_key == unresolved.run_key
    assert record.started_at == unresolved.started_at
    assert record.session_name == unresolved.session_name
    assert record.run_key == store.run_key_for(run)


# ---------------------------------------------------------------------------
# Acceptance 2 - every uncertainty fails closed, writing nothing
# ---------------------------------------------------------------------------


def _assert_nothing_changed(store: SqlitePendingWorkClaimStore) -> None:
    """The ledger after a refusal: no disposition, no evidence, no loss."""
    assert store.list_retired_claims() == ()
    for unresolved in store.list_unresolved_claims():
        assert not unresolved.retired
        assert unresolved.re_admissible


def test_an_unknown_work_key_retires_nothing(tmp_path: Path) -> None:
    store, _ = _ledger(tmp_path)

    with pytest.raises(ClaimRetirementRefused) as refused:
        store.retire_claim(_request(target=_target(work_key="tech_lead:999")))

    assert refused.value.refusal is ClaimRetirementRefusal.NO_SUCH_CLAIM
    _assert_nothing_changed(store)


def test_a_duplicated_work_identity_retires_nothing(tmp_path: Path) -> None:
    """Two rows carry this work. WHICH one is meant is exactly what is unknown.

    ``work_key`` is indexed, not unique - two runs can be recorded against one
    piece of work - so picking either would retire a row nobody named.
    """
    store, _ = _ledger(tmp_path)
    store.hold_pending_work_claim(
        _run_assets(tmp_path, run_id="r2"), _planning_claim(), issue_number=_ISSUE
    )

    with pytest.raises(ClaimRetirementRefused) as refused:
        store.retire_claim(_request())

    assert refused.value.refusal is ClaimRetirementRefusal.AMBIGUOUS_IDENTITY
    assert len(store.list_unresolved_claims()) == 2
    _assert_nothing_changed(store)


@pytest.mark.parametrize(
    "name, overrides",
    [
        ("a different issue", {"issue_number": 24}),
        ("a different work kind", {"work_kind": PendingWorkKind.REVIEW}),
        ("a different flavor", {"flavor": "failure_investigation"}),
        ("no flavor at all", {"flavor": None}),
    ],
)
def test_a_claim_that_is_not_the_one_described_retires_nothing(
    tmp_path: Path, name: str, overrides: dict
) -> None:
    """The address matched and the claim did not, which is not a near miss.

    ``tech_lead:23`` alone cannot tell a planning investigation from a failure
    investigation of the same issue, and those are opposite decisions.
    """
    store, _ = _ledger(tmp_path)

    with pytest.raises(ClaimRetirementRefused) as refused:
        store.retire_claim(_request(target=_target(**overrides)))

    assert refused.value.refusal is ClaimRetirementRefusal.IDENTITY_MISMATCH, name
    _assert_nothing_changed(store)


def test_the_wrong_variant_under_the_right_address_retires_nothing(
    tmp_path: Path,
) -> None:
    """The mirror image: the row is the failure investigation, and the operator
    described the planning one."""
    store, _ = _ledger(tmp_path, _failure_claim())

    with pytest.raises(ClaimRetirementRefused) as refused:
        store.retire_claim(_request())

    assert refused.value.refusal is ClaimRetirementRefusal.IDENTITY_MISMATCH
    _assert_nothing_changed(store)


def test_an_unreadable_claim_retires_nothing(tmp_path: Path) -> None:
    """Expectations are checked AGAINST the payload, so an unreadable one cannot
    be confirmed as the row described - and it already has a settlement of its
    own (#210)."""
    store, run = _ledger(tmp_path)
    conn = _connect(tmp_path)
    conn.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        (json.dumps({"kind": "tech_lead"}), store.run_key_for(run)),
    )
    conn.commit()
    conn.close()
    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)

    with pytest.raises(ClaimRetirementRefused) as refused:
        reopened.retire_claim(_request())

    assert refused.value.refusal is ClaimRetirementRefusal.CLAIM_UNREADABLE
    assert reopened.list_retired_claims() == ()
    (unreadable,) = reopened.list_unreadable_claims()
    assert not unreadable.retired


def test_an_already_retired_claim_is_not_retired_twice(tmp_path: Path) -> None:
    """The second decision would overwrite the evidence of the first."""
    store, _ = _ledger(tmp_path)
    store.retire_claim(_request())

    with pytest.raises(ClaimRetirementRefused) as refused:
        store.retire_claim(_request(reason="a different reason"))

    assert refused.value.refusal is ClaimRetirementRefusal.ALREADY_RETIRED
    (record,) = store.list_retired_claims()
    assert record.reason == _REASON


def test_a_quarantine_settled_claim_retires_nothing(tmp_path: Path) -> None:
    """Two authorities over one row is the ambiguity this avoids.

    A quarantine's release un-parks the row it settled. A retirement recorded
    underneath one would either be revoked by that release or outlive an
    escalation that has since been repaired, and neither is a disposition
    anybody could reason about.
    """
    from issue_orchestrator.ports.pending_work_claim_store import (
        AnnouncedStory,
        ClaimReadability,
        ClaimSettlement,
        QuarantineCause,
    )

    store, run = _ledger(tmp_path)
    store.record_quarantine(
        store.quarantine_key_for(run),
        run_key=store.run_key_for(run),
        session_name="tech-lead-23",
        issue_number=_ISSUE,
        error="unreadable",
        story=AnnouncedStory(
            QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
            ClaimReadability.UNREADABLE_CORRUPT,
        ),
        work_kind=PendingWorkKind.TECH_LEAD,
        settlement=ClaimSettlement.PARK,
    )

    with pytest.raises(ClaimRetirementRefused) as refused:
        store.retire_claim(_request())

    assert refused.value.refusal is ClaimRetirementRefusal.QUARANTINE_SETTLED
    assert store.list_retired_claims() == ()


# ---------------------------------------------------------------------------
# Acceptance 3 - the evidence survives the decision
# ---------------------------------------------------------------------------


def test_retirement_preserves_the_payload_and_its_provenance(
    tmp_path: Path,
) -> None:
    """No delete-and-forget. The row stays, and so does what it was carrying."""
    store, _ = _ledger(tmp_path)
    (before,) = store.list_unresolved_claims()

    record = store.retire_claim(_request())

    (after,) = store.list_unresolved_claims()
    assert after.claim == before.claim == _planning_claim()
    assert after.session_name == before.session_name
    assert after.started_at == before.started_at
    assert after.issue_number == before.issue_number
    # And the record carries its own copy, byte for byte as the ledger held it,
    # so the audit trail does not depend on a row later work could supersede.
    assert json.loads(record.payload) == encode_claim(_planning_claim())


def test_the_evidence_outlives_a_relaunch_of_the_same_work(
    tmp_path: Path,
) -> None:
    """A retired row is not a deferral waiting to be superseded.

    ``hold_pending_work_claim`` clears the deferred row for the work it is
    taking, because relaunching is what resolves a deferral. A retired row is
    not waiting to be relaunched, so an unrelated later launch that happens to
    share the work key must not delete the evidence.
    """
    store, run = _ledger(tmp_path)
    store.mark_deferred_by_run_key(store.run_key_for(run))
    store.retire_claim(_request())

    store.hold_pending_work_claim(
        _run_assets(tmp_path, run_id="r2"), _planning_claim(), issue_number=_ISSUE
    )

    keys = {row.run_key for row in store.list_unresolved_claims()}
    assert store.run_key_for(run) in keys
    (record,) = store.list_retired_claims()
    assert record.run_key == store.run_key_for(run)


def test_a_retired_payload_is_never_rewritten_by_a_deferred_refresh(
    tmp_path: Path,
) -> None:
    """The preserved payload is evidence, so the deferred-row refresh skips it -
    and says so, rather than reporting an overwrite that did not happen."""
    store, run = _ledger(tmp_path)
    store.mark_deferred_by_run_key(store.run_key_for(run))
    store.retire_claim(_request())

    assert not store.refresh_deferred_claim(_WORK_KEY, _failure_claim())

    (unresolved,) = store.list_unresolved_claims()
    assert unresolved.claim == _planning_claim()


def test_a_retired_row_is_not_dropped_by_a_deferred_retirement(
    tmp_path: Path,
) -> None:
    """The launch-drop compensation deletes work. A retired row is not work."""
    store, run = _ledger(tmp_path)
    store.mark_deferred_by_run_key(store.run_key_for(run))
    store.retire_claim(_request())

    store.retire_deferred_claim(_WORK_KEY)

    assert len(store.list_unresolved_claims()) == 1


# ---------------------------------------------------------------------------
# Acceptance 4 and 9 - recovery is blocked, and blocked BY the disposition
# ---------------------------------------------------------------------------


def test_ordinary_recovery_cannot_re_admit_a_retired_claim(
    tmp_path: Path,
) -> None:
    """The sweep re-admits every unresolved row whose run is not live, and a
    claim whose run ended is exactly that shape."""
    store, _ = _ledger(tmp_path)
    store.retire_claim(_request())
    quarantine = MagicMock()

    readmitted, state = _sweep(tmp_path, quarantine)

    assert readmitted == 0
    assert state.pending_tech_lead_reviews == []
    # Not escalated either: a decision was recorded, so there is nobody to ask.
    quarantine.quarantine.assert_not_called()
    (row,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_unresolved_claims()
    assert row.retired
    assert not row.re_admissible


def test_removing_the_retirement_disposition_lets_recovery_take_the_work_back(
    tmp_path: Path,
) -> None:
    """The failure-direction proof (#245 acceptance 9).

    The same ledger, the same sweep, the same claim - with the retired bit
    cleared underneath it. If the work comes back here, then the test above is
    passing because of the disposition and not because a planning investigation
    happens to be unschedulable for some unrelated reason.
    """
    store, _ = _ledger(tmp_path)
    store.retire_claim(_request())
    conn = _connect(tmp_path)
    conn.execute("UPDATE pending_work_claim SET retired = 0")
    conn.commit()
    conn.close()

    readmitted, state = _sweep(tmp_path, MagicMock())

    assert readmitted == 1
    assert [item.issue_number for item in state.pending_tech_lead_reviews] == [_ISSUE]


def test_a_retired_row_that_later_reads_as_unreadable_raises_no_escalation(
    tmp_path: Path,
) -> None:
    """A pinned runtime meeting a larger vocabulary is normal (#209).

    Escalating here would have the recovery sweep mutate GitHub - a block and a
    comment - over a decision that has already been taken, and do it again on
    every sweep for as long as the skew lasts.
    """
    store, run = _ledger(tmp_path)
    store.retire_claim(_request())
    conn = _connect(tmp_path)
    conn.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        (json.dumps({"kind": "tech_lead"}), store.run_key_for(run)),
    )
    conn.commit()
    conn.close()
    quarantine = MagicMock()

    readmitted, _ = _sweep(tmp_path, quarantine)

    assert readmitted == 0
    quarantine.quarantine.assert_not_called()
    # The row is still enumerable as evidence, carrying both facts.
    (unreadable,) = SqlitePendingWorkClaimStore.for_repo(
        tmp_path
    ).list_unreadable_claims()
    assert unreadable.retired


def test_recovery_retires_nothing_by_itself(tmp_path: Path) -> None:
    """Acceptance 8: no sweep, TTL or GC reaches this disposition.

    An ordinary recovery pass over an ordinary ledger takes the work back, and
    records no retirement doing it.
    """
    _ledger(tmp_path)

    readmitted, _ = _sweep(tmp_path, MagicMock())

    assert readmitted == 1
    assert SqlitePendingWorkClaimStore.for_repo(tmp_path).list_retired_claims() == ()


# ---------------------------------------------------------------------------
# Acceptance 5 - the disposition is durable across a restart and a migration
# ---------------------------------------------------------------------------


def test_the_disposition_survives_a_restart(tmp_path: Path) -> None:
    store, _ = _ledger(tmp_path)
    store.retire_claim(_request())

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)

    (row,) = reopened.list_unresolved_claims()
    assert row.retired
    assert not row.re_admissible
    (record,) = reopened.list_retired_claims()
    assert record.authority == _AUTHORITY
    assert record.reason == _REASON


def test_the_disposition_survives_the_schema_catching_up(tmp_path: Path) -> None:
    """Re-running the migration is what every start does, so it must lose nothing.

    ``add_missing_columns`` is additive precisely so that a database written by
    an earlier build keeps its rows - and a re-initialization that reset a
    retirement to its permissive default would hand the abandoned work straight
    back to the next sweep.
    """
    store, _ = _ledger(tmp_path)
    store.retire_claim(_request())

    conn = _connect(tmp_path)
    initialize_schema(conn)
    conn.close()

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    (row,) = reopened.list_unresolved_claims()
    assert row.retired
    assert len(reopened.list_retired_claims()) == 1


def test_a_ledger_written_before_this_build_reads_as_not_retired(
    tmp_path: Path,
) -> None:
    """The additive direction: absent means permissive, so an upgrade alone
    never takes anybody's work away."""
    store, run = _ledger(tmp_path)
    conn = _connect(tmp_path)
    conn.execute("ALTER TABLE pending_work_claim DROP COLUMN retired")
    conn.execute("DROP TABLE pending_work_claim_retirement")
    conn.commit()
    conn.close()

    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)

    (row,) = reopened.list_unresolved_claims()
    assert not row.retired
    assert row.re_admissible
    assert reopened.list_retired_claims() == ()
    assert row.run_key == store.run_key_for(run)


# ---------------------------------------------------------------------------
# Acceptance 6 - an unattributable retirement cannot be expressed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["reason", "authority", "recorded_at"])
@pytest.mark.parametrize("empty", ["", "   "])
def test_a_retirement_without_an_attributable_authority_cannot_be_built(
    field: str, empty: str
) -> None:
    """Refused at construction, not at the store: a request that cannot say who
    decided must not exist long enough to be passed anywhere."""
    with pytest.raises(ValueError) as raised:
        _request(**{field: empty})

    assert field in str(raised.value)


def test_a_target_without_a_ledger_address_cannot_be_built() -> None:
    with pytest.raises(ValueError):
        _target(work_key="  ")


# ---------------------------------------------------------------------------
# Acceptance 7 - local commits, and nothing remote on any path
# ---------------------------------------------------------------------------


def test_retirement_commits_without_any_remote_collaborator(
    tmp_path: Path,
) -> None:
    """The store has no notifier to fail, because retirement has no remote half.

    Telling anybody about this is a separate, separately authorized act. What
    this pins is that the local disposition does not wait for one, cannot be
    undone by one, and is complete on this machine the moment it commits - the
    same separation #210 drew for a quarantine's announcement.
    """
    store, _ = _ledger(tmp_path)

    store.retire_claim(_request())

    assert not hasattr(store, "announce")
    # Committed on a connection that has never seen this process's writes.
    probe = _connect(tmp_path)
    (row,) = probe.execute(
        "SELECT retired FROM pending_work_claim"
    ).fetchall()
    (record,) = probe.execute(
        "SELECT authority FROM pending_work_claim_retirement"
    ).fetchall()
    probe.close()
    assert row["retired"] == 1
    assert record["authority"] == _AUTHORITY


# ---------------------------------------------------------------------------
# The operator entry point - inert unless invoked, and refusing out loud
# ---------------------------------------------------------------------------


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--root",
        str(tmp_path),
        "record",
        "--work-key",
        _WORK_KEY,
        "--issue",
        str(_ISSUE),
        "--work-kind",
        "tech_lead",
        "--flavor",
        _FLAVOR,
        "--reason",
        _REASON,
        "--authority",
        _AUTHORITY,
        *extra,
    ]


def test_the_operator_command_records_one_retirement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger(tmp_path)

    assert claim_retire.main(_argv(tmp_path)) == 0

    assert "RETIRED" in capsys.readouterr().out
    (record,) = SqlitePendingWorkClaimStore.for_repo(tmp_path).list_retired_claims()
    assert record.authority == _AUTHORITY


def test_the_operator_command_rehearses_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` runs the real checks against the real ledger and rolls back.

    Retirement is a one-way door, so finding out that the described row is not
    the intended row has to be possible on the near side of it.
    """
    _ledger(tmp_path)

    assert claim_retire.main(_argv(tmp_path, "--dry-run")) == 0

    assert "WOULD RETIRE" in capsys.readouterr().out
    reopened = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    assert reopened.list_retired_claims() == ()
    (row,) = reopened.list_unresolved_claims()
    assert not row.retired
    assert row.re_admissible


def test_the_operator_command_rehearses_a_refusal_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger(tmp_path, _failure_claim())

    assert claim_retire.main(_argv(tmp_path, "--dry-run")) == 1

    assert "identity_mismatch" in capsys.readouterr().err


def test_the_operator_command_reports_a_refusal_as_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger(tmp_path)

    exit_code = claim_retire.main(
        [
            "--root",
            str(tmp_path),
            "record",
            "--work-key",
            "tech_lead:999",
            "--issue",
            str(_ISSUE),
            "--work-kind",
            "tech_lead",
            "--flavor",
            _FLAVOR,
            "--reason",
            _REASON,
            "--authority",
            _AUTHORITY,
        ]
    )

    assert exit_code == 1
    assert "no_such_claim" in capsys.readouterr().err
    assert SqlitePendingWorkClaimStore.for_repo(tmp_path).list_retired_claims() == ()


def test_the_operator_command_rejects_an_unattributable_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed request is a different exit code from a refused one: one is
    the operator's shell, the other is the ledger's answer."""
    _ledger(tmp_path)

    exit_code = claim_retire.main(_argv(tmp_path)[:-1] + ["   "])

    assert exit_code == 2
    assert "authority" in capsys.readouterr().err
    assert SqlitePendingWorkClaimStore.for_repo(tmp_path).list_retired_claims() == ()


def test_the_operator_command_reads_the_evidence_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, _ = _ledger(tmp_path)
    store.retire_claim(_request())

    assert claim_retire.main(["--root", str(tmp_path), "evidence"]) == 0

    printed = capsys.readouterr().out
    assert _AUTHORITY in printed
    assert _REASON in printed
    assert _FLAVOR in printed
