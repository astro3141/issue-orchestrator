"""An authenticated ``/api/shutdown`` must answer over a real socket (#277).

#269 gave both HTTP surfaces one admin bearer; #273 made the supervisor
present it. #267's V3 B2 canary then recorded **zero** auth rejections
and still lost the stop:

    request start                   21:12:49.292
    auth rejection count            0
    client HTTP timeout / SIGTERM   21:12:51.304
    elapsed                         +2.012 s

which is ``_request_graceful_shutdown``'s 2.0 s budget to the
millisecond. The handler had torn down its own uvicorn server before
building the ``JSONResponse``, so the acknowledgment never reached the
wire and a healthy engine was killed by signal.

Two deployment facts made that fatal, and both are reproduced here.

The first is the server: ``run_web_dashboard`` configures
``timeout_graceful_shutdown=0``, so once ``should_exit`` is seen, an
in-flight request task is cancelled rather than awaited. These tests run
the mounted dashboard under a real uvicorn server with that same value
and point ``web.set_server`` at it, so the production
``trigger_server_shutdown`` sets ``should_exit`` and ``force_exit`` on
the server actually serving the request. That is what the existing #273
test could not do — it left ``web._server`` unset, which made the
destructive call a no-op and the defect invisible.

The second is the loop: ``run_with_web_dashboard`` runs the engine on the
same event loop as the dashboard, so from the moment teardown begins the
loop has other things to do. :class:`TeardownOccupiesTheEngineLoop`
states that as a rule instead of a race — the loop is held from the
instant the teardown flags land until the test releases it. Anything the
handler still owed its caller at that point is simply never delivered,
which is precisely what "+2.012 s, no response" was. With the
acknowledgment sent first, the same occupancy is harmless.

The one thing intercepted is ``shutdown_manager.exit`` — the final
process-exit owner, which would ``os._exit`` this test run. The
web-server teardown that caused the failure is left entirely alone.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from issue_orchestrator.entrypoints import web, web_operator_routes
from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.infra import browser_session, supervisor
from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR, default_token_path
from issue_orchestrator.infra.repo_identity import lock_file, state_dir
from issue_orchestrator.infra.repo_lock import LockInfo
from tests.integration.live_dashboard_server import LiveDashboardServer

ENGINE_TOKEN = "response-order-admin-token"
STOP_REASON = "operator stopped the repository engine"
STOP_ACTOR = "supervisor.stop"

# The budget the supervisor actually gives the graceful phase, so
# "the response arrived" and "the supervisor would have counted it"
# are one assertion.
SUPERVISOR_HTTP_TIMEOUT_SECONDS = 2.0
GRACEFUL_TIMEOUT_SECONDS = 20.0
PROCESS_EXIT_TIMEOUT_SECONDS = 10.0
ENGINE_EXIT_TIMEOUT_SECONDS = 10.0
# How long teardown holds the engine loop when nobody releases it
# sooner: exactly as long as a caller is allowed to wait for its answer.
# No longer, so a passing run never pays for it; no shorter, so a
# regression cannot slip a late response in under the wire.
LOOP_OCCUPANCY_SECONDS = SUPERVISOR_HTTP_TIMEOUT_SECONDS
# Bounded wait for the deferred teardown to start after the ACK is in
# the client's hands. Only the ordering is under test, not the latency.
TEARDOWN_TIMEOUT_SECONDS = 10.0
# ``run_web_dashboard`` deploys with this exact value: once shutdown
# starts, an unfinished request task is cancelled, not awaited.
DEPLOYED_GRACEFUL_SHUTDOWN_TIMEOUT = 0


class RecordedProcessExit:
    """The one thing that must not really happen in a test process."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.codes: list[int] = []

    def exit(self, code: int = 0) -> None:
        self.codes.append(code)
        self.requested.set()

    def wait(self) -> bool:
        return self.requested.wait(PROCESS_EXIT_TIMEOUT_SECONDS)


class TeardownOccupiesTheEngineLoop:
    """The deployed server, plus the deployed loop contention.

    ``web.trigger_server_shutdown`` writes ``should_exit`` and then
    ``force_exit``; both land on the real ``uvicorn.Server``, which
    really does stop. What this adds is the engine's own loop: the
    deployment shares one loop between the dashboard and the
    orchestrator's tick, so teardown does not run in a quiet process.
    Holding the loop from the second flag makes that a rule rather than
    a race — after this point the handler gets no more turns, so
    anything it still owed the caller is lost.
    """

    def __init__(self, server: Any) -> None:
        self._server = server
        self.began = threading.Event()
        self.resume = threading.Event()
        self.exit_flags: list[bool] = []

    @property
    def should_exit(self) -> bool:
        return bool(self._server.should_exit)

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self.exit_flags.append(value)
        self._server.should_exit = value

    @property
    def force_exit(self) -> bool:
        return bool(self._server.force_exit)

    @force_exit.setter
    def force_exit(self, value: bool) -> None:
        self._server.force_exit = value
        self.began.set()
        self.resume.wait(LOOP_OCCUPANCY_SECONDS)

    def release(self) -> None:
        """Give the engine loop back."""
        self.resume.set()


@dataclass
class DeployedDashboard:
    """A live dashboard and the teardown it is going to perform."""

    server: LiveDashboardServer
    teardown: TeardownOccupiesTheEngineLoop
    process_exit: RecordedProcessExit

    @property
    def port(self) -> int:
        assert self.server.port is not None
        return self.server.port

    def shutdown_request(self) -> urllib.request.Request:
        return urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/shutdown",
            method="POST",
            data=json.dumps(
                {"reason": STOP_REASON, "actor": STOP_ACTOR}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ENGINE_TOKEN}",
            },
        )


class OrchestratorStub:
    """An engine that records its own shutdown request and nothing else."""

    def __init__(self) -> None:
        self.state = SimpleNamespace(active_sessions=[])
        self.shutdown_requests: list[bool] = []

    def request_shutdown(self, force: bool = False) -> None:
        self.shutdown_requests.append(force)


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


@pytest.fixture
def orchestrator() -> Iterator[OrchestratorStub]:
    stub = OrchestratorStub()
    web.set_orchestrator(stub)
    try:
        yield stub
    finally:
        web.set_orchestrator(None)


@pytest.fixture
def dashboard(
    private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[DeployedDashboard]:
    """The dashboard as deployed: real uvicorn, real gate, real teardown."""
    previous_admin = get_configured_api_token()
    previous_agent = get_configured_agent_callback_token()
    _write_admin_token_file(ENGINE_TOKEN)
    browser_session.shutdown()
    browser_session.initialize(admin_token=ENGINE_TOKEN)
    configure_api_token(ENGINE_TOKEN, agent_callback=None)

    process_exit = RecordedProcessExit()
    monkeypatch.setattr(
        web_operator_routes.shutdown_manager, "exit", process_exit.exit
    )

    server = LiveDashboardServer(
        name="web_dashboard",
        timeout_graceful_shutdown=DEPLOYED_GRACEFUL_SHUTDOWN_TIMEOUT,
    )
    server.start()
    teardown = TeardownOccupiesTheEngineLoop(server.uvicorn_server())
    web.set_server(teardown)
    try:
        yield DeployedDashboard(
            server=server, teardown=teardown, process_exit=process_exit
        )
    finally:
        teardown.release()
        web.set_server(None)
        server.stop()
        browser_session.shutdown()
        configure_api_token(previous_admin, agent_callback=previous_agent)


@pytest.mark.integration
def test_the_acknowledgment_arrives_before_the_engine_loop_is_taken(
    dashboard: DeployedDashboard, orchestrator: OrchestratorStub
) -> None:
    """Status *and* body, read off a socket, inside the real budget.

    The loop is held for as long as this client is waiting, so a 200
    here can only mean the response was already written when teardown
    began. Restore the old ``trigger_server_shutdown()``-before-return
    ordering and this raises a timeout instead — the same one the live
    canary measured at +2.012 s.
    """
    request = dashboard.shutdown_request()
    try:
        with urllib.request.urlopen(
            request, timeout=SUPERVISOR_HTTP_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
        teardown_began = dashboard.teardown.began.wait(TEARDOWN_TIMEOUT_SECONDS)
    finally:
        dashboard.teardown.release()

    assert status == 200
    assert body == {
        "status": "shutdown_requested",
        "active_sessions": 0,
        "reason": STOP_REASON,
        "actor": STOP_ACTOR,
    }
    assert orchestrator.shutdown_requests == [False]
    assert teardown_began, (
        "the destructive teardown never ran — /api/shutdown answered but "
        "left the server up"
    )


@pytest.mark.integration
def test_the_deferred_teardown_really_stops_the_server(
    dashboard: DeployedDashboard, orchestrator: OrchestratorStub
) -> None:
    """Deferring is not cancelling.

    An ACK-only ``/api/shutdown`` would pass every ordering assertion and
    leave the engine running. So: the real uvicorn server stops serving,
    a later request finds nothing there, and the scheduled process exit
    is reached with the code the handler asks for.
    """
    try:
        with urllib.request.urlopen(
            dashboard.shutdown_request(), timeout=SUPERVISOR_HTTP_TIMEOUT_SECONDS
        ) as response:
            assert response.status == 200
        assert dashboard.teardown.began.wait(TEARDOWN_TIMEOUT_SECONDS)
    finally:
        dashboard.teardown.release()

    assert dashboard.teardown.exit_flags == [True]
    assert dashboard.teardown.force_exit is True

    assert dashboard.server.wait_until_stopped(), (
        "the dashboard server kept running after an accepted shutdown"
    )
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(
            dashboard.shutdown_request(), timeout=SUPERVISOR_HTTP_TIMEOUT_SECONDS
        ).close()
    assert dashboard.process_exit.wait(), "the scheduled process exit never ran"
    assert dashboard.process_exit.codes == [0]


class EngineProcess:
    """A real child process standing in for the repository engine.

    The stop controller probes liveness with ``os.kill(pid, 0)`` and
    waits until that answers "gone", so this has to be a real process —
    and it has to be reaped, because an unreaped zombie still answers.
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
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait(timeout=ENGINE_EXIT_TIMEOUT_SECONDS)

    def cleanup(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=ENGINE_EXIT_TIMEOUT_SECONDS)


@dataclass
class RecordedEscalation:
    """Every route out of the graceful phase, recorded rather than taken.

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


@pytest.mark.integration
def test_the_supported_stop_completes_without_any_signal_fallback(
    dashboard: DeployedDashboard,
    orchestrator: OrchestratorStub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B2, stated end to end against the real ordering.

    Same supervisor path #273 proved the credential with, now against a
    dashboard that really tears itself down. SIGTERM, force-stop and
    port-kill are recorders, so "no signal was sent" is an assertion
    rather than a hope — and a regression cannot signal this test's own
    process group.
    """
    caplog.set_level(logging.DEBUG)
    engine = EngineProcess()
    escalation = RecordedEscalation(engine=engine)
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._send_kill_signal",
        escalation.send_kill_signal,
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._force_stop", escalation.force_stop
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._kill_by_port", escalation.kill_by_port
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _publish_lock(repo_root, pid=engine.pid, port=dashboard.port)

    # The engine process exits when the dashboard reaches its own
    # process-exit owner, which is where a real engine begins exiting.
    def exit_engine_when_the_dashboard_does() -> None:
        if dashboard.process_exit.wait():
            engine.exit()

    exit_watcher = threading.Thread(
        target=exit_engine_when_the_dashboard_does, daemon=True
    )
    exit_watcher.start()

    try:
        stopped = supervisor.stop(
            repo_root,
            reason=STOP_REASON,
            actor=STOP_ACTOR,
            graceful_timeout_seconds=GRACEFUL_TIMEOUT_SECONDS,
        )
    finally:
        dashboard.teardown.release()
        exit_watcher.join(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
        engine.cleanup()

    assert stopped is True
    assert orchestrator.shutdown_requests == [False], (
        "the mounted dashboard never accepted the supervisor's shutdown"
    )
    assert escalation.escalated is False, (
        "the supported stop still escalated: "
        f"signals={escalation.signals} force_stops={escalation.force_stops} "
        f"port_kills={escalation.port_kills}"
    )
    assert lock_file(repo_root).exists() is False
    assert ENGINE_TOKEN not in caplog.text
