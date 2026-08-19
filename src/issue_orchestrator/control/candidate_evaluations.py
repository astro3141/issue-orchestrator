"""One candidate's durable evaluation history, as one contract sees it (#139).

``Attempt(issue, A)`` keeps an ordered, append-only list of every gate run that
reached a verdict on that candidate. Two questions are asked of it, and both
belong together because both are answered *for one contract*:

* **"has this contract already decided about this commit?"** — the cache
  consultation. Answered from the receipts, which live in the primary checkout,
  rather than by dereferencing ``Attempt.validation_record_path``: that path
  points into the session directory inside the coder worktree, so after
  cleanup the attempt still said a gate ran without any reader being able to
  find out what it decided.
* **"file what this contract just decided"** — the append. Appended, never
  assigned: a slot meant a second evaluation of the same candidate destroyed
  the first, and with it the only account of a candidate that failed for a
  reason unrelated to the candidate.

Both use :meth:`~..infra.validation_profiles.ValidationGateContract.result_mismatch`,
the one predicate this codebase has for "did the contract that ran answer for
the contract now being asked about". That is what makes a shared history safe:
the quick gate runs again after every completion, and its receipt sits beside
the publication one without either being readable as the other.

The record file is looked up only so a surviving one can still be materialised
into a run directory. It is not authority and its absence is not a miss —
:class:`PriorEvaluation` says so by carrying ``record=None``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from ..domain.attempt import Attempt, AttemptKey
from ..domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from ..infra.validation_profiles import ValidationGateContract
from ..ports.attempt_store import AttemptStore
from ..ports.session_output import ValidationRecord
from .publication_verdict import receipt_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriorEvaluation:
    """A completed evaluation a gate may reuse, and its record if it survives.

    Two cache sources answer the same question and can now answer it with
    different amounts of evidence: the attempt-scoped source reads a durable
    receipt that outlives the worktree, the SHA-scoped source reads the record
    itself. Both produce this, so the decision code has one shape and ``record``
    being ``None`` means exactly "nothing left to materialise", never "no
    verdict".
    """

    passed: bool
    record: ValidationRecord | None


class CandidateEvaluations:
    """Reads and appends one contract's evaluations of one candidate."""

    def __init__(
        self,
        attempts: AttemptStore,
        key: AttemptKey,
        *,
        contract: ValidationGateContract,
        worktree: Path,
    ) -> None:
        self._attempts = attempts
        self._key = key
        self._contract = contract
        self._worktree = worktree

    @property
    def suite(self) -> str:
        return self._contract.suite

    def prior(self, head_sha: str) -> PriorEvaluation | None:
        """The latest durable evaluation of this contract for ``head_sha``."""
        attempt = self._attempts.for_key(self._key)
        receipt = None if attempt is None else self._reusable(attempt, head_sha)
        if attempt is None or receipt is None:
            logger.debug("%s: attempt cache miss for %s", self.suite, head_sha[:8])
            return None
        logger.debug("%s: attempt cache hit for %s", self.suite, head_sha[:8])
        return PriorEvaluation(
            passed=receipt.verdict is ValidationVerdict.PASSED,
            record=self._surviving_record(attempt, head_sha),
        )

    def file(
        self,
        record: ValidationRecord,
        record_path: Path,
        *,
        completed: bool,
    ) -> None:
        """Point the attempt at ``record``, appending it when it is a new verdict.

        ``completed`` says whether the gate just *reached* this verdict. A run
        that did appends its receipt, so the decision survives the worktree the
        record lives in; reusing an earlier evaluation appends nothing, because
        reuse is not a second completed evaluation and an append-only history
        that recorded it would grow one entry per lookup.

        One store write either way, so a reader can never see the pointer moved
        without the verdict it points at, or the reverse.
        """
        receipt = receipt_for(record) if completed else None

        def file_evidence(attempt: Attempt) -> Attempt:
            updated = replace(
                attempt, validation_record_path=str(record_path.resolve())
            )
            return updated if receipt is None else updated.with_completed_evaluation(
                receipt
            )

        self._attempts.update(self._key, file_evidence)

    # -- internals ---------------------------------------------------------

    def _reusable(
        self, attempt: Attempt, head_sha: str
    ) -> ValidationVerdictReceipt | None:
        for receipt in reversed(attempt.completed_evaluations):
            if not receipt.covers(head_sha):
                continue
            mismatch = self._contract.result_mismatch(
                suite=receipt.suite,
                command=receipt.command,
                profile=receipt.profile,
            )
            if mismatch is None:
                return receipt
            logger.debug(
                "%s: attempt cache miss for %s: %s mismatch "
                "(recorded suite=%s profile='%s', requested profile='%s')",
                self.suite,
                head_sha[:8],
                mismatch,
                receipt.suite,
                receipt.profile,
                self._contract.profile,
            )
        return None

    def _surviving_record(
        self, attempt: Attempt, head_sha: str
    ) -> ValidationRecord | None:
        """The full record behind a durable verdict, if it outlived its run.

        Best-effort by construction: absent, unreadable, or describing another
        contract all read the same way — there is nothing to materialise.
        """
        if not attempt.validation_record_path:
            return None
        record_path = self._resolve(attempt.validation_record_path)
        if not record_path.exists():
            return None
        record = self._read(record_path)
        if record is None:
            return None
        if record.head_sha != head_sha:
            return None
        if (
            self._contract.result_mismatch(
                suite=record.suite, command=record.command, profile=record.profile
            )
            is not None
        ):
            return None
        return record

    def _resolve(self, raw_path: str) -> Path:
        path = Path(raw_path)
        return path if path.is_absolute() else self._worktree / path

    @staticmethod
    def _read(path: Path) -> ValidationRecord | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                logger.warning("Validation cache record must be an object: %s", path)
                return None
            return ValidationRecord.from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("Failed to read validation cache record at %s: %s", path, exc)
            return None


__all__ = ["CandidateEvaluations", "PriorEvaluation"]
