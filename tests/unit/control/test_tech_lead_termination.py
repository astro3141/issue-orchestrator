"""Termination releases BOTH coordination holds (#6994 round 2 F10 / A7).

The one-shot driver terminates a timed-out session and then runs NO further
tick, so whatever termination does not clean up is never cleaned up. Round 2
released the per-issue claim but left the repository-wide RUN hold live, which
meant a timed-out whole-repository review kept every conflicting tech-lead run
blocked until its lease expired — for a command that had already exited.

These tests drive the REAL one-shot timeout path against a REAL shared run
ledger, so "the hold is gone" is observed in the ledger rather than asserted
about a mock. The clock and the drive loop are injected, so nothing sleeps.

The same "no later tick exists" argument is why this owner captures the run's
staged evidence before it reaps the checkout (#360): the classes at the bottom
of this file drive the REAL removal against a REAL directory tree, so "the
evidence outlived the worktree" is observed on disk rather than asserted about
a mock.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from issue_orchestrator.control.tech_lead_launch_authority import (
    TechLeadLaunchAuthority,
)
from issue_orchestrator.control.tech_lead_termination import (
    terminate_tech_lead_session,
)
from issue_orchestrator.control.tech_lead_trigger import (
    TechLeadOutcomeStatus,
    run_health_review,
    run_targeted_investigations,
)
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_evidence_capture import (
    CAPTURE_RECEIPT_FILENAME,
    tech_lead_evidence_capture_dir,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunOutcome,
    TechLeadRunRequest,
)
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_DATA_DIRNAME,
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryEventSink
from tests.unit.session_run_helpers import make_session_run_assets

from .run_ledger_doubles import SharedRunLedger

TECH_LEAD_AGENT = "agent:tech-lead"
ANCHOR = 900
FOCUS = 42
MANIFEST_BODY = '{"prs": [1234]}'


class FakeIssue:
    def __init__(self, number: int, *, state: str = "open") -> None:
        self.number = number
        self.title = f"Issue #{number}"
        self.labels = ("blocked-failed",)
        self.state = state
        self.body = ""
        self.milestone = None


class FakeSession:
    """The session surface both the drive loop and termination touch."""

    def __init__(
        self,
        issue_number: int,
        flavor: TechLeadSessionFlavor,
        workspace: Path,
        *,
        scratch_worktree: bool = True,
    ) -> None:
        self.issue = FakeIssue(issue_number)
        self.agent_label = TECH_LEAD_AGENT
        self.terminal_id = f"tech-lead-{issue_number}"
        self.key = SimpleNamespace(stable_id=lambda: f"tech_lead:{issue_number}")
        self.lease_id = None
        self.scratch_worktree = scratch_worktree
        self.worktree_path = workspace / f"scratch-{issue_number}"
        self.worktree_path.mkdir(parents=True, exist_ok=True)
        # Typed run assets, allocated INSIDE the checkout a reap destroys —
        # which is the whole reason the evidence has to be copied out (#360).
        self.run_assets = make_session_run_assets(
            self.worktree_path, session_name=f"issue-{issue_number}"
        )
        self.tech_lead_scope = TechLeadLaunchScope(flavor=flavor)


class _State:
    def __init__(self) -> None:
        self.active_sessions: list[FakeSession] = []
        self.pending_tech_lead_reviews: list[PendingTechLeadReview] = []
        self.paused = False

    def drop_active_session(self, terminal_id: str) -> None:
        self.active_sessions = [
            s for s in self.active_sessions if s.terminal_id != terminal_id
        ]


class FailingWorktrees:
    """A worktree manager whose removal always fails, to prove independence."""

    def remove_checkout_and_branch(self, path, force: bool = False) -> None:
        raise RuntimeError(f"cannot remove {path} (force={force})")


class ReapingWorktrees:
    """A worktree manager that REALLY removes the tree, witnessing as it goes.

    ``witness`` is evaluated at the instant of removal, so the ordering claim
    ("the capture was already durable") is observed rather than inferred from
    the end state.
    """

    def __init__(self, witness: Callable[[], bool]) -> None:
        self._witness = witness
        self.captured_before_removal: Optional[bool] = None
        self.removed: list[Path] = []

    def remove_checkout_and_branch(self, path, force: bool = False) -> None:
        self.captured_before_removal = self._witness()
        self.removed.append(Path(path))
        shutil.rmtree(path)


class _Host:
    """A one-shot dispatch host wired to the REAL launch and termination owners."""

    def __init__(
        self,
        *,
        flavor: TechLeadSessionFlavor,
        workspace: Path,
        shared: Optional[SharedRunLedger] = None,
        worktrees: object = None,
    ) -> None:
        self.workspace = workspace
        self.state = _State()
        self.config = Config()
        # The HOST repository the capture writes into — deliberately outside
        # every session worktree, which is what lets it survive the reap.
        self.config.repo_root = workspace / "host-repo"
        self.config.repo_root.mkdir(parents=True, exist_ok=True)
        self.config.tech_lead_review_agent = TECH_LEAD_AGENT
        self.shared = shared or SharedRunLedger()
        self.ownership = self.shared.ownership("engine-a")
        self.flavor = flavor
        self.issues = {ANCHOR: FakeIssue(ANCHOR), FOCUS: FakeIssue(FOCUS)}
        self.repository_host = SimpleNamespace(get_issue=self.issues.get)
        self.events = InMemoryEventSink()
        self.deps = SimpleNamespace(
            run_ownership=self.ownership,
            claim_manager=None,
            state_machine_manager=None,
            worktree_manager=worktrees,
            events=self.events,
        )
        self.killed: list[str] = []
        self.pause_calls = 0
        self.tick_count = 0

    # -- TechLeadDispatchHost -------------------------------------------
    def pause(self) -> None:
        self.pause_calls += 1

    def tick(self) -> bool:
        # Deliberately never drains the session: this IS the timeout path. The
        # run stages its evidence as it works, which is what the reap that
        # follows would otherwise take with it.
        self.tick_count += 1
        for session in self.state.active_sessions:
            stage_evidence(session)
        return True

    def request_tech_lead_run(self, request: TechLeadRunRequest):
        number = ANCHOR if request.scope.kind.is_global else FOCUS
        self.state.pending_tech_lead_reviews.append(_queued(number, self.flavor))
        return TechLeadRunAdmission(
            outcome=TechLeadRunOutcome.QUEUED,
            scope_kind=request.scope.kind,
            run_key=request.scope.run_key,
            reason="admitted",
            detail="queued",
            trigger=request.trigger,
            issue_number=number,
        )

    def launch_tech_lead_session(self, tech_lead: PendingTechLeadReview):
        return TechLeadLaunchAuthority(
            state=self.state,  # type: ignore[arg-type]
            config=self.config,
            ownership=self.ownership,
            repository_host=self.repository_host,  # type: ignore[arg-type]
            is_blocking_any=lambda labels: any(
                str(label).startswith("blocked") for label in labels
            ),
            events=SimpleNamespace(publish=lambda _e: None),  # type: ignore[arg-type]
            launch=self._start_session,
        ).launch(tech_lead)

    def _start_session(self, tech_lead: PendingTechLeadReview):
        self.state.pending_tech_lead_reviews = [
            item
            for item in self.state.pending_tech_lead_reviews
            if item.issue_number != tech_lead.issue_number
        ]
        session = FakeSession(
            tech_lead.issue_number, tech_lead.flavor, self.workspace
        )
        self.state.active_sessions.append(session)
        return session

    def terminate_tech_lead_session(self, session):
        return terminate_tech_lead_session(self, session)  # type: ignore[arg-type]

    # -- TechLeadTerminationHost ----------------------------------------
    def kill_session(self, name: str) -> None:
        self.killed.append(name)


def _queued(number: int, flavor: TechLeadSessionFlavor) -> PendingTechLeadReview:
    failure = (
        DiscoveredFailure(
            issue_number=number,
            issue_title=f"Investigate #{number}",
            failure_reason="timed_out",
        )
        if flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        else None
    )
    return PendingTechLeadReview(
        number, f"Tech lead #{number}", flavor=flavor, failure=failure
    )


def _clock(values):
    """A monotonic ``now`` that yields the given values, holding the last."""
    seq = list(values)
    cursor = {"i": 0}

    def now() -> float:
        index = min(cursor["i"], len(seq) - 1)
        cursor["i"] += 1
        return float(seq[index])

    return now


def _no_sleep(_seconds: float) -> None:
    pass


def stage_evidence(session: FakeSession) -> Path:
    """Stage what a tech_lead run writes into its own checkout as it works."""
    data_dir = session.run_assets.run_dir / TECH_LEAD_DATA_DIRNAME
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "manifest.json").write_text(MANIFEST_BODY, encoding="utf-8")
    (data_dir / "board-snapshot.json").write_text('{"board": []}', encoding="utf-8")
    (data_dir / "candidates" / "1234").mkdir(parents=True, exist_ok=True)
    (data_dir / "candidates" / "1234" / "diff.patch").write_text(
        "diff --git a/x b/x\n", encoding="utf-8"
    )
    return data_dir


def _capture_dir(host: _Host, session: FakeSession) -> Path:
    return tech_lead_evidence_capture_dir(
        host.config.repo_root,
        session_name=session.run_assets.session_name,
        run_id=session.run_assets.run_id,
    )


# ---------------------------------------------------------------------------


def test_a_timed_out_global_review_hands_its_repository_wide_hold_back(tmp_path):
    """No later tick exists to reconcile it, so termination must do it."""
    host = _Host(flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path)

    result = run_health_review(
        host,  # type: ignore[arg-type]
        now=_clock([0, 10_000]),
        sleep=_no_sleep,
        timeout_s=1.0,
    )

    assert result.status is TechLeadOutcomeStatus.TIMED_OUT
    assert host.killed == [f"tech-lead-{ANCHOR}"]
    assert host.shared.live_keys() == (), (
        "a terminated review must not block the repository until lease expiry"
    )
    assert result.termination is not None
    assert result.termination.run_released is True
    assert result.termination.clean is True


def test_a_timed_out_targeted_investigation_hands_its_run_back_too(tmp_path):
    host = _Host(flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION, workspace=tmp_path)

    results = run_targeted_investigations(
        host,  # type: ignore[arg-type]
        [FOCUS],
        now=_clock([0, 10_000]),
        sleep=_no_sleep,
        timeout_s=1.0,
    )

    assert results[0].status is TechLeadOutcomeStatus.TIMED_OUT
    assert host.shared.live_keys() == ()
    assert results[0].termination is not None
    assert results[0].termination.run_released is True


def test_the_run_is_released_even_when_another_cleanup_effect_FAILS(tmp_path):
    """Effects are independent: a leaked worktree must not strand the run."""
    host = _Host(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        workspace=tmp_path,
        worktrees=FailingWorktrees(),
    )

    result = run_health_review(
        host,  # type: ignore[arg-type]
        now=_clock([0, 10_000]),
        sleep=_no_sleep,
        timeout_s=1.0,
    )

    termination = result.termination
    assert termination is not None
    assert termination.clean is False
    assert "scratch-worktree removal" in termination.failures()
    assert termination.run_released is True
    assert host.shared.live_keys() == ()


def test_an_UNAVAILABLE_ledger_is_reported_as_a_failed_release_not_a_clean_one(tmp_path):
    """The production contract: the store REFUSES, it does not raise (F12).

    A caller that inferred success from "no exception" would report a leak-free
    teardown while the durable entry was still there, blocking every conflicting
    run until its lease expired.
    """
    host = _Host(flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path)
    session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
    host.state.active_sessions.append(session)
    assert host.ownership.claim(GlobalHealthReviewScope()).owned
    host.shared.unavailable = True

    outcome = host.terminate_tech_lead_session(session)

    assert outcome.run_released is False
    assert outcome.clean is False
    assert "tech-lead run release" in outcome.failures()
    assert host.shared.live_keys() == (GlobalHealthReviewScope().run_key,), (
        "the durable hold is still there, so the outcome must not claim otherwise"
    )


def test_an_unavailable_release_KEEPS_the_lease_so_a_later_tick_retries(tmp_path):
    """Dropping the lease would strand the durable entry until it expired."""
    host = _Host(flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path)
    session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
    host.state.active_sessions.append(session)
    assert host.ownership.claim(GlobalHealthReviewScope()).owned
    host.shared.unavailable = True
    host.terminate_tech_lead_session(session)

    assert host.ownership.owns(GlobalHealthReviewScope().run_key), (
        "the engine must still remember the hold in order to retry it"
    )

    # The store comes back; the next reconciliation finds a held run that is no
    # longer live and hands it back.
    host.shared.unavailable = False
    host.ownership.reconcile([])

    assert host.shared.live_keys() == ()


def test_a_raising_release_is_also_reported_rather_than_silently_clean(tmp_path):
    """The other failure shape: an owner that blows up must not read as clean."""

    class RefusingOwnership:
        def end_run(self, run_key: str) -> None:
            raise RuntimeError(f"coordination store unreachable for {run_key}")

    host = _Host(flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path)
    host.deps.run_ownership = RefusingOwnership()
    session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
    host.state.active_sessions.append(session)

    outcome = host.terminate_tech_lead_session(session)

    assert outcome.run_released is False
    assert outcome.clean is False
    assert "tech-lead run release" in outcome.failures()


def test_a_stamped_session_with_NO_run_ownership_wired_fails_loudly(tmp_path):
    """A composition error must not masquerade as a successful no-op."""
    host = _Host(flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path)
    host.deps.run_ownership = None
    session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
    host.state.active_sessions.append(session)

    outcome = host.terminate_tech_lead_session(session)

    assert outcome.run_released is False
    assert outcome.clean is False


def test_a_session_with_no_launch_stamp_has_no_run_to_release(tmp_path):
    """Nothing to release is not a failure — and must not read as one."""
    host = _Host(flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path)
    session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
    session.tech_lead_scope = None  # type: ignore[assignment]
    host.state.active_sessions.append(session)

    outcome = host.terminate_tech_lead_session(session)

    assert outcome.run_released is True
    assert outcome.clean is True


def test_the_hold_a_termination_releases_is_the_one_the_launch_took(tmp_path):
    """Scope in, scope out — derived from the session's own launch stamp."""
    host = _Host(flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION, workspace=tmp_path)
    tech_lead = _queued(FOCUS, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
    host.state.pending_tech_lead_reviews.append(tech_lead)

    session = host.launch_tech_lead_session(tech_lead)
    assert session is not None
    assert host.shared.live_keys() == (IssueInvestigationScope(FOCUS).run_key,)

    host.terminate_tech_lead_session(session)

    assert host.shared.live_keys() == ()
    assert GlobalHealthReviewScope().run_key not in host.shared.live_keys()


# --- The reap this owner performs preserves the run's evidence first (#360) ---


class TestTerminationCapturesBeforeItReaps:
    """The rule belongs to the owner that destroys the checkout.

    Termination is reached by two live callers — the on-demand drive loop
    hitting its deadline, and a stop of a run this engine can no longer prove it
    owns — and NEITHER reaches the completion handoff, so the capture wired
    there covers neither.
    """

    def test_the_capture_is_already_durable_when_the_checkout_is_destroyed(
        self, tmp_path
    ):
        host = _Host(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION, workspace=tmp_path
        )
        session = FakeSession(
            FOCUS, TechLeadSessionFlavor.FAILURE_INVESTIGATION, tmp_path
        )
        stage_evidence(session)
        destination = _capture_dir(host, session)
        worktrees = ReapingWorktrees(lambda: (destination / "manifest.json").exists())
        host.deps.worktree_manager = worktrees
        host.state.active_sessions.append(session)

        outcome = host.terminate_tech_lead_session(session)

        assert outcome.worktree_removed is True
        assert worktrees.removed == [session.worktree_path]
        assert worktrees.captured_before_removal is True, (
            "the evidence must be copied out BEFORE the removal, not after"
        )
        # The source is genuinely gone, and the capture genuinely survived it.
        assert not session.run_assets.run_dir.exists()
        assert (destination / "manifest.json").read_text(
            encoding="utf-8"
        ) == MANIFEST_BODY
        assert (destination / "board-snapshot.json").exists()
        assert (destination / "candidates" / "1234" / "diff.patch").exists()
        assert (destination / CAPTURE_RECEIPT_FILENAME).exists()
        [event] = host.events.get_events(
            EventName.TECH_LEAD_EVIDENCE_CAPTURED.value
        )
        assert event.data["preserved"] is True

    def test_a_drive_loop_timeout_preserves_the_run_it_gives_up_on(self, tmp_path):
        """The R29 #354 shape, end to end: a focused investigation hangs past its
        deadline and is terminated, with no completion handoff anywhere on the
        path — and its launch inputs still outlive the force-removed checkout."""
        host = _Host(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION, workspace=tmp_path
        )
        worktrees = ReapingWorktrees(lambda: True)
        host.deps.worktree_manager = worktrees

        results = run_targeted_investigations(
            host,  # type: ignore[arg-type]
            [FOCUS],
            now=_clock([0, 0, 10_000]),
            sleep=_no_sleep,
            timeout_s=1.0,
        )

        assert results[0].status is TechLeadOutcomeStatus.TIMED_OUT
        assert host.tick_count == 1, "the run must have staged evidence before dying"
        assert worktrees.removed, "the timeout path force-removes the scratch checkout"
        destination = tech_lead_evidence_capture_dir(
            host.config.repo_root,
            session_name=f"issue-{FOCUS}",
            run_id="20260603T000000000000Z",
        )
        assert (destination / "manifest.json").read_text(
            encoding="utf-8"
        ) == MANIFEST_BODY
        assert (destination / "candidates" / "1234" / "diff.patch").exists()

    def test_a_non_disposable_checkout_is_captured_too(self, tmp_path):
        """A lost-ownership stop leaves the tree standing but drops the session
        from ``active_sessions``, so orphan recovery force-removes it on a later
        tick. Capture cannot be conditional on ``disposable``."""
        host = _Host(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path
        )
        session = FakeSession(
            ANCHOR,
            TechLeadSessionFlavor.HEALTH_REVIEW,
            tmp_path,
            scratch_worktree=False,
        )
        stage_evidence(session)
        worktrees = ReapingWorktrees(lambda: True)
        host.deps.worktree_manager = worktrees
        host.state.active_sessions.append(session)

        outcome = host.terminate_tech_lead_session(session)

        assert worktrees.removed == [], "a non-scratch checkout is not reaped here"
        assert outcome.leaked_worktree is None
        assert session.worktree_path.exists()
        assert host.state.active_sessions == [], "...but the session is gone"
        assert (_capture_dir(host, session) / "manifest.json").exists()

    def test_a_capture_with_nothing_staged_says_so_without_making_the_teardown_unclean(
        self, tmp_path
    ):
        """A run terminated before it staged anything has nothing to preserve —
        an ordinary outcome, reported in full, that must not read as a leak the
        operator has to clean up by hand."""
        host = _Host(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path
        )
        session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
        host.state.active_sessions.append(session)
        assert host.ownership.claim(GlobalHealthReviewScope()).owned

        outcome = host.terminate_tech_lead_session(session)

        assert outcome.clean is True
        assert outcome.failures() == ()
        assert host.shared.live_keys() == ()
        [event] = host.events.get_events(
            EventName.TECH_LEAD_EVIDENCE_CAPTURED.value
        )
        assert event.data["preserved"] is False
        assert event.data["failure"]
        assert (_capture_dir(host, session) / CAPTURE_RECEIPT_FILENAME).exists()

    def test_a_raising_capture_never_strands_the_coordination_holds(
        self, tmp_path, monkeypatch
    ):
        """The capture is attempted like every other effect: even a broken one
        cannot abort the releases and the reap that follow it. Termination has
        no later tick to fall back on, so an unguarded capture would be the one
        way to leak a repository-wide hold."""
        from issue_orchestrator.control import tech_lead_evidence_capture

        def _boom(**_kwargs):
            raise RuntimeError("capture owner is broken")

        monkeypatch.setattr(
            tech_lead_evidence_capture,
            "capture_tech_lead_session_evidence",
            _boom,
        )
        host = _Host(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW, workspace=tmp_path
        )
        session = FakeSession(ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW, tmp_path)
        worktrees = ReapingWorktrees(lambda: True)
        host.deps.worktree_manager = worktrees
        host.state.active_sessions.append(session)
        assert host.ownership.claim(GlobalHealthReviewScope()).owned

        outcome = host.terminate_tech_lead_session(session)

        assert outcome.clean is True
        assert host.shared.live_keys() == ()
        assert host.killed == [session.terminal_id]
        assert worktrees.removed == [session.worktree_path]
