"""Work that must not run until a response has actually been sent.

``POST /api/shutdown`` is the case this exists for. The handler tears
down the very uvicorn server that still owes its caller a body, and
:func:`.web.trigger_server_shutdown` is deliberately destructive —
``should_exit`` *and* ``force_exit``, against a server configured with
``timeout_graceful_shutdown=0``. Running that before the ``JSONResponse``
was constructed meant the acknowledgment never reached the wire: #267's
V3 B2 canary watched an authenticated stop sit for the supervisor's full
2.0 s HTTP budget and then fall back to SIGTERM, with zero auth
rejections recorded (#277).

Starlette's ``Response(background=...)`` is the obvious seam and it is
not sufficient here. The dashboard's auth gate is a
``BaseHTTPMiddleware``, which streams the inner response through a
memory object stream and rebuilds it as a ``_StreamingResponse`` whose
``background`` is unconditionally ``None``. The inner handler's
background therefore runs off the *inner* send — which returns as soon
as the outer layer has received the message, not once the server has
written it. Ordering would be a scheduling race.

So the boundary is owned here instead. :class:`AfterResponseMiddleware`
is installed as the outermost user middleware, so the ``send`` it wraps
is the server's own: when the terminal ``http.response.body`` message
comes back, uvicorn has written the response and marked the cycle
complete. Only then does deferred work run.

This is a boundary, not a lifecycle framework. It defers callables until
one response is out; anything larger belongs to an owner that models it.
"""

from __future__ import annotations

from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

AFTER_RESPONSE_SCOPE_KEY = "issue_orchestrator.after_response_work"
"""Scope key holding the deferred work registered for one request.

Starlette hands the same ``scope`` dict to every layer, so a handler far
inside the middleware chain and the outermost middleware see one list.
"""

DeferredWork = Callable[[], None]


def defer_until_response_sent(scope: Scope, work: DeferredWork) -> None:
    """Register ``work`` to run once this response has been sent.

    Fails loudly when :class:`AfterResponseMiddleware` is not installed:
    a handler that silently ran its teardown inline is exactly the
    defect this module exists to prevent, so a missing boundary must not
    degrade into the old ordering.
    """
    pending = scope.get(AFTER_RESPONSE_SCOPE_KEY)
    if pending is None:
        raise RuntimeError(
            "AfterResponseMiddleware is not installed on this app; "
            "after-response work cannot be deferred safely"
        )
    pending.append(work)


class AfterResponseMiddleware:
    """Runs deferred work after the response is on the wire.

    Install it last (``add_middleware`` puts the newest layer outermost)
    so nothing else can buffer the response behind it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        pending: list[DeferredWork] = []
        scope[AFTER_RESPONSE_SCOPE_KEY] = pending
        response_sent = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_sent
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_sent = True

        await self.app(scope, receive, send_wrapper)

        if not response_sent:
            # The response never completed (disconnect, or an error the
            # server will answer itself). Deferred work is an
            # acknowledgment's continuation; without the acknowledgment
            # it has no reason to run.
            return
        for work in pending:
            work()
