"""Filing the publication gate's verdict where it outlives the worktree (#85).

Composition only. The storage is :class:`~..ports.attempt_store.AttemptStore`
— the record already keyed by exactly ``(issue, commit)``, already durable in
the primary checkout, already holding the other halves of the evidence about
one candidate. Nothing here is a second place facts live; what it adds is the
one narrow write the publication gate needs, so the gate does not have to know
that the bytes happen to land in an attempt sidecar. This is the same shape as
``execution.attempt_execution_identity_store``, which files §4's identity half
on the same record.

The verdict is taken from the gate's own :class:`ValidationRecord` and nothing
else — in particular ``head_sha`` comes from the record rather than from a
second read of the working copy. A gate that validated A and a key built from
a HEAD re-read moments later could disagree, and the disagreement would be
invisible: the receipt would be filed under the wrong candidate and read as
evidence about it. One source, one candidate.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..domain.issue_key import IssueKey
from ..domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from ..ports.attempt_store import AttemptStore
from ..ports.session_output import ValidationRecord
from ..ports.validation_attempt_key_factory import ValidationAttemptKeyFactory

logger = logging.getLogger(__name__)


def receipt_for(record: ValidationRecord) -> ValidationVerdictReceipt:
    """The durable receipt for a gate run, taken entirely from its record.

    Every field is a projection of what the gate actually executed and
    decided, so a receipt cannot claim a suite, a command or a profile the run
    did not have.
    """
    return ValidationVerdictReceipt(
        suite=record.suite,
        head_sha=record.head_sha,
        verdict=ValidationVerdict.observed(
            passed=record.passed, timed_out=record.timed_out
        ),
        command=record.command,
        profile=record.profile,
    )


class PublicationVerdictReceipts:
    """Records each publication-gate run's verdict on ``Attempt(issue, A)``."""

    def __init__(
        self,
        attempts: AttemptStore,
        attempt_keys: ValidationAttemptKeyFactory,
    ) -> None:
        self._attempts = attempts
        self._attempt_keys = attempt_keys

    def record(
        self,
        *,
        issue_key: IssueKey,
        record: ValidationRecord,
    ) -> ValidationVerdictReceipt:
        """Persist ``record``'s verdict under the candidate it validated.

        Both invariants are the store's and the domain's rather than this
        caller's: :meth:`~..ports.attempt_store.AttemptStore.update` hands over
        the current attempt so its other facts survive the write, and
        :class:`~..domain.attempt.Attempt` enforces that the receipt names the
        key's own commit.
        """
        receipt = receipt_for(record)
        key = self._attempt_keys.for_validation_attempt(
            issue_key=issue_key,
            head_sha=receipt.head_sha,
        )
        self._attempts.update(
            key, lambda attempt: replace(attempt, publication_verdict=receipt)
        )
        logger.info(
            "[PUBLICATION_VERDICT] recorded %s suite=%s profile=%s for %s@%s",
            receipt.verdict.value,
            receipt.suite,
            receipt.profile,
            key.issue_key,
            receipt.head_sha[:12],
        )
        return receipt


__all__ = ["PublicationVerdictReceipts", "receipt_for"]
