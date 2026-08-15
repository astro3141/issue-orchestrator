"""The live event stream must prove its own liveness (#44).

The stream used to emit an SSE *comment* as its keepalive. Comments never
reach `EventSource` consumers, so a browser holding a half-open socket had no
frame to miss and no error to react to — it simply rendered a frozen board.
These tests pin the replacement: a contracted beacon, emitted immediately and
then on a known cadence, carrying the engine's own tick progression.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from issue_orchestrator.domain.engine_liveness import (
    EngineLivenessState,
    classify_engine_liveness,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.entrypoints.web_event_stream import (
    LIVENESS_INTERVAL_SECONDS,
    read_engine_liveness,
    stream_events,
)
from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION

NOW = 5_000_000.0


class FakeRequest:
    """Minimal stand-in for the parts of ``Request`` the stream touches."""

    def __init__(self) -> None:
        self.connected = True

    async def is_disconnected(self) -> bool:
        return not self.connected


class Subscribers:
    """Records the stream's registration lifecycle at the port boundary."""

    def __init__(self) -> None:
        self.queues: list[asyncio.Queue] = []
        self.removed: list[asyncio.Queue] = []
        self.registered = asyncio.Event()

    def add(self, queue: asyncio.Queue) -> None:
        self.queues.append(queue)
        self.registered.set()

    def remove(self, queue: asyncio.Queue) -> None:
        self.removed.append(queue)


def _advancing_reading(tick_id: int = 485):
    state = OrchestratorState()
    state.last_tick_completed_at = NOW - 3.0
    state.current_tick_phase = ""
    return classify_engine_liveness(state, tick_id=tick_id, now=NOW)


def _stalled_reading(tick_id: int = 485):
    state = OrchestratorState()
    state.last_tick_completed_at = NOW - 600.0
    state.current_tick_phase = "planning"
    return classify_engine_liveness(state, tick_id=tick_id, now=NOW)


def _open_stream(request, subscribers, reading, *, interval=LIVENESS_INTERVAL_SECONDS):
    return stream_events(
        request,
        add_subscriber=subscribers.add,
        remove_subscriber=subscribers.remove,
        read_liveness=lambda: reading,
        interval_seconds=interval,
    )


class TestLivenessBeacon:
    @pytest.mark.asyncio
    async def test_first_frame_is_the_beacon_carrying_the_engine_reading(self) -> None:
        subscribers = Subscribers()
        iterator = _open_stream(FakeRequest(), subscribers, _advancing_reading(485))

        frame = await iterator.__anext__()

        assert frame.event == "engine.liveness"
        payload = json.loads(frame.data)
        assert payload["state"] == "advancing"
        assert payload["tick_id"] == 485
        assert payload["seconds_since_tick"] == 3.0
        await iterator.aclose()

    @pytest.mark.asyncio
    async def test_beacon_publishes_its_own_cadence_so_a_watchdog_cannot_drift(
        self,
    ) -> None:
        """The client's deadline is derived from this, not from a constant."""
        subscribers = Subscribers()
        iterator = _open_stream(
            FakeRequest(), subscribers, _advancing_reading(), interval=2.5
        )

        payload = json.loads((await iterator.__anext__()).data)

        assert payload["interval_seconds"] == 2.5
        await iterator.aclose()

    @pytest.mark.asyncio
    async def test_beacon_carries_the_public_schema_envelope(self) -> None:
        """It is on the versioned stream, so it cannot be the one bare frame."""
        subscribers = Subscribers()
        iterator = _open_stream(FakeRequest(), subscribers, _advancing_reading())

        payload = json.loads((await iterator.__anext__()).data)

        assert payload["schema"] == EVENT_SCHEMA_VERSION
        await iterator.aclose()

    @pytest.mark.asyncio
    async def test_a_quiet_stream_keeps_emitting_beacons(self) -> None:
        """Silence must be impossible: absence of a frame is the loss signal."""
        subscribers = Subscribers()
        iterator = _open_stream(
            FakeRequest(), subscribers, _advancing_reading(), interval=0.01
        )

        first = await iterator.__anext__()
        second = await iterator.__anext__()

        assert first.event == "engine.liveness"
        assert second.event == "engine.liveness"
        await iterator.aclose()

    @pytest.mark.asyncio
    async def test_a_busy_stream_still_emits_beacons_on_schedule(self) -> None:
        """The cadence is a deadline, not a quiet-period timer.

        If the beacon only fired after ``interval`` of silence, a stream busy
        enough to keep resetting that timer would starve it — and the client
        would call the *most active* engine dead. The guarantee has to hold
        while events are flowing.
        """
        subscribers = Subscribers()
        elapsed = [0.0]
        iterator = stream_events(
            FakeRequest(),
            add_subscriber=subscribers.add,
            remove_subscriber=subscribers.remove,
            read_liveness=_advancing_reading,
            interval_seconds=10.0,
            # A manual clock: every read advances it by a second, so the
            # deadline arrives without the test waiting for it.
            clock=lambda: elapsed[0],
        )
        await iterator.__anext__()  # baseline beacon
        await subscribers.registered.wait()
        queue = subscribers.queues[0]

        seen: list[str] = []
        for _ in range(40):
            queue.put_nowait({"type": "tick.completed", "data": {}})
            elapsed[0] += 1.0
            seen.append((await iterator.__anext__()).event)

        assert "engine.liveness" in seen, (
            "a continuously busy stream never emitted a beacon: " f"{set(seen)}"
        )
        await iterator.aclose()

    @pytest.mark.asyncio
    async def test_beacon_reports_a_stalled_engine_on_a_healthy_stream(self) -> None:
        """Transport health and engine health are separate answers."""
        subscribers = Subscribers()
        iterator = _open_stream(FakeRequest(), subscribers, _stalled_reading())

        payload = json.loads((await iterator.__anext__()).data)

        assert payload["state"] == "stalled"
        assert payload["phase"] == "planning"
        assert payload["seconds_since_tick"] == 600.0
        await iterator.aclose()


class TestEventPassThrough:
    @pytest.mark.asyncio
    async def test_broadcast_events_reach_the_subscriber_unmodified(self) -> None:
        subscribers = Subscribers()
        iterator = _open_stream(FakeRequest(), subscribers, _advancing_reading())
        await iterator.__anext__()  # beacon

        read = asyncio.create_task(iterator.__anext__())
        await subscribers.registered.wait()
        subscribers.queues[0].put_nowait(
            {"type": "session.started", "data": {"issue_number": 44, "schema": 1}}
        )
        frame = await read

        assert frame.event == "session.started"
        assert json.loads(frame.data) == {"issue_number": 44, "schema": 1}
        await iterator.aclose()

    @pytest.mark.asyncio
    async def test_subscriber_is_deregistered_when_the_client_disconnects(self) -> None:
        subscribers = Subscribers()
        request = FakeRequest()
        iterator = _open_stream(request, subscribers, _advancing_reading(), interval=0.01)
        await iterator.__anext__()

        request.connected = False
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()

        assert subscribers.removed == subscribers.queues


class TestReadEngineLiveness:
    def test_missing_orchestrator_reads_unknown(self) -> None:
        assert read_engine_liveness(None).state is EngineLivenessState.UNKNOWN

    def test_reads_tick_id_from_the_orchestrator_event_context(self) -> None:
        class Ctx:
            tick_id = 77

        class Orchestrator:
            state = OrchestratorState()
            event_context = Ctx()

        assert read_engine_liveness(Orchestrator()).tick_id == 77

    def test_an_orchestrator_without_state_fails_loudly(self) -> None:
        """A facade missing its state is a bug, not an "unknown" reading."""

        class Broken:
            pass

        with pytest.raises(AttributeError):
            read_engine_liveness(Broken())

    def test_a_non_integer_tick_id_is_reported_as_absent(self) -> None:
        """A test double or partially built facade must not fake a tick."""

        class Ctx:
            tick_id = "not-a-tick"

        class Orchestrator:
            state = OrchestratorState()
            event_context = Ctx()

        assert read_engine_liveness(Orchestrator()).tick_id is None
