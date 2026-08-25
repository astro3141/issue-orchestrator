"""One admin bearer, one verdict — on every surface this process serves.

#268 measured the defect on a live engine: one unrotated
``~/.issue-orchestrator/api-token`` was accepted by the Control API
(``GET /api/status`` → 200) and rejected by the dashboard surface in the
same process (401), because each kept its own mutable copy of the admin
token. #267 showed what that cost: the supported repository-engine stop
asks the dashboard ``/api/shutdown`` to shut down gracefully, could not
authenticate, and fell back to signals — leaving a half-dead engine.

So the property under test is *parity*, not "the dashboard has a token":
whatever verdict one surface reaches on a given credential, the other
must reach too. Each test therefore configures the bearer exactly once
— through the single owner — and asserts both surfaces at once.

Both apps here are the real ones, and the dashboard ``app`` mounts
``control_app`` at ``""``, so requests flow through the actual
middleware chain. The socket-level version of the same parity, plus a
reconfiguration applied to running servers, lives in
``tests/integration/test_admin_bearer_surface_parity.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints import web
from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    control_app,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.entrypoints.web import app
from issue_orchestrator.infra import browser_session

ADMIN_TOKEN = "single-owner-admin-token"
OTHER_TOKEN = "not-the-admin-token"
AGENT_TOKEN = "single-owner-agent-callback-token"

# A route that is neither public nor on the agent-callback allowlist,
# and that both apps serve — the plainest possible admin-gated read.
ADMIN_ROUTE = "/api/status"
AGENT_CALLBACK_ROUTE = "/api/preflight-push"

#: ``(surface name, client)`` pairs. Tests assert over both so a
#: divergence names the surface that disagreed.
SURFACES = ("web_dashboard", "control_api")


@pytest.fixture
def surfaces():
    """A client per HTTP surface, sharing the one bearer-token owner."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    try:
        yield {
            "web_dashboard": TestClient(app),
            "control_api": TestClient(control_app),
        }
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _verdicts(surfaces: dict[str, TestClient], token: str | None) -> dict[str, bool]:
    """Map surface name → "was this credential accepted?"."""
    headers = _bearer(token) if token is not None else {}
    return {
        name: client.get(ADMIN_ROUTE, headers=headers).status_code != 401
        for name, client in surfaces.items()
    }


# ---------------------------------------------------------------------------
# The core contract: one configuration, one verdict everywhere.
# ---------------------------------------------------------------------------


def test_one_configuration_gives_both_surfaces_the_same_verdict(surfaces) -> None:
    """The exact shape #268 measured, asserted as an equality.

    Configure once — as ``EngineStartup.configure_auth`` does — then ask
    both surfaces about the same three credentials. Any disagreement is
    the defect, whichever direction it points in.
    """
    configure_api_token(ADMIN_TOKEN, agent_callback=None)

    correct = _verdicts(surfaces, ADMIN_TOKEN)
    wrong = _verdicts(surfaces, OTHER_TOKEN)
    absent = _verdicts(surfaces, None)

    assert correct == {name: True for name in SURFACES}, correct
    assert wrong == {name: False for name in SURFACES}, wrong
    assert absent == {name: False for name in SURFACES}, absent


def test_login_exchanges_the_same_bearer_on_both_surfaces(surfaces) -> None:
    """Fixing the gate alone would leave login bound to a second value.

    ``POST /login`` is the browser's way in, and the dashboard used to
    verify it against its own mirror. Both surfaces must accept exactly
    the current owner's token and refuse anything else.
    """
    browser_session.shutdown()
    browser_session.initialize(admin_token=ADMIN_TOKEN)
    configure_api_token(ADMIN_TOKEN, agent_callback=None)

    accepted = {
        name: client.post("/login", json={"token": ADMIN_TOKEN}).status_code
        for name, client in surfaces.items()
    }
    refused = {
        name: client.post("/login", json={"token": OTHER_TOKEN}).status_code
        for name, client in surfaces.items()
    }

    assert accepted == {name: 200 for name in SURFACES}, accepted
    assert refused == {name: 401 for name in SURFACES}, refused


def test_rotating_the_owner_cannot_leave_a_stale_value_enforced(surfaces) -> None:
    """Failure direction: a mirror would keep honouring the old secret.

    Nothing here reconfigures a dashboard-side token, because there is
    none to reconfigure. If one were reintroduced, this is what would
    catch it: the dashboard would still accept the superseded bearer, or
    still reject the current one.
    """
    configure_api_token(ADMIN_TOKEN, agent_callback=None)
    assert _verdicts(surfaces, ADMIN_TOKEN) == {name: True for name in SURFACES}

    configure_api_token(OTHER_TOKEN, agent_callback=None)

    superseded = _verdicts(surfaces, ADMIN_TOKEN)
    current = _verdicts(surfaces, OTHER_TOKEN)
    assert superseded == {name: False for name in SURFACES}, superseded
    assert current == {name: True for name in SURFACES}, current


def test_clearing_the_owner_disarms_both_surfaces_together(surfaces) -> None:
    """``--dev-no-auth`` must be an authoritative OFF, not a half-open door.

    The dangerous asymmetry is a surface left enforcing stale state while
    the operator believes auth is off — or worse, one open while the
    other still guards. Clearing is a single write, so both move.
    """
    configure_api_token(ADMIN_TOKEN, agent_callback=AGENT_TOKEN)
    assert _verdicts(surfaces, None) == {name: False for name in SURFACES}

    configure_api_token(None, agent_callback=None)

    assert _verdicts(surfaces, None) == {name: True for name in SURFACES}
    stale = _verdicts(surfaces, ADMIN_TOKEN)
    assert stale == {name: True for name in SURFACES}, (
        "a surface still judging requests against the cleared token"
    )


# ---------------------------------------------------------------------------
# The agent-callback bearer keeps its own, narrower authority.
# ---------------------------------------------------------------------------


def test_agent_callback_bearer_stays_scoped_on_both_surfaces(surfaces) -> None:
    """Unifying the admin bearer must not widen the scoped one.

    The callback token reaches its allowlist and nothing else, and both
    surfaces answer that identically — the rule ``_auth_middleware``
    already single-owned (#6913).
    """
    configure_api_token(ADMIN_TOKEN, agent_callback=AGENT_TOKEN)

    on_allowlist = {
        name: client.post(
            AGENT_CALLBACK_ROUTE, headers=_bearer(AGENT_TOKEN), json={}
        ).status_code
        not in (401, 403)
        for name, client in surfaces.items()
    }
    off_allowlist = _verdicts(surfaces, AGENT_TOKEN)

    assert on_allowlist == {name: True for name in SURFACES}, on_allowlist
    assert off_allowlist == {name: False for name in SURFACES}, off_allowlist


def test_admin_bearer_still_reaches_the_agent_callback_routes(surfaces) -> None:
    """The scoped allowlist narrows the agent token, not the admin one."""
    configure_api_token(ADMIN_TOKEN, agent_callback=AGENT_TOKEN)

    verdicts = {
        name: client.post(
            AGENT_CALLBACK_ROUTE, headers=_bearer(ADMIN_TOKEN), json={}
        ).status_code
        not in (401, 403)
        for name, client in surfaces.items()
    }

    assert verdicts == {name: True for name in SURFACES}, verdicts


# ---------------------------------------------------------------------------
# What this leaf deliberately does NOT change.
# ---------------------------------------------------------------------------


def test_shutdown_stays_an_admin_gated_dashboard_route(surfaces) -> None:
    """Authentication ownership moved; shutdown authority did not.

    ``/api/shutdown`` remains the dashboard/operator surface's route,
    still admin-gated, still refusing an unreasoned stop. The mock
    orchestrator lets the reason contract answer without any real
    shutdown being requested.
    """
    configure_api_token(ADMIN_TOKEN, agent_callback=AGENT_TOKEN)
    dashboard = surfaces["web_dashboard"]
    orchestrator = MagicMock()
    web.set_orchestrator(orchestrator)
    try:
        assert dashboard.post("/api/shutdown").status_code == 401
        assert (
            dashboard.post(
                "/api/shutdown", headers=_bearer(AGENT_TOKEN)
            ).status_code
            in (401, 403)
        )

        reasoned = dashboard.post("/api/shutdown", headers=_bearer(ADMIN_TOKEN))

        assert reasoned.status_code == 400, reasoned.text
        assert reasoned.json()["error"] == "reason is required"
        orchestrator.request_shutdown.assert_not_called()
    finally:
        web.set_orchestrator(None)


def test_dashboard_module_owns_no_admin_token_of_its_own() -> None:
    """Structural guard: the second owner must not come back.

    Parity tests prove the surfaces agree *now*; nothing in them fails
    the moment someone reintroduces a dashboard-local admin token and
    dutifully writes it at startup. That is precisely how #268 shipped,
    so the absence of a per-surface copy is pinned directly.
    """
    for attribute in (
        "_dashboard_admin_token",
        "configure_dashboard_admin_token",
        "get_configured_dashboard_admin_token",
    ):
        assert not hasattr(web, attribute), (
            f"web.{attribute} reintroduces a second admin-bearer owner; "
            "both surfaces must read _auth_tokens.PROCESS_BEARER_TOKENS"
        )
