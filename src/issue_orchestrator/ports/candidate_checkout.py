"""Materializing one recorded candidate at exactly the commit it is about (#139).

The revalidation route re-evaluates an artifact that already has durable
evidence filed against it. That artifact is a commit, not a branch: the branch
that once pointed at it may have moved, and evaluating whatever it points at
now would file a verdict about other work under the recorded candidate's name.

So the behaviour this port names is "give me a working copy of *this SHA*",
never "give me a working copy of this branch". A caller that holds only a
branch cannot express the request, which is the point — the failure direction
this closes is a moved branch being revalidated in the candidate's place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class CandidateCheckoutError(RuntimeError):
    """Raised when a candidate's exact commit cannot be materialized."""


@dataclass(frozen=True, slots=True)
class MaterializedCandidate:
    """A working copy that is checked out at exactly ``head_sha``.

    Both fields together, because the path alone does not prove what is in it.
    Implementations must verify the checked-out HEAD before returning one, so a
    holder of this value never has to re-ask.
    """

    path: Path
    head_sha: str


@runtime_checkable
class CandidateCheckouts(Protocol):
    """Disposable working copies of exact commits."""

    def materialize(self, head_sha: str) -> MaterializedCandidate:
        """Check out ``head_sha`` into a fresh working copy.

        Raises:
            CandidateCheckoutError: when the commit is unavailable, the
                destination is occupied, or the resulting checkout does not
                actually sit at ``head_sha``.
        """
        ...

    def release(self, candidate: MaterializedCandidate) -> None:
        """Discard a checkout obtained from :meth:`materialize`."""
        ...


__all__ = [
    "CandidateCheckoutError",
    "CandidateCheckouts",
    "MaterializedCandidate",
]
