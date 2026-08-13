"""Candidate execution-identity evidence, carried by the attempt record.

Why this store and not a new one. §4's admission evidence is two records about
**one** ``(issue, A)``: validation's, and review's. ``Attempt`` is already the
durable owner keyed by exactly ``(issue, commit)`` and already holds
``validation_record_path``, so putting the execution identities beside it makes
one record answer §4 for one candidate. Its production sidecar directory is
``<repo_root>/.issue-orchestrator/attempts`` — the primary checkout, not an
issue worktree — so the evidence survives both an orchestrator restart and
``git worktree remove``, which is what admission needs and what the exchange
directory's own artifacts cannot offer.

The alternatives were measured, not assumed. The exchange directory
(``execution/review_exchange_records.py``) lives inside the coder worktree and
dies with it. The timeline store is durable but is a per-issue ring buffer with
a ``delete(issue)``: evidence that a trimming policy may drop is not evidence a
gate can rely on, and it is keyed by issue rather than by candidate. Neither
disqualifies them as *traces*; both disqualify them as the authority copy.

This is admission evidence, not disposition state, so §9's "no second source of
truth" is not what decides it — but the same instinct applies, and the attempt
record is the existing owner rather than a second one.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..domain.attempt import AttemptKey
from ..domain.execution_identity import CandidateExecutionIdentities
from ..ports.attempt_store import AttemptStore

logger = logging.getLogger(__name__)


class AttemptExecutionIdentityStore:
    """:class:`~..ports.execution_identity_store.CandidateExecutionIdentityStore`
    over the attempt record.

    Composition only: the durability, atomic-write and corruption behaviour are
    the attempt store's, unchanged. What this adds is the narrow evidence view
    a Foundation gate should depend on, so a caller never has to know that the
    bytes happen to live in an attempt sidecar.
    """

    def __init__(self, attempts: AttemptStore) -> None:
        self._attempts = attempts

    def record(
        self, key: AttemptKey, identities: CandidateExecutionIdentities
    ) -> None:
        """Persist ``identities`` under ``key``, preserving the attempt's facts.

        Both invariants are the store's rather than this caller's:
        :meth:`~..ports.attempt_store.AttemptStore.update` hands over the
        current record so the attempt's other facts survive, and
        :class:`Attempt` enforces the candidate/key agreement — so both hold
        for every writer of that record rather than only for this one.
        """
        self._attempts.update(
            key, lambda attempt: replace(attempt, execution_identities=identities)
        )
        logger.info(
            "[EXECUTION_IDENTITY] recorded actor=%s reviewer=%s for %s@%s",
            identities.actor.agent_label,
            identities.reviewer.agent_label,
            key.issue_key,
            identities.candidate_sha[:12],
        )

    def read(self, key: AttemptKey) -> CandidateExecutionIdentities | None:
        attempt = self._attempts.for_key(key)
        return attempt.execution_identities if attempt is not None else None
