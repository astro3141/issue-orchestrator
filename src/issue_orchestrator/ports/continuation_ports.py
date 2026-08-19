"""The durable ports one control continuation is assembled around (#149).

A typed bundle rather than two loose fields, because the two are useless
apart: the continuation owner needs somewhere to record that it holds an
operation AND somewhere to read back what the run it drove decided, and a
composition that supplied one without the other would produce an owner that
either cannot exclude or cannot settle.

Ports only. The OWNER is built by the facade, which holds the live
``OrchestratorState`` this container deliberately does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from .control_operation_ownership_store import ControlOperationOwnershipStore
from .review_verdict_bindings import ReviewVerdictBindings


@dataclass(frozen=True, slots=True)
class ContinuationPorts:
    """Durable leases (#146) and finished runs' exact-SHA verdicts (#149)."""

    ownership_store: ControlOperationOwnershipStore
    review_verdicts: ReviewVerdictBindings


__all__ = ["ContinuationPorts"]
