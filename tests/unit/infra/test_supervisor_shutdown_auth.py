"""The supervisor's shutdown POST must carry the admin bearer (#273).

The dashboard ``/api/shutdown`` route is admin-gated. The supervisor
built its POST with a content type and nothing else, so a live R21
canary measured every ordinary stop being refused ``401 missing
credentials`` and degrading to signals — while the overall stop still
reported success, because a signal did eventually stop the engine.

These tests drive the public ``stop_by_port`` entry against a real
loopback gate that refuses an unauthenticated POST exactly as the
mounted route does, and assert what the *server received*. The
graceful-stop path with zero signal escalation is proven end to end in
``tests/integration/test_supervisor_graceful_shutdown_auth.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from issue_orchestrator.infra import supervisor
from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR, default_token_path
from tests.shutdown_endpoint_server import AuthRequiringShutdownEndpoint

ENGINE_TOKEN = "supervisor-shutdown-admin-token"
FILE_TOKEN = "supervisor-shutdown-token-from-file"
STOP_REASON = "operator asked the repository engine to stop"
STOP_ACTOR = "supervisor.stop_by_port"
# Short enough to keep an unconfirmed stop quick, long enough that the
# port is probed more than once before the budget expires.
GRACEFUL_BUDGET_SECONDS = 0.3


@pytest.fixture
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home, so no developer's real admin token leaks in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    return home


def _write_token_file(token: str) -> Path:
    path = default_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return path


class StopByPortHarness:
    """A live gate plus the port-kill fallback, wired to one truth.

    The port stays "in use" until something actually stops the engine —
    either the gate accepting an authenticated shutdown, or the
    fallback killing it. That is what keeps the two outcomes
    distinguishable: both can end in ``stop_by_port`` returning True,
    and only the recorded path says which one happened.
    """

    def __init__(self, endpoint: AuthRequiringShutdownEndpoint) -> None:
        self.endpoint = endpoint
        self.port_kills: list[bool] = []

    def kill_by_port(self, port: int, use_sigkill: bool = False) -> bool:
        self.port_kills.append(use_sigkill)
        return True

    def port_in_use(self, port: int) -> bool:
        return not (self.endpoint.accepted or self.port_kills)


@pytest.fixture
def harness(
    private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> StopByPortHarness:
    endpoint = AuthRequiringShutdownEndpoint(token=ENGINE_TOKEN)
    endpoint.start()
    live = StopByPortHarness(endpoint)
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._kill_by_port", live.kill_by_port
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._is_port_in_use", live.port_in_use
    )
    try:
        yield live
    finally:
        endpoint.stop()


def test_stop_by_port_presents_the_existing_admin_bearer(
    harness: StopByPortHarness,
) -> None:
    """The measured failure, in the direction it must now go.

    One request, carrying the credential the operator already has, and
    accepted by a gate that refuses anything else.
    """
    _write_token_file(ENGINE_TOKEN)

    stopped = supervisor.stop_by_port(
        harness.endpoint.port, reason=STOP_REASON, actor=STOP_ACTOR
    )

    assert stopped is True
    assert len(harness.endpoint.requests) == 1
    request = harness.endpoint.requests[0]
    assert request.authorization == f"Bearer {ENGINE_TOKEN}"
    assert request.content_type == "application/json"
    assert request.status == 200
    assert harness.port_kills == [], "an accepted graceful stop still killed the port"


def test_the_shutdown_body_still_carries_the_callers_reason_and_actor(
    harness: StopByPortHarness,
) -> None:
    """Authentication was added to the request; the contract is unchanged."""
    _write_token_file(ENGINE_TOKEN)

    supervisor.stop_by_port(
        harness.endpoint.port, reason=STOP_REASON, actor=STOP_ACTOR
    )

    assert harness.endpoint.requests[0].payload == {
        "reason": STOP_REASON,
        "actor": STOP_ACTOR,
    }


def test_the_env_token_wins_over_an_existing_token_file(
    harness: StopByPortHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence is ``read_existing_admin_token``'s, not a second rule."""
    _write_token_file(FILE_TOKEN)
    monkeypatch.setenv(TOKEN_ENV_VAR, ENGINE_TOKEN)

    supervisor.stop_by_port(
        harness.endpoint.port, reason=STOP_REASON, actor=STOP_ACTOR
    )

    assert harness.endpoint.requests[0].authorization == f"Bearer {ENGINE_TOKEN}"


def test_an_existing_token_file_is_used_when_the_env_is_unset(
    harness: StopByPortHarness,
) -> None:
    _write_token_file(FILE_TOKEN)

    supervisor.stop_by_port(
        harness.endpoint.port,
        reason=STOP_REASON,
        actor=STOP_ACTOR,
        graceful_timeout_seconds=GRACEFUL_BUDGET_SECONDS,
    )

    assert harness.endpoint.requests[0].authorization == f"Bearer {FILE_TOKEN}"


def test_no_credential_anywhere_sends_no_bearer_and_mints_nothing(
    private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dev-no-auth`` keeps working, and stays a client that asks.

    Requesting a stop must never be the thing that creates
    ``~/.issue-orchestrator/api-token``: a fabricated credential would
    authenticate nothing and would leave a secret behind on a machine
    that never started an engine.
    """
    endpoint = AuthRequiringShutdownEndpoint(token=None)
    endpoint.start()
    live = StopByPortHarness(endpoint)
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._kill_by_port", live.kill_by_port
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._is_port_in_use", live.port_in_use
    )
    try:
        stopped = supervisor.stop_by_port(
            endpoint.port, reason=STOP_REASON, actor=STOP_ACTOR
        )
    finally:
        endpoint.stop()

    assert stopped is True
    assert endpoint.requests[0].authorization is None
    assert endpoint.requests[0].status == 200
    assert live.port_kills == []
    assert not default_token_path().exists(), (
        "requesting a shutdown created an admin token file"
    )


def test_a_refused_credential_is_not_reclassified_as_a_graceful_stop(
    harness: StopByPortHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape of the live failure, now fail-closed (#326).

    A wrong bearer is refused, so the request is unconfirmed. That is
    not authority to signal: the engine is left running and the stop
    reports failure, instead of a SIGTERM by port dressed up as a
    successful non-force stop.
    """
    monkeypatch.setenv(TOKEN_ENV_VAR, "a-superseded-admin-token")

    stopped = supervisor.stop_by_port(
        harness.endpoint.port,
        reason=STOP_REASON,
        actor=STOP_ACTOR,
        graceful_timeout_seconds=GRACEFUL_BUDGET_SECONDS,
    )

    assert stopped is False
    assert harness.endpoint.requests[0].status == 401
    assert harness.endpoint.accepted is False
    assert harness.port_kills == [], (
        "an unconfirmed non-force shutdown signalled the port anyway"
    )


def test_a_forced_stop_still_skips_the_graceful_request(
    harness: StopByPortHarness,
) -> None:
    """Escalation policy is untouched: force never asks first."""
    _write_token_file(ENGINE_TOKEN)

    stopped = supervisor.stop_by_port(
        harness.endpoint.port, reason=STOP_REASON, actor=STOP_ACTOR, force=True
    )

    assert stopped is True
    assert harness.endpoint.requests == []
    assert harness.port_kills == [True]


def test_an_empty_reason_still_fails_before_anything_is_sent(
    harness: StopByPortHarness, tmp_path: Path
) -> None:
    """Fail-fast on an unreasoned stop, ahead of the credential lookup."""
    _write_token_file(ENGINE_TOKEN)

    with pytest.raises(ValueError, match="non-empty reason"):
        supervisor.stop_by_port(harness.endpoint.port, reason="   ")

    with pytest.raises(ValueError, match="non-empty reason"):
        supervisor.stop(tmp_path, reason="")

    assert harness.endpoint.requests == []
    assert harness.port_kills == []


def test_the_bearer_never_reaches_the_logs(
    harness: StopByPortHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the accepted request nor the refused one may print it."""
    _write_token_file(ENGINE_TOKEN)
    caplog.set_level(logging.DEBUG)

    supervisor.stop_by_port(
        harness.endpoint.port, reason=STOP_REASON, actor=STOP_ACTOR
    )
    monkeypatch.setenv(TOKEN_ENV_VAR, FILE_TOKEN)
    supervisor.stop_by_port(
        harness.endpoint.port,
        reason=STOP_REASON,
        actor=STOP_ACTOR,
        graceful_timeout_seconds=GRACEFUL_BUDGET_SECONDS,
    )

    assert harness.endpoint.requests[0].authorization == f"Bearer {ENGINE_TOKEN}"
    assert ENGINE_TOKEN not in caplog.text
    assert FILE_TOKEN not in caplog.text
