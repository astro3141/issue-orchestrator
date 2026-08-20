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

    def for_exchange(
        self, assets: ReviewExchangeRunAssets
    ) -> BoundReviewVerdict | None:
        # The caller hands over the exchange's own run contract, so the
        # canonical exchange directory is read off it rather than rebuilt from
        # a path here: the value object already validated that this is where
        # the run's binding lives.
        exchange_dir = assets.exchange_dir
        if not exchange_dir.exists():
            return None
        return load_review_verdict(exchange_dir)


__all__ = ["RunReviewVerdictBindings"]
