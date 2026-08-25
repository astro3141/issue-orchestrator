"""A real loopback ``/api/shutdown`` that answers only to the admin bearer.

The #267 B2 failure was invisible to every test that mocked the HTTP
call: the supervisor built a ``Request``, something answered, and the
stop reported ``engine stopped`` because a signal finished the job.
What was never asserted is the thing the running dashboard actually
does — refuse an unauthenticated POST with 401.

So tests that care about the graceful phase talk to a real server over
a real socket, gated the way the mounted route is gated: bearer first
(401), then the reason contract (400), then accept (200). It records
what it was sent, so a test can state both halves of the property —
which credential arrived, and whether the request was accepted.

``token=None`` models a ``--dev-no-auth`` engine: the gate is off and
an unauthenticated request is accepted, which is the direction that
must keep working when no admin token exists anywhere.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

SHUTDOWN_PATH = "/api/shutdown"
SERVER_STOP_TIMEOUT_SECONDS = 10.0
# How often the serving thread looks for a stop request. The stdlib
# default is 0.5s, which every teardown would pay in full.
SERVER_POLL_INTERVAL_SECONDS = 0.02


@dataclass(frozen=True)
class RecordedShutdownRequest:
    """One POST as the server received it, and the verdict it reached."""

    authorization: str | None
    content_type: str | None
    payload: dict[str, Any]
    status: int

    @property
    def authenticated(self) -> bool:
        return self.status != 401


@dataclass
class ShutdownEndpointState:
    """The gate's rules and everything it has been asked so far."""

    token: str | None
    on_accepted: Callable[[], None] = lambda: None
    requests: list[RecordedShutdownRequest] = field(default_factory=list)
    accepted: bool = False

    def judge(
        self,
        *,
        authorization: str | None,
        content_type: str | None,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, str]]:
        """Answer one request in the mounted route's own order."""
        status, body = self._verdict(authorization, payload)
        self.requests.append(
            RecordedShutdownRequest(
                authorization=authorization,
                content_type=content_type,
                payload=payload,
                status=status,
            )
        )
        if status == 200:
            self.accepted = True
            self.on_accepted()
        return status, body

    def _verdict(
        self, authorization: str | None, payload: dict[str, Any]
    ) -> tuple[int, dict[str, str]]:
        if self.token is not None and authorization != f"Bearer {self.token}":
            return 401, {"error": "missing credentials"}
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return 400, {"error": "reason is required"}
        return 200, {"status": "shutting down"}


class _ShutdownRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        state: ShutdownEndpointState = self.server.shutdown_state  # type: ignore[attr-defined]
        if self.path.split("?")[0] != SHUTDOWN_PATH:
            self._respond(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {}
        status, body = state.judge(
            authorization=self.headers.get("Authorization"),
            content_type=self.headers.get("Content-Type"),
            payload=payload if isinstance(payload, dict) else {},
        )
        self._respond(status, body)

    def _respond(self, status: int, body: dict[str, str]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Keep the stdlib server out of the test's stderr."""


class AuthRequiringShutdownEndpoint:
    """Serves :data:`SHUTDOWN_PATH` on an OS-picked loopback port."""

    def __init__(
        self,
        *,
        token: str | None,
        on_accepted: Callable[[], None] = lambda: None,
    ) -> None:
        self.state = ShutdownEndpointState(token=token, on_accepted=on_accepted)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        """Start serving and return the port the OS assigned."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ShutdownRequestHandler)
        server.shutdown_state = self.state  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": SERVER_POLL_INTERVAL_SECONDS},
            daemon=True,
        )
        self._thread.start()
        return int(server.server_address[1])

    @property
    def port(self) -> int:
        assert self._server is not None, "endpoint has not been started"
        return int(self._server.server_address[1])

    @property
    def requests(self) -> list[RecordedShutdownRequest]:
        return list(self.state.requests)

    @property
    def accepted(self) -> bool:
        """Whether an authenticated, reasoned shutdown was ever accepted."""
        return self.state.accepted

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
