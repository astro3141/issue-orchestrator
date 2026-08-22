"""The catch-all bound on post-review validation reroutes.

A validation failure discovered AFTER a review approval is handed back to the
exchange's coder rather than surfaced, which means the completion pipeline can
re-enter the same path on every tick. Bounding that is one rule with one piece
of state — a count per ``(session, head_sha)`` — and it lives here rather than
as a dict on the completion processor so that "who may spend the budget" is not
a question of who happens to hold the field.

The key is the pair, not the session: a SHA that advances is progress, and it
resets the count by being a different key. A permanently-failing validation on
one commit is what the ceiling exists for.

The bound is deliberately coarse. The in-loop bounds (``max_rounds``,
``max_no_progress``) are the ones that normally stop an exchange; this one only
has to make an infinite loop impossible if the cache predicate that prevents
re-entry is ever weakened or bypassed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .completion_types import ProcessingResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 10
"""Used when no config states a round bound. Matches the historic literal."""


class ValidationRerouteBudget:
    """Counts consecutive reroutes per ``(session, head_sha)`` and halts."""

    def __init__(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        # Reuses ``review_exchange_max_rounds`` at the composition site, so the
        # catch-all ceiling matches the in-loop bound rather than inventing a
        # second number for the same question.
        self._max_attempts = max_attempts
        self._counts: dict[tuple[str, str], int] = {}

    def consume(
        self, *, session_name: str, validation_record_path: Path
    ) -> ProcessingResult | None:
        """Spend one attempt; return a halting result when the budget is gone.

        ``None`` means the caller may reroute. A record with no readable
        ``head_sha`` also returns ``None``: there is no key to count under, and
        escalating on an unreadable record would halt work the in-loop bounds
        still cover.
        """
        head_sha = _validation_head_sha(validation_record_path)
        if not head_sha:
            return None
        key = (session_name, head_sha)
        attempt = self._counts.get(key, 0) + 1
        self._counts[key] = attempt
        if attempt <= self._max_attempts:
            return None
        logger.error(
            "[VALIDATION_REROUTE] budget exhausted: session=%s head_sha=%s "
            "attempts=%d max=%d — halting reroute",
            session_name,
            head_sha[:8],
            attempt,
            self._max_attempts,
        )
        return ProcessingResult(
            success=False,
            message=(
                "Validation failed after review approval and the reroute "
                f"budget is exhausted (attempts={attempt} "
                f"max={self._max_attempts}); halting to surface the failure"
            ),
            errors=[
                f"validation_reroute: exhausted budget on {head_sha[:8]} "
                f"(attempts={attempt}, max={self._max_attempts})"
            ],
            review_exchange_halted=True,
        )


def _validation_head_sha(record_path: Path) -> str | None:
    try:
        data = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    head_sha = data.get("head_sha")
    return head_sha if isinstance(head_sha, str) and head_sha else None


__all__ = ["DEFAULT_MAX_ATTEMPTS", "ValidationRerouteBudget"]
