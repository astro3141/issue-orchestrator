"""Filesystem-backed :class:`~..ports.review_verdict_bindings.ReviewVerdictBindings`.

Delegates to :func:`~.review_exchange_records.load_review_verdict`, the same
function the exchange's own reload path uses, so a reader and the writer cannot
disagree about where the binding lives or how it parses.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..domain.review_verdict_binding import BoundReviewVerdict
from .review_exchange_records import load_review_verdict


class RunReviewVerdictBindings:
    """Reads a run directory's exact-SHA verdict binding."""

    def for_run(self, run_dir: Path) -> BoundReviewVerdict | None:
        # The canonical exchange directory for a run, derived by the domain
        # value object rather than re-spelled here: one owner of the layout.
        exchange_dir = ReviewExchangeRunAssets.from_run_dir(run_dir).exchange_dir
        if not exchange_dir.exists():
            return None
        return load_review_verdict(exchange_dir)


__all__ = ["RunReviewVerdictBindings"]
