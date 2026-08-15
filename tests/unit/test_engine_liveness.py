"""Engine liveness is proven by advancing ticks, never by anything else (#44).

The incident this pins: the dashboard reported a healthy engine for hours on
evidence that was not evidence — a reachable HTTP surface and a non-null
EventSource handle. These tests fix the one rule that replaces that
inference: a reading is ``advancing`` only when a tick actually *completed*
recently, and every other case is reported honestly rather than defaulted to
healthy.
"""

from __future__ import annotations

from issue_orchestrator.domain.engine_liveness import (
    DEFAULT_STALL_THRESHOLD_SECONDS,
    EngineLivenessState,
    classify_engine_liveness,
)
from issue_orchestrator.domain.models import OrchestratorState


NOW = 1_000_000.0


def _state(**kwargs) -> OrchestratorState:
    state = OrchestratorState()
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


class TestClassification:
    def test_recent_tick_completion_is_advancing(self) -> None:
        reading = classify_engine_liveness(
            _state(last_tick_completed_at=NOW - 5.0, last_tick_started_at=NOW - 6.0),
            tick_id=485,
            now=NOW,
        )

        assert reading.state is EngineLivenessState.ADVANCING
        assert reading.advancing is True
        assert reading.tick_id == 485
        assert reading.seconds_since_tick == 5.0

    def test_tick_older_than_the_threshold_is_stalled(self) -> None:
        reading = classify_engine_liveness(
            _state(last_tick_completed_at=NOW - (DEFAULT_STALL_THRESHOLD_SECONDS + 1)),
            tick_id=485,
            now=NOW,
        )

        assert reading.state is EngineLivenessState.STALLED
        assert reading.advancing is False

    def test_tick_exactly_at_the_threshold_is_still_advancing(self) -> None:
        """The boundary belongs to the healthy side; a stall must be strict."""
        reading = classify_engine_liveness(
            _state(last_tick_completed_at=NOW - DEFAULT_STALL_THRESHOLD_SECONDS),
            tick_id=1,
            now=NOW,
        )

        assert reading.state is EngineLivenessState.ADVANCING

    def test_started_but_never_completed_is_stalled_and_names_the_phase(self) -> None:
        """A loop stuck mid-tick is the case a completion-only age would hide."""
        reading = classify_engine_liveness(
            _state(
                last_tick_started_at=NOW - 300.0,
                last_tick_completed_at=0.0,
                current_tick_phase="planning",
            ),
            tick_id=12,
            now=NOW,
        )

        assert reading.state is EngineLivenessState.STALLED
        assert reading.seconds_since_tick == 300.0
        assert reading.phase == "planning"

    def test_a_fresh_start_with_no_completion_is_still_not_advancing(self) -> None:
        """Starting a tick is not evidence of finishing one."""
        reading = classify_engine_liveness(
            _state(last_tick_started_at=NOW - 1.0, last_tick_completed_at=0.0),
            tick_id=1,
            now=NOW,
        )

        assert reading.state is EngineLivenessState.STALLED

    def test_no_tick_recorded_is_unknown_not_healthy(self) -> None:
        reading = classify_engine_liveness(_state(), tick_id=0, now=NOW)

        assert reading.state is EngineLivenessState.UNKNOWN
        assert reading.seconds_since_tick is None

    def test_absent_orchestrator_is_unknown_not_healthy(self) -> None:
        """A web process serving before the engine attaches must say so."""
        reading = classify_engine_liveness(None, tick_id=None, now=NOW)

        assert reading.state is EngineLivenessState.UNKNOWN
        assert reading.advancing is False
        assert reading.tick_id is None

    def test_clock_moving_backwards_never_yields_a_negative_age(self) -> None:
        reading = classify_engine_liveness(
            _state(last_tick_completed_at=NOW + 30.0), tick_id=3, now=NOW
        )

        assert reading.seconds_since_tick == 0.0
        assert reading.state is EngineLivenessState.ADVANCING

    def test_threshold_is_caller_configurable(self) -> None:
        state = _state(last_tick_completed_at=NOW - 20.0)

        assert (
            classify_engine_liveness(
                state, tick_id=1, now=NOW, stall_threshold_seconds=10.0
            ).state
            is EngineLivenessState.STALLED
        )
        assert (
            classify_engine_liveness(
                state, tick_id=1, now=NOW, stall_threshold_seconds=60.0
            ).state
            is EngineLivenessState.ADVANCING
        )
