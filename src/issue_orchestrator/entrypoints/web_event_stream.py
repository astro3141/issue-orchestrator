"""Owner of the dashboard's live event stream (``GET /api/events``).

Before #44 this generator did two things and proved neither: it forwarded
queued events, and every 30 seconds it emitted an SSE *comment* as a keepalive.
A comment is invisible to ``EventSource`` — the browser never surfaces it to
JavaScript — so the page had no way to distinguish "the engine is quiet" from
"this socket died twenty minutes ago". It never had to notice, because it
trusted ``EventSource.onerror`` to tell it. A half-open connection (engine
killed and restarted, laptop resumed, port rebound) never fires that error:
readyState stays ``OPEN`` forever, no reconnect is attempted, and the dashboard
renders a frozen read model as live.

So the keepalive is replaced by a **contracted beacon**,
:data:`~issue_orchestrator.events.catalog.EventName.ENGINE_LIVENESS`, and the
liveness question is answered by the engine rather than by the transport:

- The frame carries the engine's own tick progression
  (:mod:`issue_orchestrator.domain.engine_liveness`), so a consumer can tell a
  healthy socket apart from a working orchestrator.
- It carries ``interval_seconds``, so a consumer's "the stream is gone"
  deadline is derived from the server's actual cadence instead of a constant
  that can drift out of sync with it.
- One is emitted **immediately on subscribe**, so a client has a baseline and a
  cadence from the first frame rather than after a full silent interval.

The absence of the beacon is the signal. That is the only liveness proof that
does not depend on the browser reporting a failure it demonstrably does not
report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from fastapi import Request
from sse_starlette import ServerSentEvent

from ..contracts.public import EngineLivenessPayload
from ..domain.engine_liveness import EngineLiveness, classify_engine_liveness
from ..events.catalog import EventName
from ..events.sse_envelope import apply_sse_envelope

logger = logging.getLogger(__name__)

__all__ = [
    "LIVENESS_INTERVAL_SECONDS",
    "SUBSCRIBER_QUEUE_MAXSIZE",
    "EngineLivenessReader",
    "liveness_frame_payload",
    "stream_events",
]

#: How often the beacon is emitted while the stream is otherwise quiet.
#:
#: Ten seconds, not the old thirty: this is now the resolution at which a
#: consumer can detect a dead stream, and half a minute of a dashboard silently
#: showing stale state is the failure mode #44 is about. It is small enough to
#: be responsive and large enough that an idle stream costs one tiny frame per
#: subscriber per ten seconds.
LIVENESS_INTERVAL_SECONDS = 10.0

#: Bound on how far a slow subscriber may fall behind before it is dropped.
#: Unchanged from the original generator; kept here so the stream's sizing
#: lives with the stream.
SUBSCRIBER_QUEUE_MAXSIZE = 100

#: Supplies the current engine reading. Injected rather than reached for, so
#: the stream can be driven in tests without an orchestrator.
EngineLivenessReader = Callable[[], EngineLiveness]


@dataclass(frozen=True)
class _StreamPorts:
    """The collaborators the stream needs, bound once by the caller."""

    add_subscriber: Callable[["asyncio.Queue"], None]
    remove_subscriber: Callable[["asyncio.Queue"], None]
    read_liveness: EngineLivenessReader


def liveness_frame_payload(
    reading: EngineLiveness, *, interval_seconds: float
) -> EngineLivenessPayload:
    """Build one beacon payload as its committed public contract.

    Returning the contract rather than a bare mapping is what makes the
    published ``sse.engine.liveness`` schema binding on the producer: a field
    renamed here fails to construct instead of quietly shipping a payload no
    documented consumer expects.

    ``seconds_since_tick`` is rounded because a consumer uses it to render an
    age, not to do arithmetic, and full float precision would make otherwise
    identical frames differ on every emission.
    """
    age = reading.seconds_since_tick
    return EngineLivenessPayload(
        state=reading.state.value,
        tick_id=reading.tick_id,
        seconds_since_tick=None if age is None else round(age, 1),
        phase=reading.phase,
        interval_seconds=interval_seconds,
        stall_threshold_seconds=reading.stall_threshold_seconds,
    )


def read_engine_liveness(orchestrator: object | None) -> EngineLiveness:
    """Take a liveness reading from a (possibly absent) orchestrator facade.

    ``None`` is a real production state — the web surface binds its port
    before the engine attaches — and reports ``unknown``. ``state`` is then
    read directly, so an orchestrator that somehow lacks it raises here rather
    than being quietly classified. What it must never do in any case is report
    *healthy* on missing evidence; that is enforced in
    :func:`~issue_orchestrator.domain.engine_liveness.classify_engine_liveness`.

    ``tick_id`` is the one tolerant read. It is operator-facing detail (the
    counter shown beside "Live"), the dashboard's test doubles legitimately
    omit ``event_context``, and its absence only costs the reading a number —
    it can never turn a stalled engine into an advancing one.
    """
    if orchestrator is None:
        return classify_engine_liveness(None, tick_id=None, now=time.time())
    tick_id = getattr(getattr(orchestrator, "event_context", None), "tick_id", None)
    if not isinstance(tick_id, int) or isinstance(tick_id, bool):
        tick_id = None
    return classify_engine_liveness(
        orchestrator.state,  # pyright: ignore[reportAttributeAccessIssue]
        tick_id=tick_id,
        now=time.time(),
    )


def _sse_frame(event_type: str, data: dict) -> ServerSentEvent:
    """Serialize one event, stamped with the public envelope.

    Every frame leaving this module goes through ``apply_sse_envelope`` for the
    same reason ``broadcast_event`` does: ``docs/user/stability.md`` promises a
    ``schema`` field on *every* SSE event, and a beacon that skipped it would
    be the one uncontracted frame on the stream.
    """
    enveloped = apply_sse_envelope(event_type, data)
    return ServerSentEvent(
        event=enveloped.type, data=json.dumps(dict(enveloped.data))
    )


async def stream_events(
    request: Request,
    *,
    add_subscriber: Callable[["asyncio.Queue"], None],
    remove_subscriber: Callable[["asyncio.Queue"], None],
    read_liveness: EngineLivenessReader,
    interval_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> AsyncIterator[ServerSentEvent]:
    """Yield SSE frames for one subscriber until the client goes away.

    The beacon is on a **deadline**, not a quiet-period timer: the loop tracks
    when the next one is due and waits only that long for an event, so a busy
    stream still emits one on schedule. Waiting ``interval_seconds`` for the
    queue instead would starve the beacon exactly when the engine is most
    active — events arriving faster than the interval would reset the wait
    forever — and a consumer watching for the beacon's absence would call a
    perfectly healthy stream dead. The guarantee has to hold unconditionally
    or it is not a guarantee.

    ``interval_seconds`` defaults to :data:`LIVENESS_INTERVAL_SECONDS`, read
    here rather than bound as a default argument so the module constant stays
    the single authority for the cadence — a browser test that needs a
    detectable outage in seconds rather than half a minute lowers it there and
    the wire, the watchdog, and this loop all follow together.

    ``clock`` defaults to the running loop's monotonic clock; a test injects
    one so the deadline can be driven without waiting.
    """
    if interval_seconds is None:
        interval_seconds = LIVENESS_INTERVAL_SECONDS
    ports = _StreamPorts(
        add_subscriber=add_subscriber,
        remove_subscriber=remove_subscriber,
        read_liveness=read_liveness,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
    ports.add_subscriber(queue)
    logger.info("[SSE] Client connected to live event stream")
    if clock is None:
        clock = asyncio.get_running_loop().time

    def beacon() -> ServerSentEvent:
        payload = liveness_frame_payload(
            ports.read_liveness(), interval_seconds=interval_seconds
        )
        return _sse_frame(
            EventName.ENGINE_LIVENESS.value, payload.model_dump()
        )

    try:
        # Baseline first: the consumer learns the cadence and the engine's
        # current state before anything else, so its watchdog is armed from
        # frame one rather than after a silent interval.
        yield beacon()
        next_beacon_at = clock() + interval_seconds
        while True:
            if await request.is_disconnected():
                break
            remaining = next_beacon_at - clock()
            if remaining <= 0:
                yield beacon()
                next_beacon_at = clock() + interval_seconds
                continue
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue  # the deadline passed; the top of the loop emits it
            yield ServerSentEvent(
                event=event["type"], data=json.dumps(event["data"])
            )
    finally:
        ports.remove_subscriber(queue)
        logger.info("[SSE] Client disconnected from live event stream")
