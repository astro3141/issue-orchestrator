"""The exact-``A`` reviewer verdict, carried by the attempt record (#345).

Composition only, and the same composition
:class:`~.attempt_execution_identity_store.AttemptExecutionIdentityStore` makes
for §4's other half: ``Attempt`` is already the durable owner keyed by exactly
``(issue, commit)``, already living in the primary checkout's
``.issue-orchestrator/attempts`` sidecar directory rather than in an issue
worktree, and therefore already surviving both an orchestrator restart and
``git worktree remove``.

That durability is the whole reason this exists. The exchange writes
``review-verdict.json`` into its own exchange directory, which is inside the
coder worktree and dies with it — durable enough for the session that made it,
and gone before a Tech Lead batch review (launched ticks or hours later, in a
different worktree) could ever read what the independent reviewer decided about
this exact commit.

The durability, atomic-write and corruption behaviour are the attempt store's,
unchanged; what this adds is the narrow evidence view a gate should depend on.
"""

from __future__ import annotations

import logging

from ..domain.attempt import Attempt, AttemptKey
from ..domain.review_verdict_binding import BoundReviewVerdict
from ..ports.attempt_store import AttemptStore

logger = logging.getLogger(__name__)


class AttemptReviewVerdictStore:
    """:class:`~..ports.candidate_review_verdicts.CandidateReviewVerdictStore`
    over the attempt record."""

    def __init__(self, attempts: AttemptStore) -> None:
        self._attempts = attempts

    def record(self, key: AttemptKey, verdict: BoundReviewVerdict) -> None:
        """Persist ``verdict`` under ``key``, preserving the attempt's facts.

        Both invariants are the store's rather than this caller's:
        :meth:`~..ports.attempt_store.AttemptStore.update` hands over the
        current record so the attempt's other facts survive, and
        :class:`~..domain.attempt.Attempt` enforces the candidate/key agreement
        — so a verdict about another commit is refused for every writer of that
        record rather than only for this one.
        """

        def apply(attempt: Attempt) -> Attempt:
            return attempt.with_review_verdict(verdict)

        self._attempts.update(key, apply)
        logger.info(
            "[REVIEW_VERDICT] recorded verdict=%s for %s@%s",
            verdict.verdict.value,
            key.issue_key,
            verdict.reviewed_sha[:12],
        )

    def read(self, key: AttemptKey) -> BoundReviewVerdict | None:
        attempt = self._attempts.for_key(key)
        return attempt.review_verdict if attempt is not None else None
