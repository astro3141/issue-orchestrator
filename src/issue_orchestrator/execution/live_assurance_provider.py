"""Where a live-assurance store comes from (#194).

The promotion gate and the lane both need one, and neither should have to know
that the evidence happens to be JSON on disk under a checkout — `AGENTS.md`'s
rule that entry points depend on behaviour-level ports rather than on storage
details, and the guardrail (`entrypoints_no_adapters`) that enforces it.

One function, deliberately: a promotion refused in one place and admitted in
another because two callers built different stores would be the worst possible
failure of this gate.
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.json_live_assurance_store import JsonLiveAssuranceStore
from ..ports.live_assurance_store import LiveAssuranceStore


def live_assurance_store_for(root: Path) -> LiveAssuranceStore:
    """The live-assurance evidence store for the checkout at ``root``."""
    return JsonLiveAssuranceStore(root)


__all__ = ["live_assurance_store_for"]
