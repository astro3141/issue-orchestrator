"""The shutdown ACK must leave before the server is torn down (#277).

#267's V3 B2 canary sent an authenticated ``POST /api/shutdown`` to a
healthy R21 engine, recorded zero auth rejections, and still lost the
stop: the handler set ``should_exit``/``force_exit`` on its own uvicorn
server before it built the ``JSONResponse``, so the supervisor waited
out its whole 2.0 s HTTP budget and fell back to SIGTERM.

Wall-clock evidence cannot pin that. The ordering can be stated exactly,
though, because it is an ASGI-message ordering: the destructive teardown
must land *after* the terminal ``http.response.body`` message has been
accepted by the layer below. So these tests wrap the whole dashboard app
— outside every middleware, where the server's own ``send`` sits — and
record teardown and response messages onto one timeline.

The real ``web.trigger_server_shutdown`` runs here, against a server
object that records the moment it is told to exit. That is the exact
production call; only the object it mutates belongs to the test. The
real-uvicorn half of the proof lives in
``tests/integration/test_shutdown_acknowledgment_before_teardown.py``.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.contracts.public import ShutdownRequestedPayload
from issue_orchestrator.entrypoints import web, web_operator_routes
from tests.conftest import FakeBrowserAuth

RESPONSE_STARTED = "response.start"
RESPONSE_COMPLETE = "response.complete"
TEARDOWN = "teardown.server_exit"
PROCESS_EXIT = "teardown.process_exit"

REASON = "ordering test stop"
ACTOR = "unit-test"
EXIT_TIMEOUT_SECONDS = 5.0


@dataclass
class ShutdownTimeline:
    """Everything that happened, in the order it finished happening."""

    entries: list[str] = field(default_factory=list)
    process_exit_requested: threading.Event = field(default_factory=threading.Event)

    def record(self, entry: str) -> None:
        self.entries.append(entry)

    def index_of(self, entry: str) -> int:
        assert entry in self.entries, f"{entry!r} never happened: {self.entries}"
        return self.entries.index(entry)


class RecordingUvicornServer:
    """Stands in for the ``uvicorn.Server`` the dashboard runs on.

    ``web.trigger_server_shutdown`` is the production function under
    test; this only supplies the two attributes it writes, and notes
    when. Setting ``should_exit`` is the destructive moment — from there
    the real server stops serving, and with ``timeout_graceful_shutdown=0``
    it does so immediately.
    """

    def __init__(self, timeline: ShutdownTimeline) -> None:
        self.timeline = timeline
        self.force_exit = False
        self.exit_flags: list[bool] = []

    @property
    def should_exit(self) -> bool:
        return bool(self.exit_flags and self.exit_flags[-1])

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self.exit_flags.append(value)
        self.timeline.record(TEARDOWN)


@dataclass(frozen=True)
class DashboardUnderTest:
    """The dashboard with both destructive steps observable."""

    timeline: ShutdownTimeline
    server: RecordingUvicornServer

    def client(self) -> TestClient:
        return TestClient(SendRecordingApp(web.app, self.timeline))


class SendRecordingApp:
    """Wraps an ASGI app where the server's own ``send`` would sit.

    Nothing in the app can buffer a response past this point, so an
    entry recorded here means the layer below has taken the message.
    """

    def __init__(self, app: Any, timeline: ShutdownTimeline) -> None:
        self.app = app
        self.timeline = timeline

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def recording_send(message: Any) -> None:
            await send(message)
            if message["type"] == "http.response.start":
                self.timeline.record(RESPONSE_STARTED)
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                self.timeline.record(RESPONSE_COMPLETE)

        await self.app(scope, receive, recording_send)


class OrchestratorStub:
    """The engine's side of the request, recorded rather than run."""

    def __init__(self) -> None:
        self.state = SimpleNamespace(active_sessions=[])
        self.shutdown_requests: list[bool] = []

    def request_shutdown(self, force: bool = False) -> None:
        self.shutdown_requests.append(force)


@pytest.fixture
def dashboard(monkeypatch: pytest.MonkeyPatch) -> Iterator[DashboardUnderTest]:
    """A dashboard whose teardown is observable but not fatal.

    Only the final process-exit owner is intercepted. The web-server
    teardown that caused the live failure runs for real — replacing it
    with a recorder would remove the thing being measured.
    """
    timeline = ShutdownTimeline()

    def record_exit(code: int = 0) -> None:
        timeline.record(PROCESS_EXIT)
        timeline.process_exit_requested.set()

    monkeypatch.setattr(web_operator_routes.shutdown_manager, "exit", record_exit)
    server = RecordingUvicornServer(timeline)
    web.set_server(server)
    try:
        yield DashboardUnderTest(timeline=timeline, server=server)
    finally:
        web.set_server(None)


@pytest.fixture
def orchestrator() -> Iterator[OrchestratorStub]:
    stub = OrchestratorStub()
    web.set_orchestrator(stub)
    try:
        yield stub
    finally:
        web.set_orchestrator(None)


def _post_shutdown(
    client: TestClient, auth: FakeBrowserAuth, *, force: bool = False
) -> Any:
    """Post the shutdown the supervisor posts: reason and actor in the
    body, ``force`` in the query string, admin bearer in the header."""
    return client.post(
        "/api/shutdown",
        json={"reason": REASON, "actor": ACTOR},
        params={"force": "true"} if force else {},
        headers=auth.bearer_headers(),
    )


def test_the_server_is_torn_down_only_after_the_response_is_sent(
    dashboard: DashboardUnderTest,
    orchestrator: OrchestratorStub,
    fake_browser_auth: FakeBrowserAuth,
) -> None:
    """The property the live canary needed and did not have."""
    response = _post_shutdown(dashboard.client(), fake_browser_auth)

    assert response.status_code == 200
    assert response.json() == {
        "status": "shutdown_requested",
        "active_sessions": 0,
        "reason": REASON,
        "actor": ACTOR,
    }
    timeline = dashboard.timeline
    assert timeline.index_of(RESPONSE_STARTED) < timeline.index_of(RESPONSE_COMPLETE)
    assert timeline.index_of(RESPONSE_COMPLETE) < timeline.index_of(TEARDOWN), (
        "the dashboard server was torn down before its acknowledgment "
        f"reached the transport: {timeline.entries}"
    )


def test_both_destructive_steps_still_run(
    dashboard: DashboardUnderTest,
    orchestrator: OrchestratorStub,
    fake_browser_auth: FakeBrowserAuth,
) -> None:
    """Deferring is not cancelling.

    An ACK-only ``/api/shutdown`` would satisfy every ordering assertion
    above and leave the engine running, so both destructive steps are
    asserted to still happen: the server is told to exit with the same
    ``force_exit`` semantics as before, and the scheduled process exit
    is reached.
    """
    _post_shutdown(dashboard.client(), fake_browser_auth)

    assert dashboard.server.exit_flags == [True]
    assert dashboard.server.force_exit is True
    assert dashboard.timeline.process_exit_requested.wait(EXIT_TIMEOUT_SECONDS), (
        f"the scheduled process exit never ran: {dashboard.timeline.entries}"
    )
    assert dashboard.timeline.index_of(TEARDOWN) < dashboard.timeline.index_of(
        PROCESS_EXIT
    )


def test_the_request_evidence_is_unchanged(
    dashboard: DashboardUnderTest,
    orchestrator: OrchestratorStub,
    fake_browser_auth: FakeBrowserAuth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason/actor/force evidence stays attributable to the request.

    The broadcast is read through the dashboard's own subscriber API, so
    "exactly once, with this payload" comes off the real SSE fan-out
    rather than a stub in place of it.
    """
    shutdown_reasons: list[str] = []

    def record_request_shutdown(reason: str = "unknown") -> bool:
        shutdown_reasons.append(reason)
        return True

    monkeypatch.setattr(
        web_operator_routes.shutdown_manager,
        "request_shutdown",
        record_request_shutdown,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    web.add_event_subscriber(queue)
    try:
        response = _post_shutdown(dashboard.client(), fake_browser_auth, force=True)
    finally:
        web.remove_event_subscriber(queue)

    assert response.status_code == 200
    assert response.json()["status"] == "force_shutdown"
    assert orchestrator.shutdown_requests == [True]

    assert len(shutdown_reasons) == 1
    assert REASON in shutdown_reasons[0]
    assert ACTOR in shutdown_reasons[0]
    assert "force=true" in shutdown_reasons[0]

    event = queue.get_nowait()
    assert queue.empty(), "the shutdown broadcast fired more than once"
    assert event["type"] == "shutdown_requested"
    ShutdownRequestedPayload.model_validate(event["data"])
    assert event["data"]["force"] is True
    assert event["data"]["active_sessions"] == 0
    assert event["data"]["reason"] == REASON
    assert event["data"]["actor"] == ACTOR


def test_an_unauthenticated_request_tears_nothing_down(
    dashboard: DashboardUnderTest,
    orchestrator: OrchestratorStub,
    fake_browser_auth: FakeBrowserAuth,
) -> None:
    """The gate still runs first, and a refusal has no side effect."""
    response = dashboard.client().post(
        "/api/shutdown", json={"reason": REASON, "actor": ACTOR}
    )

    assert response.status_code == 401
    assert TEARDOWN not in dashboard.timeline.entries
    assert orchestrator.shutdown_requests == []


def test_a_reasonless_request_tears_nothing_down(
    dashboard: DashboardUnderTest,
    orchestrator: OrchestratorStub,
    fake_browser_auth: FakeBrowserAuth,
) -> None:
    """A 400 must not schedule teardown for later either.

    Deferred work is easy to arm before the branch that rejects the
    request; this states that it is not.
    """
    response = dashboard.client().post(
        "/api/shutdown",
        json={"reason": "  "},
        headers=fake_browser_auth.bearer_headers(),
    )

    assert response.status_code == 400
    assert TEARDOWN not in dashboard.timeline.entries
    assert orchestrator.shutdown_requests == []
