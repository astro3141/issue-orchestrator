"""End-to-end: can an agent actually deliver a callback? (#6913, #6924)

Every prior test of this path asserted a piece — the allowlist, the
token, the port rule — and the pieces all passed while the whole was
broken. This test starts from the production shape:

    control_api_port = 0        (auto-assign; the real deployment)
    nothing bound yet
    no agent-callback token in the environment

then boots a real server on an auto-assigned port, builds the agent
environment through the same public seam session launch uses, and makes
a real HTTP request with exactly the port and token that environment
carries. If any link regresses — the surface rejects agent tokens, the
engine never publishes one, or the port is the 0 sentinel — this fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import urllib.error
import urllib.request

from issue_orchestrator.control.session_env import build_session_env_exports
from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.entrypoints.engine_startup import EngineStartup
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.api_token import AGENT_CALLBACK_TOKEN_ENV_VAR
from issue_orchestrator.infra.config import Config
from tests.integration.live_dashboard_server import LiveDashboardServer

_PORT_EXPORT = re.compile(r"ISSUE_ORCHESTRATOR_API_PORT='(\d+)'")


@dataclass
class _Cfg:
    """The config surface engine startup reads."""

    browser_session_ttl_seconds: int = 900
    sse_token_ttl_seconds: int = 10
    browser_session_max: int = 7

# Routes an agent must be able to reach with its scoped token. Each is
# POSTed with an empty body: we assert on the auth verdict only, so any
# non-401/403 status (400 bad payload, 503 no orchestrator) is a pass.
AGENT_CALLBACK_PATHS = (
    "/api/review-exchange/respond",
    "/api/preflight-push",
    "/api/issues/6410/resume",
)


@pytest.fixture
def live_server():
    server = LiveDashboardServer(name="agent-callback")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def clean_auth_state(monkeypatch: pytest.MonkeyPatch):
    """Start from a process that has never seen a callback token."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    # Absent, not pre-seeded — pre-seeding is what let the earlier test
    # pass while the engine published nothing (#6924 F2).
    monkeypatch.delenv(AGENT_CALLBACK_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_API_TOKEN", "integration-admin-token-long-enough"
    )
    try:
        yield
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


def _post(url: str, token: str) -> int:
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.integration
def test_agent_callback_is_deliverable_from_the_generated_environment(
    live_server: LiveDashboardServer,
    clean_auth_state: None,
    tmp_path: Path,
) -> None:
    # 1. Engine startup — the same object run_orchestrator drives.
    endpoint = RuntimeAgentCallbackEndpoint()
    startup = EngineStartup(callback_endpoint=endpoint)
    startup.configure_auth(dev_no_auth=False, config=_Cfg())

    import os

    token = os.environ.get(AGENT_CALLBACK_TOKEN_ENV_VAR)
    assert token, (
        "engine startup must publish the agent-callback token to the "
        "environment agents inherit"
    )

    # 2. The server binds an auto-assigned port; startup's own hook
    #    publishes it, exactly as uvicorn's on_server_started does.
    bound_port = live_server.start()
    startup.server_started_hook(
        repo_root=tmp_path, requested_port=0, instance_id=None
    )(bound_port)

    # 3. Session launch builds the agent environment — config still 0.
    exports = build_session_env_exports(
        config=Config(control_api_port=0),
        completion_path=".issue-orchestrator/completion.json",
        session_id="coding-1",
        agent_label="agent:backend",
        issue_number=6410,
        run_dir=tmp_path / "run",
        worktree_path=tmp_path,
        callback_endpoint=endpoint,
    )
    match = _PORT_EXPORT.search(exports)
    assert match, f"no callback port in the agent environment: {exports}"
    agent_port = int(match.group(1))
    assert agent_port == bound_port, (
        f"agent was handed port {agent_port}, server is on {bound_port}"
    )

    # 4. The agent's own port + token must reach every callback route.
    for path in AGENT_CALLBACK_PATHS:
        status = _post(f"http://127.0.0.1:{agent_port}{path}", token)
        assert status not in (401, 403), (
            f"agent callback rejected on {path}: HTTP {status} — "
            "a verdict sent here would be undeliverable"
        )


@pytest.mark.integration
def test_agent_token_still_cannot_reach_admin_routes(
    live_server: LiveDashboardServer,
    clean_auth_state: None,
) -> None:
    """The fix must not turn the scoped token into an admin credential."""

    startup = EngineStartup(callback_endpoint=RuntimeAgentCallbackEndpoint())
    startup.configure_auth(dev_no_auth=False, config=_Cfg())

    import os

    token = os.environ[AGENT_CALLBACK_TOKEN_ENV_VAR]
    port = live_server.start()

    for path in ("/api/shutdown", "/api/resume", "/api/issues/not-an-int/resume"):
        status = _post(f"http://127.0.0.1:{port}{path}", token)
        assert status in (401, 403), (
            f"{path} accepted the agent-callback token (HTTP {status})"
        )
