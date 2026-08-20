"""Reading back the exact-SHA verdict one review exchange bound (#149).

The binding itself is a domain record
(:class:`~..domain.review_verdict_binding.BoundReviewVerdict`) and the exchange
writes it into its own run directory. Where that directory *is*, and how the
file inside it is named and parsed, is filesystem knowledge that belongs to the
adapter that writes it — so a control-layer caller that needs to know what a
finished run decided asks for the verdict, not for a path.

Read-only on purpose. A verdict is authored by the review exchange as it
concludes; nothing that merely *consults* one may create or amend it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.review_verdict_binding import BoundReviewVerdict


@runtime_checkable
class ReviewVerdictBindings(Protocol):
    """The exact-SHA verdicts finished review-exchange runs left behind."""

    def for_run(self, run_dir: Path) -> BoundReviewVerdict | None:
        """The verdict this run bound, or ``None`` if it bound none.

        ``None`` means the run reached no verdict it could bind — an exchange
        that never concluded, or one whose presented commit the orchestrator
        could not observe as a canonical SHA. It never means "approved by
        default": a caller may only become more conservative on ``None``.

        A binding that exists but cannot be parsed raises. A corrupt authority
        artifact is not an absent one, and reading the first as the second is
        how a damaged record becomes a clean answer.
        """
        ...


__all__ = ["ReviewVerdictBindings"]
