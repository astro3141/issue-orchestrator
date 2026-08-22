"""Filesystem-backed :class:`~..ports.review_verdict_bindings.ReviewVerdictBindings`.

Delegates to :func:`~.review_exchange_records.load_review_verdict`, the same
function the exchange's own reload path uses, so a reader and the writer cannot
disagree about where the binding lives or how it parses.
"""

from __future__ import annotations

from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..domain.review_verdict_binding import BoundReviewVerdict
from .review_exchange_records import load_review_verdict


class RunReviewVerdictBindings:
    """Reads an exchange run's exact-SHA verdict binding."""

    def for_exchange_run(
        self, exchange_run: ReviewExchangeRunAssets
    ) -> BoundReviewVerdict | None:
        # The assets carry the canonical exchange directory already — the same
        # value object the writer allocated — so nothing is re-derived from a
        # path here, and there is no directory to guess at.
        exchange_dir = exchange_run.exchange_dir
        if not exchange_dir.exists():
            return None
        return load_review_verdict(exchange_dir)


__all__ = ["RunReviewVerdictBindings"]
