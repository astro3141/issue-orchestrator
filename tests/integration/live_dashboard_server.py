"""A real uvicorn server hosting an app on an OS-picked port.

Several integration tests need what ``TestClient`` cannot give them: a
real socket, a real middleware chain, and a server that keeps running
while the test changes process state underneath it. They each grew their
own copy of this class; one copy means one place to fix when a
start/stop detail is wrong.

Defaults to the dashboard ``app`` (which mounts ``control_app`` at
``""``, so it serves both surfaces) — pass ``control_app`` explicitly to
stand up the standalone Control API surface alongside it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import FastAPI

from issue_orchestrator.entrypoints.web import app as dashboard_app
from issue_orchestrator.entrypoints.web import broadcast_event

STARTUP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 15.0
BROADCAST_TIMEOUT_SECONDS = 10.0


class LiveDashboardServer:
    """Runs ``app`` in a background thread and reports its bound port."""

    def __init__(
        self,
        app: FastAPI | None = None,
        *,
        name: str = "dashboard",
        timeout_graceful_shutdown: int | None = None,
    ) -> None:
        self._app = dashboard_app if app is None else app
        self._name = name
        # ``run_web_dashboard`` deploys with ``timeout_graceful_shutdown=0``.
        # Tests about shutdown ordering must reproduce that shape; tests
        # about anything else keep uvicorn's default (None).
        self._timeout_graceful_shutdown = timeout_graceful_shutdown
        self.port: int | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> int:
        """Start serving and return the port the OS assigned."""
        import uvicorn

        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            # port=0 mirrors the deployment: the OS picks the port and
            # only the running server knows which one.
            config = uvicorn.Config(
                self._app,
                host="127.0.0.1",
                port=0,
                log_level="error",
                access_log=False,
                timeout_graceful_shutdown=self._timeout_graceful_shutdown,
            )
            self._server = uvicorn.Server(config)

            async def _serve() -> None:
                task = self._loop.create_task(self._server.serve())
                while not self._server.started:
                    await asyncio.sleep(0.01)
                self.port = self._server.servers[0].sockets[0].getsockname()[1]
                ready.set()
                await task

            self._loop.run_until_complete(_serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not ready.wait(timeout=STARTUP_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"{self._name} server did not start within "
                f"{STARTUP_TIMEOUT_SECONDS:.0f}s"
            )
        assert self.port is not None
        return self.port

    def uvicorn_server(self) -> Any:
        """The running ``uvicorn.Server``.

        Handed to ``web.set_server`` by tests that need the production
        ``trigger_server_shutdown`` to act on a real server rather than
        on the ``None`` a test process usually leaves it as.
        """
        assert self._server is not None, "server has not been started"
        return self._server

    def wait_until_stopped(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        """Wait for the serving thread to exit; ``True`` if it did."""
        assert self._thread is not None, "server has not been started"
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def base_url(self) -> str:
        """The origin a client should talk to; requires ``start`` first."""
        assert self.port is not None, "server has not been started"
        return f"http://127.0.0.1:{self.port}"

    def broadcast(self, event_type: str, data: dict) -> None:
        """Emit an event the way the engine does — from the server's loop."""
        assert self._loop is not None, "server has not been started"
        future = asyncio.run_coroutine_threadsafe(
            broadcast_event(event_type, data), self._loop
        )
        future.result(timeout=BROADCAST_TIMEOUT_SECONDS)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._server.force_exit = True
        if self._thread is not None:
            self._thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
