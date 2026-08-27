"""An ordinary engine stop must finish over HTTP, not over signals (#273).

#267's B2 canary ran a supported ``stop`` against a healthy R21 engine
and watched it degrade: the dashboard refused the shutdown POST with
``401 missing credentials`` because the supervisor sent no bearer, the
supervisor fell through to SIGTERM, and the run still reported the
engine stopped. Every layer looked green; the graceful phase had simply
stopped happening.

That failure is only visible end to end, so these tests assemble the
real pieces:

- a **real process** to stop, so ``process_is_alive`` answers from the
  kernel and the stop controller's wait is the real one;
- a **real gate** over a real socket — either a loopback endpoint
  refusing an unauthenticated POST the way the mounted route does, or
  the mounted dashboard route itself, served by uvicorn with its own
  middleware chain and the admin bearer #269 made a single owner;
- **recorded escalation**: SIGTERM, force-stop and port-kill are
  replaced by recorders, so "no signal was sent" is an assertion rather
  than a hope, and a regression cannot signal this test's own process
  group.

The recorders stop the engine when they run, which is the point: the
stop returns True whether it went gracefully or by signal, exactly as
it did live. Only the recorded route separates the two.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.entrypoints import web, web_operator_routes
from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.infra import browser_session, shutdown_timing, supervisor
from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR, default_token_path
from issue_orchestrator.infra.repo_identity import lock_file, state_dir
from issue_orchestrator.infra.repo_lock import LockInfo
from tests.integration.live_dashboard_server import LiveDashboardServer
from tests.shutdown_endpoint_server import AuthRequiringShutdownEndpoint

ENGINE_TOKEN = "graceful-stop-admin-token"
STOP_REASON = "operator stopped the repository engine"
STOP_ACTOR = "supervisor.stop"
GRACEFUL_TIMEOUT_SECONDS = 20.0
# Spent in full whenever nothing may be signalled, so keep it short.
UNAUTHORIZED_BUDGET_SECONDS = 1.0
ENGINE_EXIT_TIMEOUT_SECONDS = 10.0


class EngineProcess:
    """A real child process standing in for the repository engine.

    It has to be real: the stop controller probes liveness with
    ``os.kill(pid, 0)`` and waits until that answers "gone". It also
    has to be *reaped* when it exits — an unreaped zombie still answers
    signal-zero, so a test that only killed it would wait out the whole
    graceful budget and then blame the credential.
    """

    def __init__(self) -> None:
        self._process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        return self._process.pid

    def exit(self) -> None:
        """Exit the way a stopped engine does, and be reaped."""
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait(timeout=ENGINE_EXIT_TIMEOUT_SECONDS)

    def cleanup(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=ENGINE_EXIT_TIMEOUT_SECONDS)


@dataclass
class RecordedEscalation:
    """Stands in for every route out of the graceful phase.

    Each recorder stops the engine, because the real one would. What is
    asserted is whether any of them was needed.
    """

    engine: EngineProcess
    signals: list[tuple[int, bool]] = field(default_factory=list)
    force_stops: list[int] = field(default_factory=list)
    port_kills: list[int] = field(default_factory=list)

    def send_kill_signal(self, pid: int, force: bool) -> None:
        self.signals.append((pid, force))
        self.engine.exit()

    def force_stop(
        self,
        repo_root: Path,
        pid: int,
        port: int | None,
        instance_id: str | None,
    ) -> bool:
        self.force_stops.append(pid)
        self.engine.exit()
        return True

    def kill_by_port(self, port: int, use_sigkill: bool = False) -> bool:
        self.port_kills.append(port)
        self.engine.exit()
        return True

    @property
    def escalated(self) -> bool:
        return bool(self.signals or self.force_stops or self.port_kills)


@dataclass(frozen=True)
class StoppableEngine:
    """One engine: a process, its lock file, and its escalation record."""

    repo_root: Path
    process: EngineProcess
    escalation: RecordedEscalation

    def stop(
        self,
        *,
        graceful_timeout_seconds: float = GRACEFUL_TIMEOUT_SECONDS,
        force_if_graceful_fails: bool = True,
    ) -> bool:
        return supervisor.stop(
            self.repo_root,
            reason=STOP_REASON,
            actor=STOP_ACTOR,
            graceful_timeout_seconds=graceful_timeout_seconds,
            force_if_graceful_fails=force_if_graceful_fails,
        )

    def is_alive(self) -> bool:
        return shutdown_timing.process_is_alive(self.process.pid)

    def lock_exists(self) -> bool:
        return lock_file(self.repo_root).exists()


@pytest.fixture
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home, so no developer's real admin token leaks in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    return home


def _write_admin_token_file(token: str) -> Path:
    path = default_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return path


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


@pytest.fixture
def engine_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[int], StoppableEngine]]:
    """Builds an engine bound to a port, with escalation recorded."""
    processes: list[EngineProcess] = []

    def build(port: int) -> StoppableEngine:
        repo_root = tmp_path / "repo"
        repo_root.mkdir(exist_ok=True)
        process = EngineProcess()
        processes.append(process)
        escalation = RecordedEscalation(engine=process)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._send_kill_signal",
            escalation.send_kill_signal,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._force_stop",
            escalation.force_stop,
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._kill_by_port",
            escalation.kill_by_port,
        )
        _publish_lock(repo_root, pid=process.pid, port=port)
        return StoppableEngine(
            repo_root=repo_root, process=process, escalation=escalation
        )

    try:
        yield build
    finally:
        for process in processes:
            process.cleanup()


@pytest.mark.integration
def test_an_authenticated_stop_completes_without_any_signal(
    private_home: Path,
    engine_factory: Callable[[int], StoppableEngine],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The B2 property, stated directly: HTTP finished the stop.

    The gate refuses anything but the admin bearer, so a 200 here is
    proof the supervisor presented the operator's existing credential —
    and the empty escalation record is proof nothing had to be killed.
    """
    _write_admin_token_file(ENGINE_TOKEN)
    caplog.set_level(logging.DEBUG)
    engine: StoppableEngine | None = None

    def engine_exits() -> None:
        assert engine is not None
        engine.process.exit()

    endpoint = AuthRequiringShutdownEndpoint(
        token=ENGINE_TOKEN, on_accepted=engine_exits
    )
    port = endpoint.start()
    engine = engine_factory(port)
    try:
        stopped = engine.stop()
    finally:
        endpoint.stop()

    assert stopped is True
    assert [request.status for request in endpoint.requests] == [200]
    assert endpoint.requests[0].authorization == f"Bearer {ENGINE_TOKEN}"
    assert engine.escalation.escalated is False, (
        f"a graceful stop still escalated: {engine.escalation}"
    )
    assert engine.lock_exists() is False
    assert ENGINE_TOKEN not in caplog.text


@pytest.mark.integration
def test_a_refused_bearer_no_longer_buys_a_signal(
    private_home: Path,
    engine_factory: Callable[[int], StoppableEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #324 failure, fail-closed (#326).

    A superseded credential is refused, so the request is unconfirmed.
    Under the old disposition that was permission to SIGTERM the
    engine's process group immediately, and the stop still reported
    success. Now the budget is spent observing a target nobody
    authorized signalling: no signal, no force, no port kill, the
    engine still running, its lock still published, and a truthful
    False.
    """
    monkeypatch.setenv(TOKEN_ENV_VAR, "a-superseded-admin-token")
    endpoint = AuthRequiringShutdownEndpoint(token=ENGINE_TOKEN)
    port = endpoint.start()
    engine = engine_factory(port)
    try:
        stopped = engine.stop(
            graceful_timeout_seconds=UNAUTHORIZED_BUDGET_SECONDS,
            force_if_graceful_fails=False,
        )
    finally:
        endpoint.stop()

    assert stopped is False
    assert [request.status for request in endpoint.requests] == [401]
    assert engine.escalation.escalated is False, (
        f"an unauthorized stop still escalated: {engine.escalation}"
    )
    assert engine.is_alive() is True
    assert engine.lock_exists() is True


@pytest.mark.integration
def test_a_refused_bearer_escalates_only_where_force_was_authorized(
    private_home: Path,
    engine_factory: Callable[[int], StoppableEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorized escalation keeps its authority — after the budget.

    ``force_if_graceful_fails`` is the operator's standing permission
    to force on timeout. It still runs, and it still runs through the
    force path; what no longer happens is the bare SIGTERM the moment
    the request came back unconfirmed.
    """
    monkeypatch.setenv(TOKEN_ENV_VAR, "a-superseded-admin-token")
    endpoint = AuthRequiringShutdownEndpoint(token=ENGINE_TOKEN)
    port = endpoint.start()
    engine = engine_factory(port)
    try:
        stopped = engine.stop(
            graceful_timeout_seconds=UNAUTHORIZED_BUDGET_SECONDS,
            force_if_graceful_fails=True,
        )
    finally:
        endpoint.stop()

    assert stopped is True
    assert [request.status for request in endpoint.requests] == [401]
    assert engine.escalation.signals == []
    assert engine.escalation.force_stops == [engine.process.pid]


@pytest.mark.integration
def test_an_unconfirmed_request_still_finishes_when_the_engine_retires(
    private_home: Path,
    engine_factory: Callable[[int], StoppableEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfirmed answer says nothing about the engine's intent.

    Here the engine retires itself while the caller's request is being
    refused. The stop must notice the real exit, report success, and
    release the lock exactly once — without any escalation having run.
    """
    monkeypatch.setenv(TOKEN_ENV_VAR, "a-superseded-admin-token")
    engine: StoppableEngine | None = None

    def engine_retires_anyway() -> None:
        assert engine is not None
        engine.process.exit()

    endpoint = AuthRequiringShutdownEndpoint(
        token=ENGINE_TOKEN, on_refused=engine_retires_anyway
    )
    port = endpoint.start()
    engine = engine_factory(port)
    try:
        stopped = engine.stop(force_if_graceful_fails=False)
    finally:
        endpoint.stop()

    assert stopped is True
    assert [request.status for request in endpoint.requests] == [401]
    assert engine.escalation.escalated is False
    assert engine.is_alive() is False
    assert engine.lock_exists() is False


@pytest.mark.integration
def test_an_engine_with_no_admin_token_still_stops_gracefully(
    private_home: Path,
    engine_factory: Callable[[int], StoppableEngine],
) -> None:
    """``--dev-no-auth`` must not be collateral damage.

    No token exists anywhere, so the supervisor sends none, an open
    gate accepts it, and the stop stays graceful. Asking for a shutdown
    must not mint a credential either.
    """
    engine: StoppableEngine | None = None

    def engine_exits() -> None:
        assert engine is not None
        engine.process.exit()

    endpoint = AuthRequiringShutdownEndpoint(token=None, on_accepted=engine_exits)
    port = endpoint.start()
    engine = engine_factory(port)
    try:
        stopped = engine.stop()
    finally:
        endpoint.stop()

    assert stopped is True
    assert endpoint.requests[0].authorization is None
    assert endpoint.requests[0].status == 200
    assert engine.escalation.escalated is False
    assert not default_token_path().exists()


@pytest.fixture
def live_dashboard(
    private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[LiveDashboardServer]:
    """The mounted dashboard, served by uvicorn with auth enforced.

    The admin bearer is configured on #269's single owner and written to
    the operator's token file — the exact pairing the live canary had,
    and the one that produced a 401.
    """
    previous_admin = get_configured_api_token()
    previous_agent = get_configured_agent_callback_token()
    _write_admin_token_file(ENGINE_TOKEN)
    browser_session.shutdown()
    browser_session.initialize(admin_token=ENGINE_TOKEN)
    configure_api_token(ENGINE_TOKEN, agent_callback=None)

    server = LiveDashboardServer(name="web_dashboard")
    server.start()
    try:
        yield server
    finally:
        server.stop()
        browser_session.shutdown()
        configure_api_token(previous_admin, agent_callback=previous_agent)


@pytest.mark.integration
def test_the_mounted_dashboard_accepts_the_supervisors_bearer(
    live_dashboard: LiveDashboardServer,
    engine_factory: Callable[[int], StoppableEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client and the server, judged against each other.

    A hand-rolled gate can only prove the supervisor sends *a* bearer.
    This runs the request through the route that refused it live: the
    dashboard's own ``/api/shutdown``, behind the real middleware chain
    and #269's admin owner. The engine exits when the route accepts the
    shutdown, which is where a real engine begins exiting.
    """
    engine: StoppableEngine | None = None
    shutdown_requests: list[str] = []

    def record_shutdown(reason: str = "unknown") -> bool:
        shutdown_requests.append(reason)
        assert engine is not None
        engine.process.exit()
        return True

    web.set_orchestrator(MagicMock())
    # The route arms a timer that would ``os._exit`` this test process.
    monkeypatch.setattr(
        web_operator_routes.shutdown_manager, "exit", lambda: None
    )
    monkeypatch.setattr(
        web_operator_routes.shutdown_manager, "request_shutdown", record_shutdown
    )

    port = live_dashboard.port
    assert port is not None
    engine = engine_factory(port)
    try:
        stopped = engine.stop()
    finally:
        web.set_orchestrator(None)

    assert stopped is True
    assert len(shutdown_requests) == 1, (
        "the mounted dashboard never accepted the supervisor's shutdown"
    )
    assert STOP_REASON in shutdown_requests[0]
    assert engine.escalation.escalated is False, (
        f"the supported stop still escalated: {engine.escalation}"
    )
    assert engine.lock_exists() is False
