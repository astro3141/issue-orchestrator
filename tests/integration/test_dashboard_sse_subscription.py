"""The dashboard's SSE subscription, over a real socket (issue #44).

The incident: ``GET /api/events`` was rejected with a persistent 401
``invalid sse token`` while every other dashboard route stayed usable, so the
board rendered a read model that no live event had touched for hours. Nothing
below can be proved with ``TestClient`` — its transport runs the ASGI app to
completion before returning a response, so an endless SSE generator never
returns one. These run against a real uvicorn server and a real HTTP client:

1. a freshly authenticated session subscribes and the server speaks first
   (the engine-liveness beacon), so a consumer has a frame whose absence it can
   detect;
2. an event broadcast by the engine reaches that live subscriber without any
   reload or poll;
3. replaying the now-spent token reproduces the incident's exact 401, which is
   why the browser must never be allowed to retry the SSE URL on its own.
"""

from __future__ import annotations

import json

import httpx
import pytest

from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.infra import browser_session
from tests.integration.live_dashboard_server import LiveDashboardServer

ADMIN_TOKEN = "sse-subscription-admin-token"
# Bounded waits only: these coordinate with a real server over a real socket,
# which tests/AGENTS.md allows. Nothing sleeps to "let work happen".
READ_TIMEOUT_SECONDS = 10.0


@pytest.fixture
def live_dashboard():
    """Dashboard on a real port with the real browser-auth gate enabled."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()

    browser_session.shutdown()
    browser_session.initialize(admin_token=ADMIN_TOKEN)
    configure_api_token(ADMIN_TOKEN, agent_callback=None)

    server = LiveDashboardServer()
    try:
        server.start()
        yield server
    finally:
        server.stop()
        browser_session.shutdown()
        configure_api_token(prev_admin, agent_callback=prev_agent)


@pytest.fixture
def signed_in(live_dashboard):
    """A browser-shaped client that has logged in through the real form."""
    with httpx.Client(
        base_url=live_dashboard.base_url(), timeout=15.0
    ) as client:
        response = client.post("/login", json={"token": ADMIN_TOKEN})
        assert response.status_code == 200, response.text
        assert client.cookies.get(browser_session.SESSION_COOKIE)
        yield client


def _mint_sse_token(client: httpx.Client) -> str:
    response = client.get("/api/sse-token")
    assert response.status_code == 200, response.text
    return response.json()["sse_token"]


def _read_frame(lines) -> tuple[str, dict]:
    """Read one ``event:``/``data:`` pair off an SSE line iterator."""
    event_name = None
    for line in lines:
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            assert event_name is not None, "data line arrived before its event line"
            return event_name, json.loads(line.split(":", 1)[1].strip())
    raise AssertionError("stream ended before a complete frame arrived")


@pytest.mark.timeout(60)
def test_freshly_authenticated_dashboard_gets_a_live_stream(signed_in) -> None:
    """Requirement 3/4: the subscription is established, and it speaks first.

    The beacon arriving with no event broadcast is the whole fix: it is the
    positive liveness proof a consumer can time out on, replacing the
    ``EventSource.onerror`` signal that a half-open socket never delivers.
    """
    token = _mint_sse_token(signed_in)

    with signed_in.stream(
        "GET",
        "/api/events",
        params={"sse_token": token},
        timeout=READ_TIMEOUT_SECONDS,
    ) as stream:
        assert stream.status_code == 200
        event_name, payload = _read_frame(stream.iter_lines())

    assert event_name == "engine.liveness"
    assert payload["state"] in {"advancing", "stalled", "unknown"}
    assert payload["interval_seconds"] > 0
    assert "schema" in payload


@pytest.mark.timeout(60)
def test_engine_event_reaches_the_live_subscriber_without_a_reload(
    live_dashboard, signed_in
) -> None:
    """Requirement 4: an event emitted by the engine is observed live."""
    token = _mint_sse_token(signed_in)

    with signed_in.stream(
        "GET",
        "/api/events",
        params={"sse_token": token},
        timeout=READ_TIMEOUT_SECONDS,
    ) as stream:
        assert stream.status_code == 200
        lines = stream.iter_lines()
        first_event, _first_payload = _read_frame(lines)
        assert first_event == "engine.liveness", (
            "the subscriber must be registered before the broadcast, and the "
            "beacon is what proves it is"
        )

        live_dashboard.broadcast("session.started", {"issue_number": 44})
        event_name, payload = _read_frame(lines)

    assert event_name == "session.started"
    assert payload["issue_number"] == 44


@pytest.mark.timeout(60)
def test_replaying_a_spent_sse_token_reproduces_the_incident_401(signed_in) -> None:
    """Requirement 1/2: why *only* the SSE route broke.

    Every other dashboard route authenticates from the session cookie alone
    and is therefore replay-safe. ``/api/events`` additionally consumes a
    single-use token, so a transport-level retry of the same URL — which is
    exactly what ``EventSource`` does on its own — is guaranteed to 401 while
    the rest of the surface keeps working.
    """
    token = _mint_sse_token(signed_in)

    with signed_in.stream(
        "GET",
        "/api/events",
        params={"sse_token": token},
        timeout=READ_TIMEOUT_SECONDS,
    ) as stream:
        assert stream.status_code == 200
        _read_frame(stream.iter_lines())

    replay = signed_in.get("/api/events", params={"sse_token": token})

    assert replay.status_code == 401
    assert replay.json() == {"error": "invalid sse token"}
    # The cookie is untouched by the refusal: the rest of the surface stays
    # usable, which is why the outage looked like a healthy dashboard rather
    # than a logged-out one.
    assert signed_in.get("/api/sse-token").status_code == 200


@pytest.mark.timeout(60)
def test_reconnecting_with_a_freshly_minted_token_succeeds(signed_in) -> None:
    """Requirement 7: a reconnect is not left permanently unauthorized.

    The client's recovery path — mint a new token, open a new stream — must
    work immediately after the previous token was spent, or the dashboard
    would stay dark after every restart.
    """
    first_token = _mint_sse_token(signed_in)
    with signed_in.stream(
        "GET",
        "/api/events",
        params={"sse_token": first_token},
        timeout=READ_TIMEOUT_SECONDS,
    ) as stream:
        _read_frame(stream.iter_lines())

    second_token = _mint_sse_token(signed_in)
    assert second_token != first_token

    with signed_in.stream(
        "GET",
        "/api/events",
        params={"sse_token": second_token},
        timeout=READ_TIMEOUT_SECONDS,
    ) as stream:
        assert stream.status_code == 200
        event_name, _payload = _read_frame(stream.iter_lines())

    assert event_name == "engine.liveness"
