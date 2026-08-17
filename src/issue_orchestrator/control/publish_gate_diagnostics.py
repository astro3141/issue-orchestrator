"""Keeping a failed publish gate's own explanation alive (#94).

#85 made the publish gate's *verdict* outlive the worktree. This is the other
half of the same loss: the verdict says A failed, and the only thing that said
*why* was ``validation-stdout.txt`` in the run directory — inside the coder
worktree, deleted by ordinary cleanup seconds after the verdict was reached.
Twice on #93 that happened, and the second failure does not reproduce in a
clean detached environment, so the deleted output was the only artefact that
could ever have explained it.

Two properties are the whole design, and both are reactions to how the evidence
was actually lost:

* **Durable destination from the start.** The output is written to the primary
  checkout at gate-execution time, from the bytes the runner already holds in
  memory, in the same step that writes them into the run directory. It is not
  copied out of the worktree afterwards. That copy is a race against cleanup,
  and the race has already been lost twice — including once by a human trying
  to win it by hand.
* **Bound to exactly one candidate.** The artefact is filed under
  :func:`~..domain.issue_key_codec.issue_key_path_part` plus the gate's own
  ``head_sha`` — the same identity ``Attempt(issue, A)`` is keyed by, in the
  same spelling — so a reader holding the verdict receipt can find the
  explanation, and an explanation can never be read as being about A′.

Diagnostic only, deliberately. Nothing here is readable by admission: the
artefact lives outside the attempt record, nothing points at it from the
attempt, and no predicate in this codebase takes its path or its existence as
input. ``Attempt.publication_verdict`` remains the sole authority on what the
gate decided (:mod:`.publication_evidence` is what reads it). An artefact that
could admit work would be a second authority, and a second authority written by
the losing side of a gate is the worst possible one.

The verdict fields it carries are not restated here either: they come from
:func:`~.publication_verdict.receipt_for`, the same projection that produces the
authoritative receipt. The explanation and the authority therefore cannot
disagree about which contract ran or what it decided — they are one value,
stored twice with different lifetimes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..domain.issue_key import IssueKey
from ..domain.issue_key_codec import encode_issue_key, issue_key_path_part
from ..domain.validation_verdict_receipt import ValidationVerdict
from ..infra.atomic_json import atomic_write_json
from ..ports.session_output import ValidationRecord
from .publication_verdict import receipt_for

logger = logging.getLogger(__name__)

PUBLISH_GATE_FAILURES_DIR = (
    Path(".issue-orchestrator") / "diagnostics" / "publish-gate-failures"
)
"""Where failed publish-gate output is kept, relative to the primary checkout.

Under the diagnostics directory that already exists in that root, beside the
``attempts/`` sidecars this artefact is named after. Not a new store: the same
owner, one drawer along.
"""

DIAGNOSTIC_FILE_NAME = "failure.json"
STDOUT_FILE_NAME = "stdout.log"
STDERR_FILE_NAME = "stderr.log"

PUBLISH_GATE_FAILURE_SCHEMA_VERSION = 1

_NON_AUTHORITATIVE_NOTE = (
    "Diagnostic evidence only. It explains a publish-gate failure and "
    "authorizes nothing; the authority on what the gate decided is the "
    "publication verdict receipt on the attempt record for this same "
    "(issue, head_sha)."
)


def needs_durable_diagnostic(record: ValidationRecord) -> bool:
    """Whether this gate run is one whose output must outlive the worktree.

    Asked of the verdict the domain derives, not of ``passed`` alone, so this
    agrees with the authoritative receipt about what a pass is: a timeout is
    not one however its truncated exit code happened to land
    (:meth:`~..domain.validation_verdict_receipt.ValidationVerdict.observed`).
    A run that passed needs no explanation — its output stays where every
    passing run's output has always stayed, and the PASS lane is untouched.
    """
    return receipt_for(record).verdict is not ValidationVerdict.PASSED


@dataclass(frozen=True, slots=True)
class GateFailureOutput:
    """A gate run's record together with the output it produced, in memory.

    The two travel as one value because the durable artefact is only meaningful
    as both: a record with no output is the receipt that already exists, and
    output with no record is bytes nobody can attribute to a contract. Held as
    text rather than as paths on purpose — a path into the run directory is
    precisely the evidence that stops resolving once the worktree is reaped.
    """

    record: ValidationRecord
    stdout: str
    stderr: str


class CandidateGateDiagnostics:
    """Files failed publish-gate output for one candidate's issue identity.

    Bound to the issue at construction and to the commit by the record it is
    handed, so no caller can supply an identity that disagrees with the run:
    the ``head_sha`` is the gate's own, exactly as
    :class:`~.publication_verdict.PublicationVerdictReceipts` takes it from the
    record rather than from a second read of the working copy.
    """

    def __init__(self, *, failures_dir: Path, issue_key: IssueKey) -> None:
        self._failures_dir = failures_dir
        self._issue_key = issue_key

    def record_failure(self, output: GateFailureOutput) -> Path | None:
        """Write ``output`` where it survives worktree cleanup.

        Returns the directory written, or ``None`` when the filesystem refused
        it. ``None`` is reported to the caller and logged loudly rather than
        raised: this is the losing side of a gate that has already failed, and
        turning an unwritable diagnostic into an exception would replace a
        diagnosable failure with an unhandled one — while still leaving no
        explanation behind. Nothing downstream branches on the return value.

        Raises:
            ValueError: when handed a run that passed. Writing a "failure"
                diagnostic for a pass would make the artefact's own name a lie,
                and the only way to get here is a caller that stopped asking
                :func:`needs_durable_diagnostic`.
        """
        receipt = receipt_for(output.record)
        if receipt.verdict is ValidationVerdict.PASSED:
            raise ValueError(
                "publish-gate failure diagnostics describe a failed run; "
                f"{receipt.suite} passed for {receipt.head_sha[:12]}"
            )
        destination = self._destination_for(receipt.head_sha)
        payload = {
            "schema_version": PUBLISH_GATE_FAILURE_SCHEMA_VERSION,
            "type": "publish_gate_failure",
            "authority": "diagnostic_only",
            "note": _NON_AUTHORITATIVE_NOTE,
            "issue_key": encode_issue_key(self._issue_key),
            # The gate's identity and decision, in the same shape the durable
            # receipt carries: suite, head_sha, verdict, command, profile.
            "verdict": receipt.to_payload(),
            "exit_code": output.record.exit_code,
            "timed_out": output.record.timed_out,
            "started_at": output.record.started_at,
            "ended_at": output.record.ended_at,
            "stdout_log": STDOUT_FILE_NAME,
            "stderr_log": STDERR_FILE_NAME,
            # Where the same output was written inside the run directory. Kept
            # for correlation with a session that still exists; it is expected
            # to be gone, which is why this artefact exists at all.
            "run_stdout_path": output.record.stdout_path,
            "run_stderr_path": output.record.stderr_path,
        }
        try:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / STDOUT_FILE_NAME).write_text(
                output.stdout, encoding="utf-8"
            )
            (destination / STDERR_FILE_NAME).write_text(
                output.stderr, encoding="utf-8"
            )
            atomic_write_json(destination / DIAGNOSTIC_FILE_NAME, payload)
        except OSError as exc:
            logger.error(
                "[PUBLISH_GATE_DIAGNOSTIC] could not write %s failure output "
                "for %s@%s to %s: %s — this failure will not be explainable "
                "after cleanup",
                receipt.suite,
                self._issue_key,
                receipt.head_sha[:12],
                destination,
                exc,
            )
            return None
        logger.info(
            "[PUBLISH_GATE_DIAGNOSTIC] kept %s %s output for %s@%s at %s",
            receipt.suite,
            receipt.verdict.value,
            self._issue_key,
            receipt.head_sha[:12],
            destination,
        )
        return destination

    def _destination_for(self, head_sha: str) -> Path:
        """One directory per gate run, named for the candidate it ran against.

        The candidate part is what makes the artefact findable from the
        receipt; the timestamp is what stops a re-run's explanation from
        erasing the previous one. A publish gate that failed twice on one
        candidate failed for two reasons worth keeping — and a cached failure
        is deliberately re-run rather than trusted, so two failures on one
        candidate is the ordinary shape of a retried publish. Sub-second
        precision because "the previous one is still there" must not depend on
        how long a gate happened to take.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        candidate = f"{issue_key_path_part(self._issue_key)}--{head_sha}"
        return self._failures_dir / f"{candidate}--{stamp}"


class PublishGateDiagnostics:
    """The durable destination for failed publish-gate output.

    Rooted in the primary checkout — the same root that holds the attempt
    sidecars — because that is what a coder worktree's removal does not touch.
    Composition only: it owns no policy beyond *where*, and hands out a
    candidate-bound writer for the *what*.
    """

    def __init__(self, repo_root: Path) -> None:
        self._failures_dir = repo_root / PUBLISH_GATE_FAILURES_DIR

    @property
    def failures_dir(self) -> Path:
        """The directory failed-gate artefacts are filed under."""
        return self._failures_dir

    def for_candidate(self, issue_key: IssueKey) -> CandidateGateDiagnostics:
        """A writer bound to ``issue_key``, for the commit its records name."""
        return CandidateGateDiagnostics(
            failures_dir=self._failures_dir, issue_key=issue_key
        )


__all__ = [
    "DIAGNOSTIC_FILE_NAME",
    "PUBLISH_GATE_FAILURES_DIR",
    "PUBLISH_GATE_FAILURE_SCHEMA_VERSION",
    "STDERR_FILE_NAME",
    "STDOUT_FILE_NAME",
    "CandidateGateDiagnostics",
    "GateFailureOutput",
    "PublishGateDiagnostics",
    "needs_durable_diagnostic",
]
