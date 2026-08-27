"""A non-force engine stop may not signal an engine it failed to ask (#326).

#324's post-merge cleanup ran the supported standalone Control Center
route against a scoped engine with every force flag false. The response
said ``status=stopped, stopped_count=1``; the evidence said something
else — ``Sending SIGTERM to orchestrator process group`` in the same
second as the shutdown request, exit code 143, no shutdown-manager
lines, and a ``repo.lock`` left behind. The operator issued no signal.
The supervisor did, because a graceful request that came back
unconfirmed was treated as permission to escalate.

The rule these tests pin is fail-closed and has one owner:

    **Failure to confirm a graceful shutdown request is not authority
    to signal the engine.**

Both non-force surfaces are covered here — the tracked-lock ``stop``
and the port-only ``stop_by_port`` — because both now run through
``InterruptibleStopController``. Restoring either the immediate
``terminate()`` on an unconfirmed request or the half-second port
SIGTERM fails these tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.infra import supervisor
from issue_orchestrator.infra.repo_identity import lock_file, state_dir
from issue_orchestrator.infra.repo_lock import LockInfo
from issue_orchestrator.infra.shutdown_timing import (
    InterruptibleStopController,
    StaticStopPolicy,
    StopOutcome,
)

BUDGET_SECONDS = 5.0
ENGINE_PORT = 19080
STOP_REASON = "operator stopped the repository engine"


class FakeClock:
    """A clock the test advances, so no test waits on a real one."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Escalations:
    """Every route out of the graceful phase, recorded rather than run."""

    def __init__(self) -> None:
        self.force_stops: list[float] = []

    def force_stop(self, at: float) -> bool:
        self.force_stops.append(at)
        return True


def _publish_lock(repo_root: Path, *, pid: int, port: int) -> None:
    """Advertise a running engine the way ``acquire_lock`` does."""
    info = LockInfo(
        repo_root=str(repo_root),
        pid=pid,
        started_at=datetime.now(timezone.utc).isoformat(),
        http_port=port,
        state_dir=str(state_dir(repo_root)),
    )
    path = lock_file(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info.to_dict()), encoding="utf-8")


class TestUnconfirmedGracefulRequest:
    """Requirement 1: an unconfirmed request buys observation, not a signal."""

    def test_unconfirmed_request_spends_the_budget_without_signalling(self) -> None:
        clock = FakeClock()
        escalations = Escalations()

        controller = InterruptibleStopController(
            StaticStopPolicy(graceful_timeout_seconds=BUDGET_SECONDS),
            target_alive=lambda: True,
            force_requested=False,
            force_on_timeout=False,
            request_graceful=lambda: False,
            force_stop=lambda: escalations.force_stop(clock.now),
            on_stopped=lambda: pytest.fail("a running engine was reported stopped"),
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        outcome = controller.stop()

        assert outcome is StopOutcome.TIMED_OUT
        assert escalations.force_stops == []
        assert clock.now == pytest.approx(BUDGET_SECONDS)

    def test_unconfirmed_request_still_notices_a_natural_exit(self) -> None:
        """Requirement 4: exit observed, cleanup run exactly once, no force."""
        clock = FakeClock()
        escalations = Escalations()
        cleanups: list[float] = []
        probes = iter([True, True, False])

        controller = InterruptibleStopController(
            StaticStopPolicy(graceful_timeout_seconds=BUDGET_SECONDS),
            target_alive=lambda: next(probes),
            force_requested=False,
            force_on_timeout=False,
            request_graceful=lambda: False,
            force_stop=lambda: escalations.force_stop(clock.now),
            on_stopped=lambda: cleanups.append(clock.now),
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        outcome = controller.stop()

        assert outcome is StopOutcome.STOPPED
        assert cleanups == [pytest.approx(0.2)]
        assert escalations.force_stops == []

    def test_a_confirmed_request_is_also_observed_not_assumed(self) -> None:
        """A 200 is not proof of exit either; the same budget is observed."""
        clock = FakeClock()
        escalations = Escalations()

        controller = InterruptibleStopController(
            StaticStopPolicy(graceful_timeout_seconds=BUDGET_SECONDS),
            target_alive=lambda: True,
            force_requested=False,
            force_on_timeout=False,
            request_graceful=lambda: True,
            force_stop=lambda: escalations.force_stop(clock.now),
            on_stopped=lambda: pytest.fail("a running engine was reported stopped"),
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert controller.stop() is StopOutcome.TIMED_OUT
        assert escalations.force_stops == []


class TestAuthorizedForceIsUntouched:
    """Requirements 3 and 5: force keeps exactly the authority it had."""

    def test_force_on_timeout_escalates_only_after_the_budget(self) -> None:
        clock = FakeClock()
        escalations = Escalations()

        controller = InterruptibleStopController(
            StaticStopPolicy(graceful_timeout_seconds=BUDGET_SECONDS),
            target_alive=lambda: True,
            force_requested=False,
            force_on_timeout=True,
            request_graceful=lambda: False,
            force_stop=lambda: escalations.force_stop(clock.now),
            on_stopped=lambda: None,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert controller.stop() is StopOutcome.STOPPED
        assert escalations.force_stops == [pytest.approx(BUDGET_SECONDS)]

    def test_explicit_force_still_skips_the_graceful_request(self) -> None:
        clock = FakeClock()
        escalations = Escalations()
        requests: list[str] = []

        controller = InterruptibleStopController(
            StaticStopPolicy(graceful_timeout_seconds=BUDGET_SECONDS, force=True),
            target_alive=lambda: True,
            force_requested=True,
            force_on_timeout=False,
            request_graceful=lambda: requests.append("asked") is None,
            force_stop=lambda: escalations.force_stop(clock.now),
            on_stopped=lambda: None,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert controller.stop() is StopOutcome.STOPPED
        assert requests == []
        assert escalations.force_stops == [pytest.approx(0.0)]

    def test_a_failed_force_is_not_reported_as_a_stop(self) -> None:
        controller = InterruptibleStopController(
            StaticStopPolicy(graceful_timeout_seconds=BUDGET_SECONDS, force=True),
            target_alive=lambda: True,
            force_requested=True,
            force_on_timeout=False,
            request_graceful=lambda: False,
            force_stop=lambda: False,
            on_stopped=lambda: None,
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )

        assert controller.stop() is StopOutcome.FORCE_FAILED


class TestTrackedStopSendsNoSignal:
    """The seam #324 measured: ``supervisor.stop`` with every flag false."""

    @pytest.fixture
    def signal_recorder(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        """Record any real signal delivery instead of performing it.

        ``os.kill(pid, 0)`` is a liveness probe, not a signal, so it is
        allowed through unrecorded — everything else is the failure this
        test exists to catch.
        """
        delivered: list[tuple[int, int]] = []

        def record_kill(pid: int, sig: int) -> None:
            if sig != 0:
                delivered.append((pid, sig))

        def record_killpg(pgid: int, sig: int) -> None:
            delivered.append((pgid, sig))

        monkeypatch.setattr(supervisor.os, "kill", record_kill)
        monkeypatch.setattr(supervisor.os, "killpg", record_killpg)
        return delivered

    @pytest.fixture
    def live_engine(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """A tracked engine that stays alive for the whole budget."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _publish_lock(repo_root, pid=4242, port=ENGINE_PORT)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.shutdown_timing.process_is_alive",
            lambda _pid: True,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._request_graceful_shutdown",
            lambda *_args, **_kwargs: False,
        )
        return repo_root

    def test_timeout_without_force_authority_leaves_the_engine_running(
        self,
        live_engine: Path,
        signal_recorder: list[tuple[int, int]],
    ) -> None:
        stopped = supervisor.stop(
            live_engine,
            force=False,
            reason=STOP_REASON,
            graceful_timeout_seconds=0.2,
            force_if_graceful_fails=False,
        )

        assert stopped is False
        assert signal_recorder == [], "a non-force stop signalled the engine"
        assert lock_file(live_engine).exists(), (
            "the lock was released for an engine that never exited"
        )

    def test_a_stale_lock_is_still_reconciled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        signal_recorder: list[tuple[int, int]],
        tmp_path: Path,
    ) -> None:
        """Ordinary stale-lock reconciliation is unchanged by the invariant."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _publish_lock(repo_root, pid=4242, port=ENGINE_PORT)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.shutdown_timing.process_is_alive",
            lambda _pid: False,
        )

        stopped = supervisor.stop(
            repo_root,
            force=False,
            reason=STOP_REASON,
            graceful_timeout_seconds=0.2,
            force_if_graceful_fails=False,
        )

        assert stopped is True
        assert signal_recorder == []
        assert not lock_file(repo_root).exists()


class TestPortStopSendsNoSignal:
    """Requirement 5: the port path carries the same non-force meaning."""

    @pytest.fixture
    def port_kills(self, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
        kills: list[bool] = []

        def record_kill_by_port(port: int, use_sigkill: bool = False) -> bool:
            kills.append(use_sigkill)
            return True

        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._kill_by_port",
            record_kill_by_port,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._is_port_in_use",
            lambda _port: True,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._request_graceful_shutdown",
            lambda *_args, **_kwargs: False,
        )
        return kills

    def test_a_port_still_in_use_is_not_a_licence_to_kill(
        self,
        port_kills: list[bool],
    ) -> None:
        stopped = supervisor.stop_by_port(
            ENGINE_PORT,
            reason=STOP_REASON,
            graceful_timeout_seconds=0.2,
        )

        assert stopped is False
        assert port_kills == [], (
            "a non-force port stop killed the process holding the port"
        )

    def test_explicit_force_still_kills_by_port(
        self,
        port_kills: list[bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._is_port_in_use",
            lambda _port: False,
        )

        stopped = supervisor.stop_by_port(
            ENGINE_PORT,
            reason=STOP_REASON,
            force=True,
            graceful_timeout_seconds=0.2,
        )

        assert stopped is True
        assert port_kills == [True]
