"""Engine liveness — proven by advancing ticks, not by anything else.

The dashboard used to infer "the engine is alive" from things that are not
evidence of it: the web process answering ``/api/info``, an ``EventSource``
object existing in the page, a rendered read model. During the #44 incident all
three were true for hours while the engine's events reached nobody, so the
operator read a frozen board as a live one.

The only signal that actually moves when the orchestration loop moves is the
tick heartbeat ``OrchestratorState`` already records
(``last_tick_completed_at``, ``last_tick_started_at``, ``current_tick_phase``).
This module turns that raw state into the one value object every surface reads,
so "is the engine alive" has a single answer rather than one per consumer.

Deliberately pure domain: no transport, no orchestrator facade, no clock of its
own. The caller supplies ``now`` so the classification is testable without
waiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import OrchestratorState

__all__ = [
    "DEFAULT_STALL_THRESHOLD_SECONDS",
    "EngineLivenessState",
    "EngineLiveness",
    "classify_engine_liveness",
]

#: How long a tick may take before the loop counts as stalled rather than busy.
#: Deliberately well above ``tick_telemetry.SLOW_TICK_SECONDS`` (10s, the "this
#: tick is worth flagging" budget): a tick that overruns the budget is slow, and
#: only a loop that has not completed *any* tick for this long is stalled.
DEFAULT_STALL_THRESHOLD_SECONDS = 120.0


class EngineLivenessState(str, Enum):
    """Whether orchestration ticks are advancing."""

    #: A tick completed within the stall threshold.
    ADVANCING = "advancing"
    #: The loop has started at least one tick but none has completed recently.
    STALLED = "stalled"
    #: No tick has ever been recorded — startup, or no engine attached.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EngineLiveness:
    """One reading of engine liveness, taken at ``observed_at``.

    ``tick_id`` is the loop's own counter. A consumer that sees two readings
    with the same ``tick_id`` knows the loop has not advanced between them
    even if both readings arrived promptly — which is the difference between
    "the transport is healthy" and "the engine is working".
    """

    state: EngineLivenessState
    tick_id: int | None
    last_tick_completed_at: float
    last_tick_started_at: float
    phase: str
    observed_at: float
    stall_threshold_seconds: float

    @property
    def seconds_since_tick(self) -> float | None:
        """Age of the most recent tick signal, or ``None`` if there is none.

        Falls back to the start time when a tick has begun but not finished:
        that is precisely the stuck-mid-tick case, and reporting ``None`` there
        would hide it.
        """
        reference = self.last_tick_completed_at or self.last_tick_started_at
        if reference <= 0:
            return None
        return max(0.0, self.observed_at - reference)

    @property
    def advancing(self) -> bool:
        return self.state is EngineLivenessState.ADVANCING


def classify_engine_liveness(
    state: "OrchestratorState | None",
    *,
    tick_id: int | None,
    now: float,
    stall_threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS,
) -> EngineLiveness:
    """Read tick progression off ``state`` and classify it.

    ``state`` is optional because the web surface can be serving before an
    orchestrator is attached (and during tests). That is reported as
    :attr:`EngineLivenessState.UNKNOWN` rather than defaulted to healthy —
    "no evidence" must never render as "alive".
    """
    if state is None:
        return EngineLiveness(
            state=EngineLivenessState.UNKNOWN,
            tick_id=tick_id,
            last_tick_completed_at=0.0,
            last_tick_started_at=0.0,
            phase="",
            observed_at=now,
            stall_threshold_seconds=stall_threshold_seconds,
        )

    completed = _as_epoch(state.last_tick_completed_at)
    started = _as_epoch(state.last_tick_started_at)
    phase = (state.current_tick_phase or "").strip()

    reading = EngineLiveness(
        state=EngineLivenessState.UNKNOWN,
        tick_id=tick_id,
        last_tick_completed_at=completed,
        last_tick_started_at=started,
        phase=phase,
        observed_at=now,
        stall_threshold_seconds=stall_threshold_seconds,
    )
    age = reading.seconds_since_tick
    if age is None:
        return reading
    resolved = (
        EngineLivenessState.ADVANCING
        if completed > 0 and age <= stall_threshold_seconds
        else EngineLivenessState.STALLED
    )
    return EngineLiveness(
        state=resolved,
        tick_id=reading.tick_id,
        last_tick_completed_at=completed,
        last_tick_started_at=started,
        phase=phase,
        observed_at=now,
        stall_threshold_seconds=stall_threshold_seconds,
    )


def _as_epoch(value: object) -> float:
    """Coerce a recorded timestamp to epoch seconds, or 0.0 when unusable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else 0.0
    return 0.0
