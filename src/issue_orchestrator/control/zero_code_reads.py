"""The orchestrator-side reads every zero-code lane is decided from.

Two lanes now ask *did this run leave the repository alone?* — the tech_lead
planning lane (#202) and the ordinary evidence lane (#337) — and they must ask
it of the same reads, taken by the orchestrator from the checkout it
provisioned, never from anything the agent writes about itself.

So the contract lives here rather than inside either lane. A lane that needs a
third read declares it by EXTENDING this protocol, which keeps one definition
of the two reads both lanes share and makes the extra read visible as the extra
thing it is.

**``None`` is never "nothing".** Both reads return ``None`` for a read that
FAILED, and the rule every caller of this module owes is the one
:mod:`.candidate_integrity` states for the same two reads: a checkout whose
state could not be read is not a checkout that was proven unchanged. Failing
closed is the lanes' job; handing them a distinguishable failure is this
module's.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

_DIRT_PREVIEW = 5
"""How many altered paths a refusal names before summarising."""


class ZeroCodeWorktreeReader(Protocol):
    """The two orchestrator-side reads that decide a zero-code lane.

    Deliberately the narrowest contract these owners need, and satisfied by the
    completion path's existing git adapter — no new port, and no rummaging
    through a broader one for two methods.
    """

    def get_head_sha(self, worktree: Path) -> str | None:
        """The commit the checkout stands at, or ``None`` when unreadable."""
        ...

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        """Dirty paths for ``mode``, or ``None`` when enumeration failed."""
        ...


def summarise_dirt(paths: Iterable[str]) -> str:
    """Name the altered paths a refusal reports, bounded and in a stable order.

    Shared so a refusal reads identically whichever lane wrote it, and so an
    operator comparing two refusals is comparing the findings rather than two
    formatting habits.
    """
    ordered = sorted(paths)
    preview = ", ".join(ordered[:_DIRT_PREVIEW])
    remaining = len(ordered) - _DIRT_PREVIEW
    return f"{preview} (+{remaining} more)" if remaining > 0 else preview


__all__ = ["ZeroCodeWorktreeReader", "summarise_dirt"]
