"""Port for the durable exact-``A`` reviewer verdict (#345).

The sibling of :mod:`.execution_identity_store`, and deliberately its exact
shape: §4's admission asks two questions about one ``(issue, A)`` — *who*
executed it (the identities) and *what the reviewer decided* — and both answers
have to survive the worktree that produced them. The identity half already had
a durable home; this is the other half's.

Why a port rather than a direct store call. The verdict is written where it is
concluded — inside the review exchange, an execution adapter — and read where a
gate needs it, which is somewhere else entirely. Naming the capability instead
of the storage keeps "the exchange files what it decided" from becoming "the
exchange knows about attempt sidecars", and keeps the read side free to be a
different narrow view over the same record.

Read-only elsewhere: a verdict is authored by the review exchange as it
concludes. Nothing that merely *consults* one may create or amend it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.attempt import AttemptKey
from ..domain.review_verdict_binding import BoundReviewVerdict


@runtime_checkable
class CandidateReviewVerdictStore(Protocol):
    """Durable read/write of what a reviewer decided about one candidate."""

    def record(self, key: AttemptKey, verdict: BoundReviewVerdict) -> None:
        """Persist ``verdict`` as the review outcome for ``key``.

        Implementations MUST reject a verdict rendered against a commit other
        than ``key.head_sha``: a verdict filed under a candidate it does not
        describe is how ``A'`` inherits ``A``'s review.
        """
        ...

    def read(self, key: AttemptKey) -> BoundReviewVerdict | None:
        """The recorded verdict for ``key``, or ``None`` if none was recorded.

        ``None`` means "no review has settled on this candidate", never
        "approved by default": a caller may only become more conservative on
        it. A damaged record raises, so corruption is never read as absence.
        """
        ...


__all__ = ["CandidateReviewVerdictStore"]
