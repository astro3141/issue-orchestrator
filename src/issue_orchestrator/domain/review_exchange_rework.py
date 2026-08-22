"""Who may move the candidate when a review round asks for changes (#180).

One review exchange serves two callers that mean different things by
"changes requested".

The ordinary completion path owns the coder: its exchange is the whole review
lifecycle for a live session, and a ``CHANGES_REQUESTED`` round is an
instruction to that coder, in that worktree, inside that exchange. Reworking
in place is the point.

The control continuation (#149) owns no coder. It replays a recorded intent
against one exact candidate ``A``, and the handoff #149 settled is ordered:
``CHANGES_REQUESTED(A)`` becomes durable while the control operation still
concerns exactly ``A``, the operation settles and releases its ownership,
reconciliation publishes the exclusion change, and only then does ordinary
rework become eligible to produce ``A'``. An exchange that ran its own coder
round would move the branch to ``A'`` while the operation was still live —
which is how #193 bound an approval to ``A'`` and left ``A`` with no review
authority at all.

So the caller states which of the two it is, and the round loop reads exactly
one thing off it: whether a non-approving round may be followed by a coder
turn. It is deliberately not a bound on rounds — ``max_rounds`` already is one,
and a bound of 1 would still let the coder turn of that single round run,
which is precisely the turn that must not happen.
"""

from __future__ import annotations

from enum import Enum


class ReviewExchangeRework(Enum):
    """What one exchange does when it concludes changes are needed."""

    #: The exchange owns the coder and reworks in place: the reviewer's
    #: feedback goes back to the coder for another bounded round. Every
    #: ordinary completion — session, publish retry, tech lead — is this.
    IN_EXCHANGE = "in_exchange"
    #: The exchange owns no coder and terminates instead, leaving a durable
    #: verdict bound to the commit it presented. Rework, if any, belongs to
    #: whoever the caller hands the candidate back to.
    HAND_OFF = "hand_off"

    @property
    def runs_coder_rounds(self) -> bool:
        """Whether a non-approving round may be followed by a coder turn."""
        return self is ReviewExchangeRework.IN_EXCHANGE


__all__ = ["ReviewExchangeRework"]
