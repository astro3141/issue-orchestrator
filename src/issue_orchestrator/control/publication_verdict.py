"""What a gate run states about its candidate, as a durable receipt (#85).

One projection, used by every writer and reader of the evaluation history. A
receipt is built from the gate's own :class:`ValidationRecord` and nothing
else — in particular ``head_sha`` comes from the record rather than from a
second read of the working copy. A gate that validated A and a receipt keyed
from a HEAD re-read moments later could disagree, and the disagreement would
be invisible: the receipt would be filed under the wrong candidate and read as
evidence about it. One source, one candidate.

Filing the receipt is *not* here, and deliberately so. Until #159 there were
two writers of ``Attempt.completed_evaluations``: this module's
``PublicationVerdictReceipts``, for the publication gate, and
:class:`~.candidate_evaluations.CandidateEvaluations`, for every gate given an
attempt identity. Two writers of one append-only history is one writer too
many — the moment the publication gate gained an attempt identity so it could
*read* that history (#159), keeping its own writer would have appended every
completed publication verdict twice. So the gate consults and appends through
the one owner, and what remains here is the projection both ends share.
"""

from __future__ import annotations

from ..domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from ..ports.session_output import ValidationRecord


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


__all__ = ["receipt_for"]
