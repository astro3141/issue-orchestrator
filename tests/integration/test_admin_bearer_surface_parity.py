"""One admin bearer across two live surfaces, over real sockets (#269).

#268's measurement was made against a *running* engine, not a test
harness: the same unrotated ``~/.issue-orchestrator/api-token`` returned
200 from the Control API and 401 from the dashboard surface in the same
process. Unit parity tests drive both apps in-process; what they cannot
show is a server that has already booted, already installed its
middleware, and is then handed a different admin bearer — which is the
runtime shape the divergence appeared in.

So both surfaces run here as real uvicorn servers on real ports:

- the Web Dashboard app (which mounts ``control_app`` at ``""``, exactly
  as the repository engine serves it), and
- the standalone Control API app, as ``ControlAPIServer`` serves it.

Each request crosses a socket and the full middleware chain. No process
shutdown is invoked: the shutdown *route's* ownership is asserted in the
unit suite, and #267's stop path is out of scope for this leaf.
"""

from __future__ import annotations

import httpx
import pytest

from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    control_app,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.infra import browser_session
from tests.integration.live_dashboard_server import LiveDashboardServer

ADMIN_TOKEN = "surface-parity-admin-token"
ROTATED_TOKEN = "surface-parity-rotated-admin-token"
AGENT_TOKEN = "surface-parity-agent-callback-token"

# Neither public nor agent-callback: the plainest admin-gated read, and
# the exact route #268 measured the split on.
ADMIN_ROUTE = "/api/status"
AGENT_CALLBACK_ROUTE = "/api/preflight-push"

# Bounded waits only, against a real local socket.
REQUEST_TIMEOUT_SECONDS = 15.0


@pytest.fixture
def surfaces():
    """Both HTTP surfaces live on their own ports, sharing one owner."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()

    browser_session.shutdown()
    browser_session.initialize(admin_token=ADMIN_TOKEN)
    configure_api_token(ADMIN_TOKEN, agent_callback=AGENT_TOKEN)

    dashboard = LiveDashboardServer(name="web_dashboard")
    control = LiveDashboardServer(control_app, name="control_api")
    clients: dict[str, httpx.Client] = {}
    try:
        dashboard.start()
        control.start()
        clients = {
            "web_dashboard": httpx.Client(
                base_url=dashboard.base_url(), timeout=REQUEST_TIMEOUT_SECONDS
            ),
            "control_api": httpx.Client(
                base_url=control.base_url(), timeout=REQUEST_TIMEOUT_SECONDS
            ),
        }
        yield clients
    finally:
        for client in clients.values():
            client.close()
        control.stop()
        dashboard.stop()
        browser_session.shutdown()
        configure_api_token(prev_admin, agent_callback=prev_agent)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _accepted(clients: dict[str, httpx.Client], token: str | None) -> dict[str, bool]:
    headers = _bearer(token) if token is not None else {}
    return {
        name: client.get(ADMIN_ROUTE, headers=headers).status_code != 401
        for name, client in clients.items()
    }


def _both(value: bool) -> dict[str, bool]:
    return {"web_dashboard": value, "control_api": value}


@pytest.mark.integration
def test_live_surfaces_agree_on_every_credential(surfaces) -> None:
    """The #268 measurement, run against two live servers.

    Its output was an asymmetry — 200 here, 401 there, one token. This
    asserts the symmetry directly, for the correct bearer, a wrong one,
    and none at all.
    """
    assert _accepted(surfaces, ADMIN_TOKEN) == _both(True)
    assert _accepted(surfaces, "definitely-not-the-admin-token") == _both(False)
    assert _accepted(surfaces, None) == _both(False)


@pytest.mark.integration
def test_login_on_a_live_surface_accepts_the_current_owner(surfaces) -> None:
    """A browser must be able to sign in to either surface with one secret.

    The dashboard login used to verify against its own copy of the admin
    token, so an operator could hold the credential the Control API
    accepts and still be refused at the dashboard form.
    """
    for name, client in surfaces.items():
        accepted = client.post("/login", json={"token": ADMIN_TOKEN})
        refused = client.post("/login", json={"token": "wrong-token"})
        assert accepted.status_code == 200, f"{name}: {accepted.text}"
        assert refused.status_code == 401, f"{name}: {refused.text}"


@pytest.mark.integration
def test_rotating_the_owner_reaches_already_running_servers(surfaces) -> None:
    """The failure direction, in the runtime shape it actually appeared in.

    Both servers are already serving with their middleware installed
    when the owner is rewritten. A per-surface mirror would keep
    honouring the superseded bearer — that is the live divergence #268
    caught, and the reason the supported graceful stop could not
    authenticate (#267).
    """
    assert _accepted(surfaces, ADMIN_TOKEN) == _both(True)

    configure_api_token(ROTATED_TOKEN, agent_callback=AGENT_TOKEN)

    assert _accepted(surfaces, ADMIN_TOKEN) == _both(False), (
        "a running surface still accepts the superseded admin bearer"
    )
    assert _accepted(surfaces, ROTATED_TOKEN) == _both(True), (
        "a running surface never picked up the current admin bearer"
    )


@pytest.mark.integration
def test_agent_callback_authority_is_unchanged_on_live_surfaces(surfaces) -> None:
    """The scoped token keeps its allowlist and gains nothing else."""
    for name, client in surfaces.items():
        on_allowlist = client.post(
            AGENT_CALLBACK_ROUTE, headers=_bearer(AGENT_TOKEN), json={}
        )
        assert on_allowlist.status_code not in (401, 403), (
            f"{name}: agent callback rejected on its own allowlist "
            f"(HTTP {on_allowlist.status_code})"
        )

    assert _accepted(surfaces, AGENT_TOKEN) == _both(False), (
        "the agent-callback token authenticated an ordinary admin route"
    )


@pytest.mark.integration
def test_clearing_the_owner_opens_both_live_surfaces(surfaces) -> None:
    """``--dev-no-auth`` on a running process must not half-open the door."""
    assert _accepted(surfaces, None) == _both(False)

    configure_api_token(None, agent_callback=None)

    assert _accepted(surfaces, None) == _both(True)
    assert _accepted(surfaces, ADMIN_TOKEN) == _both(True), (
        "a surface is still enforcing the cleared admin bearer"
    )
