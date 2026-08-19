"""Production ``live_operations``, derived from durable truth alone (#149).

:class:`~.control_operation_ownership.ControlOperationOwnership` consumes a
caller-supplied set of live operations and never manufactures one — a durable
lease row says a holder reserved an operation, not that the operation is still
running. This module is that caller: the one place in production that decides
what a control operation *is*, and it decides it from records four existing
owners already keep.

Two properties are the whole design.

**No lease row is read here.** Liveness comes from the attempt sidecar (the
recorded intent, the evaluation history, the same-SHA allowance, the exact-``A``
review verdict) and from board labels the tick already fetched. Nothing in this
module can see the ownership store, so a surviving row can never vouch for
itself into the live set — which is what stops a crash after settlement from
becoming a durable deadlock.

**Ignorance is not emptiness.** A live set is a set of *claims about what is
running*, and reconciliation releases every lease outside it. So a derivation
that could not read a candidate's record must not answer "nothing is live for
this issue": that answer would release a running operation. It answers
"unreadable" instead, and the caller keeps the projection it already published.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.attempt import Attempt
from ..domain.continuation_phase import (
    ContinuationFacts,
    ContinuationPhase,
    derive_continuation_phase,
)
from ..domain.control_operation import (
    ControlOperationExclusions,
    ControlOperationKey,
    ControlOperationKind,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.issue_key import IssueKey
    from ..ports.attempt_store import AttemptStore
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)

CONTINUATION_KIND = ControlOperationKind.PUBLICATION_REVALIDATION_CONTINUATION
"""The one kind of operation this truth derives. Named once, used everywhere."""


@dataclass(frozen=True, slots=True)
class LiveContinuation:
    """One live control operation, with the phase that made it live.

    The phase travels WITH the key rather than being re-derived by whoever acts
    on it. Re-deriving would mean reading the durable record a second time, at
    a second moment, and acting on an answer the exclusion was never based on.
    """

    key: ControlOperationKey
    issue: "Issue"
    attempt: Attempt
    phase: ContinuationPhase


@dataclass(frozen=True, slots=True)
class ContinuationLiveReading:
    """What the durable record said, or that it could not be read.

    Typed rather than a bare tuple for the reason
    :class:`~..domain.control_operation.ControlOperationOwnershipStatus` splits
    ``CONTENDED`` from ``UNAVAILABLE``: "nothing is live" and "we could not
    tell" demand different words, and only the first may drive a release.
    """

    readable: bool
    operations: tuple[LiveContinuation, ...] = ()
    detail: str = ""

    @property
    def keys(self) -> tuple[ControlOperationKey, ...]:
        return tuple(operation.key for operation in self.operations)


@dataclass(frozen=True, slots=True)
class ContinuationReconciliation:
    """What one reconciliation published, and what it may act on.

    Both halves together because they are two readings of one transaction. A
    caller that wanted to *act* on a live operation and asked the projection
    separately would be acting on a later answer than the exclusion was based
    on — the stale decision this whole leaf exists to prevent.
    """

    #: The projection now in force. Every scheduler reader consults this.
    exclusions: ControlOperationExclusions
    #: Every operation the durable record declared live in this pass.
    operations: tuple[LiveContinuation, ...] = ()
    #: False when the durable record could not be read, in which case
    #: ``exclusions`` is the projection that was already standing rather than a
    #: fresh one, and ``operations`` is empty — ignorance, not "nothing is live".
    readable: bool = True

    @property
    def owned(self) -> tuple[LiveContinuation, ...]:
        """The live operations THIS engine holds and may therefore act on.

        ``CONTENDED`` and ``UNAVAILABLE`` operations still exclude ordinary
        work — they are running, or might be — but they are not ours to
        advance, and this is the only collection anything may execute against.
        """
        return tuple(
            operation
            for operation in self.operations
            if self.exclusions.owns(operation.key)
        )


class ContinuationLiveTruth:
    """Derives the live control operations for a board of already-fetched issues."""

    def __init__(self, attempts: "AttemptStore", *, pr_pending_label: str) -> None:
        self._attempts = attempts
        self._pr_pending_label = pr_pending_label

    def read(self, board: Sequence["Issue"]) -> ContinuationLiveReading:
        """Every live continuation across ``board``, or an unreadable answer.

        Args:
            board: The complete set of in-scope issues the caller is about to
                hydrate from. Complete, because reconciliation releases every
                lease this does not name: a partial board would report other
                issues' running operations as finished.

        The derivation is per candidate, but the *result* holds at most one
        operation per issue by construction — an issue's recorded intent lives
        on exactly one attempt, because filing it supersedes the older one.
        """
        operations: list[LiveContinuation] = []
        for issue in board:
            try:
                operations.extend(self._live_for_issue(issue))
            except (OSError, ValueError) as exc:
                detail = f"attempt records for issue #{issue.number} unreadable: {exc}"
                logger.warning("[CONTINUATION] %s", detail)
                return ContinuationLiveReading(readable=False, detail=detail)
        return ContinuationLiveReading(
            readable=True, operations=tuple(operations)
        )

    def _live_for_issue(self, issue: "Issue") -> list[LiveContinuation]:
        board_shows_pr_pending = self._pr_pending_label in tuple(issue.labels)
        live: list[LiveContinuation] = []
        for attempt in self._attempts.for_issue(issue.key):
            phase = derive_continuation_phase(
                _facts(attempt, board_shows_pr_pending=board_shows_pr_pending)
            )
            if not phase.live:
                continue
            live.append(
                LiveContinuation(
                    key=_operation_key(issue.key, attempt),
                    issue=issue,
                    attempt=attempt,
                    phase=phase,
                )
            )
        return live


def _facts(attempt: Attempt, *, board_shows_pr_pending: bool) -> ContinuationFacts:
    """The durable facts one attempt states, reduced to the decision's shape."""
    latest = attempt.latest_publication_evaluation
    verdict = attempt.continuation_review_verdict
    return ContinuationFacts(
        descriptor=attempt.continuation_descriptor,
        has_publication_evaluation=latest is not None,
        # Asked of the attempt rather than of the receipt, so "did this
        # candidate pass publication" is answered by the one owner of that
        # question and cannot drift from what review admission reads.
        latest_publication_passed=attempt.publication_validation_passed,
        revalidation_allowance_available=attempt.revalidation_allowance_available,
        # The binding is re-checked here even though the attempt refuses to
        # hold a verdict naming another commit: an approval is the single most
        # consequential fact in this predicate, and it costs one comparison to
        # make ``A'`` inheriting ``A``'s review impossible at both ends.
        review_verdict=(
            verdict.verdict
            if verdict is not None and verdict.covers(attempt.key.head_sha)
            else None
        ),
        board_shows_pr_pending=board_shows_pr_pending,
    )


def _operation_key(
    issue_key: "IssueKey", attempt: Attempt
) -> ControlOperationKey:
    """The operation identity for a candidate, spelled by its own owner.

    Built from the BOARD's issue key rather than the sidecar's rebuilt one, and
    from the attempt's commit: ``ControlOperationKey`` canonicalises the first
    through the durable codec and normalises the second, so the two spellings
    of one issue that ``GitHubIssueKey`` and ``StoredIssueKey`` produce cannot
    become two operations.
    """
    return ControlOperationKey(issue_key, attempt.key.head_sha, CONTINUATION_KIND)


__all__ = [
    "CONTINUATION_KIND",
    "ContinuationLiveReading",
    "ContinuationLiveTruth",
    "ContinuationReconciliation",
    "LiveContinuation",
]
