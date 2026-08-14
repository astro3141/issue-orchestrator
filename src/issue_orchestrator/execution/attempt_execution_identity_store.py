"""Candidate execution-identity evidence, carried by the attempt record.

Why this store and not a new one. §4's admission evidence is about **one**
``(issue, A)``, and ``Attempt`` is already the durable owner keyed by exactly
``(issue, commit)``, already holding ``validation_record_path``. Putting the
execution identities beside it means one record carries the identity evidence
and *references* §4's other halves, keyed by the same candidate. Its production
sidecar directory is ``<repo_root>/.issue-orchestrator/attempts`` — the primary
checkout, not an issue worktree — so the identity evidence survives both an
orchestrator restart and ``git worktree remove``, which is what admission needs
and what the exchange directory's own artifacts cannot offer.

That claim is deliberately narrow: **this record does not by itself answer §4
after cleanup.** ``validation_record_path`` points into the session directory
that produced it, and that directory dies with the coder worktree — so what
survives here is the identity evidence plus a reference whose target may not.
How the *whole* admitted evidence set survives cleanup is a separate decision
and a prerequisite for #33, whose admission reads all of it.

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
            identities.actor.principal.agent_label,
            identities.reviewer.principal.agent_label,
            key.issue_key,
            identities.candidate_sha[:12],
        )

    def read(self, key: AttemptKey) -> CandidateExecutionIdentities | None:
        attempt = self._attempts.for_key(key)
        return attempt.execution_identities if attempt is not None else None
