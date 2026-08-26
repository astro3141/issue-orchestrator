"""Keeping a failed gate's own explanation alive (#94).

#85 made the publish gate's *verdict* outlive the worktree. This is the other
half of the same loss: the verdict says A failed, and the only thing that said
*why* was ``validation-stdout.txt`` in the run directory — inside the coder
worktree, deleted by ordinary cleanup seconds after the verdict was reached.
Twice on #93 that happened, and the second failure does not reproduce in a
clean detached environment, so the deleted output was the only artefact that
could ever have explained it.

The publish gate was the first caller and is no longer the only one. Any gate
run whose output would otherwise die with the checkout that produced it files
here, under the suite it actually stamped — the continuation's quick-validation
preparation is the second (#173), and it destroys its checkout *immediately* on
a refusal rather than losing a race with cleanup. So nothing here is scoped to
one contract: the suite comes from the gate's own record, which is the only
place that knows what ran.

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
  explanation, and an explanation can never be read as being about A′. The
  suite the gate stamped completes the name, because two contracts may both
  fail for one candidate and neither explanation may erase the other.

Diagnostic, never authority. Nothing points at the artefact from the attempt
record, and no predicate anywhere reads it to decide what a gate DECIDED.
``Attempt.completed_evaluations`` remains the sole authority on that
(:mod:`.publication_evidence` is what reads it). An artefact that could admit
work would be a second authority, and a second authority written by the losing
side of a gate is the worst possible one. That holds a fortiori for a gate that
files no receipt at all, such as the continuation's: its failure is explained
here and admits nothing anywhere.

Which is why the one reader that exists (#297, :meth:`CandidateGateDiagnostics
.latest_failure`) is safe, and why it is spelled the way it is. The continuation
hands a failed publish candidate back to ordinary rework, and the correction
agent must be told the actual failing test — the receipt carries the verdict and
the command, and the only place the *output* survives worktree cleanup is here.
That read is **monotone in the refusing direction**: finding a bundle authorizes
nothing that was not already authorized by the receipt and the phase, and
failing to find one can only REFUSE a handoff that everything else admitted. So
a hand-planted artefact cannot make work happen, and a deleted one cannot make
work happen either — it strands the candidate for a human, loudly, which is the
fail-closed direction. The reader re-checks the bundle's own receipt against the
candidate and the suite it was asked for, so a bundle filed under one name that
describes another explains nothing rather than explaining the wrong candidate.

The verdict fields it carries are not restated here either: they come from
:func:`~.publication_verdict.receipt_for`, the same projection that produces the
authoritative receipt. The explanation and the authority therefore cannot
disagree about which contract ran or what it decided — they are one value,
stored twice with different lifetimes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..domain.commit_sha import normalize_commit_sha
from ..domain.issue_key import IssueKey
from ..domain.issue_key_codec import encode_issue_key, issue_key_path_part
from ..domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from ..infra.atomic_json import atomic_write_json
from ..ports.session_output import ValidationRecord
from .publication_verdict import receipt_for

logger = logging.getLogger(__name__)

GATE_FAILURES_DIR = (
    Path(".issue-orchestrator") / "diagnostics" / "gate-failures"
)
"""Where failed gate output is kept, relative to the primary checkout.

Under the diagnostics directory that already exists in that root, beside the
``attempts/`` sidecars this artefact is named after. Not a new store: the same
owner, one drawer along. One drawer for every suite rather than one per gate,
because the question a reader arrives with is "why did *this candidate* fail",
and an answer split across a directory per contract is one a reader has to know
the contract to find.
"""

DIAGNOSTIC_FILE_NAME = "failure.json"
STDOUT_FILE_NAME = "stdout.log"
STDERR_FILE_NAME = "stderr.log"

GATE_FAILURE_SCHEMA_VERSION = 1

FAILURE_LOG_TAIL_BYTES = 8_192
"""How much of each stream a reader takes when the whole log is larger.

A publish contract's stdout is routinely megabytes, and the reader's one caller
puts what it gets into an agent's prompt. The TAIL is the part that explains the
failure — a test runner names what failed at the end — and the durable path
travels with it, so an agent that needs more has somewhere to look rather than a
truncation it cannot get past. The full log is never rewritten or trimmed: the
bound is on the read, not on what #94 keeps.
"""

_NON_AUTHORITATIVE_NOTE = (
    "Diagnostic evidence only. It explains a gate failure and authorizes "
    "nothing; the authority on what a gate decided is the validation verdict "
    "receipt on the attempt record for this same (issue, head_sha), and a "
    "gate that files no receipt has decided nothing durable at all."
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


@dataclass(frozen=True, slots=True)
class GateFailureLog:
    """One stream of a failed gate's output: where it lives, and its tail.

    Both halves, because a reader needs both for different things. The tail is
    what can be shown to somebody who has to act on the failure now; the path is
    what makes the tail checkable and the rest of the log reachable. A value
    carrying only the text would be evidence nobody could go back to, and one
    carrying only the path would be the session-local pointer this whole store
    exists because of.
    """

    path: Path
    tail: str
    truncated: bool

    @property
    def has_output(self) -> bool:
        """Whether this stream said anything at all."""
        return bool(self.tail.strip())


@dataclass(frozen=True, slots=True)
class DurableGateFailure:
    """A failed gate's own explanation, resolved after its worktree is gone.

    The read side of what :meth:`CandidateGateDiagnostics.record_failure` wrote,
    and deliberately the same shape: the gate's receipt for what it decided, and
    the output for why. The receipt here is re-parsed from the bundle rather
    than taken from the caller, so a bundle can only ever describe the run that
    produced it — and :meth:`CandidateGateDiagnostics.latest_failure` refuses to
    return one whose receipt names a candidate or a contract other than the one
    asked for.
    """

    directory: Path
    receipt: ValidationVerdictReceipt
    exit_code: int | None
    timed_out: bool
    stdout: GateFailureLog
    stderr: GateFailureLog

    @property
    def explains_the_failure(self) -> bool:
        """Whether this bundle actually carries the failure's output.

        A bundle whose two streams are both empty repeats the receipt and adds
        nothing — the reader's caller must treat it as no evidence rather than
        hand an agent a prompt that says "it failed" and stops.
        """
        return self.stdout.has_output or self.stderr.has_output


class CandidateGateDiagnostics:
    """Files, and resolves, failed gate output for one candidate's issue identity.

    Bound to the issue at construction and to the commit by the record it is
    handed, so no caller can supply an identity that disagrees with the run:
    the ``head_sha`` is the gate's own, exactly as
    :func:`~.publication_verdict.receipt_for` takes it from the record rather
    than from a second read of the working copy.

    Reading is on the same type as writing because the binding rule is the same
    one: an artefact is findable only under the identity it was filed under, and
    a reader that spelled that identity itself would be a second spelling of it.
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
                "gate failure diagnostics describe a failed run; "
                f"{receipt.suite} passed for {receipt.head_sha[:12]}"
            )
        destination = self._destination_for(receipt.head_sha, receipt.suite)
        payload = {
            "schema_version": GATE_FAILURE_SCHEMA_VERSION,
            # The suite the gate stamped, not a constant chosen by whoever
            # wired the destination: a diagnostic that named a contract other
            # than the one that ran would be the mislabelling #25 removed from
            # the records themselves.
            "type": f"{receipt.suite}_failure",
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
                "[GATE_DIAGNOSTIC] could not write %s failure output "
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
            "[GATE_DIAGNOSTIC] kept %s %s output for %s@%s at %s",
            receipt.suite,
            receipt.verdict.value,
            self._issue_key,
            receipt.head_sha[:12],
            destination,
        )
        return destination

    def _destination_for(self, head_sha: str, suite: str) -> Path:
        """One directory per gate run, named for the candidate it ran against.

        The candidate part is what makes the artefact findable from the
        receipt; the suite says which contract this explanation is about, so a
        candidate whose quick contract and publish contract both failed keeps
        both accounts; and the timestamp is what stops a re-run's explanation
        from erasing the previous one. A gate that failed twice on one
        candidate failed for two reasons worth keeping — and a cached failure
        is deliberately re-run rather than trusted, so two failures on one
        candidate is the ordinary shape of a retried publish. Sub-second
        precision because "the previous one is still there" must not depend on
        how long a gate happened to take.

        The candidate parts stay leftmost so one prefix match still finds
        everything filed for ``(issue, A)``, whatever ran.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        return self._failures_dir / f"{self._prefix_for(head_sha, suite)}{stamp}"

    def latest_failure(
        self, *, head_sha: str, suite: str
    ) -> DurableGateFailure | None:
        """The newest surviving explanation of ``suite`` refusing ``head_sha``.

        Found by name and by nothing else: there is no pointer from the attempt
        to follow, and adding one is what would turn this store into a second
        authority. The name is built by :meth:`_prefix_for`, which is the same
        expression :meth:`_destination_for` writes under, so the writer and the
        reader cannot drift into two spellings of one candidate.

        Newest first because a retried publish files one bundle per attempt and
        the last one is the failure the candidate is actually sitting on. Older
        bundles are not skipped over silently, though: a newest bundle that
        cannot be read falls through to the one before it, so an unreadable
        artefact costs the caller detail rather than all of its evidence.

        Returns ``None`` when nothing filed under that name can be read as being
        about this exact candidate and this exact contract. ``None`` is the
        honest answer for "no explanation survives", and the caller's own
        fail-closed behaviour is built on its being answered rather than
        approximated: see :mod:`.continuation_rework_handoff`.
        """
        prefix = self._prefix_for(head_sha, suite)
        try:
            bundles = sorted(
                (
                    path
                    for path in self._failures_dir.iterdir()
                    if path.is_dir() and path.name.startswith(prefix)
                ),
                key=lambda path: path.name,
                reverse=True,
            )
        except OSError:
            # No store yet, or one this process cannot list. Either way nothing
            # is resolvable, which is what the caller is asking.
            return None
        for directory in bundles:
            failure = _read_failure(directory, head_sha=head_sha, suite=suite)
            if failure is not None:
                return failure
        return None

    def _prefix_for(self, head_sha: str, suite: str) -> str:
        """The one name a ``(candidate, suite)`` bundle is filed under.

        The commit is canonicalised on the way in, because the writer's is: it
        comes from a receipt, and ``ValidationVerdictReceipt`` normalises case.
        A reader handed an upper-case SHA is naming the same commit and must
        find the same bundle.
        """
        commit = normalize_commit_sha(head_sha, field_name="head_sha")
        candidate = f"{issue_key_path_part(self._issue_key)}--{commit}"
        return f"{candidate}--{suite}--"


class GateFailureDiagnostics:
    """The durable destination for failed gate output.

    Rooted in the primary checkout — the same root that holds the attempt
    sidecars — because that is what a coder worktree's removal does not touch.
    Composition only: it owns no policy beyond *where*, and hands out a
    candidate-bound writer for the *what*.
    """

    def __init__(self, repo_root: Path) -> None:
        self._failures_dir = repo_root / GATE_FAILURES_DIR

    @property
    def failures_dir(self) -> Path:
        """The directory failed-gate artefacts are filed under."""
        return self._failures_dir

    def for_candidate(self, issue_key: IssueKey) -> CandidateGateDiagnostics:
        """A reader/writer bound to ``issue_key``, for the commit its records name."""
        return CandidateGateDiagnostics(
            failures_dir=self._failures_dir, issue_key=issue_key
        )


def _read_failure(
    directory: Path, *, head_sha: str, suite: str
) -> DurableGateFailure | None:
    """One bundle, read back and checked against what the caller asked for.

    Every refusal below returns ``None`` and says so in the log rather than
    raising. The caller is resolving evidence for a candidate that has already
    failed, and an unreadable artefact is one more thing that failed — not a
    reason to take down the reconciliation pass that asked.
    """
    try:
        payload = json.loads(
            (directory / DIAGNOSTIC_FILE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "[GATE_DIAGNOSTIC] %s is not a readable failure bundle: %s",
            directory,
            exc,
        )
        return None
    if not isinstance(payload, Mapping):
        logger.warning("[GATE_DIAGNOSTIC] %s holds no failure object", directory)
        return None
    verdict = payload.get("verdict")
    if not isinstance(verdict, Mapping):
        logger.warning("[GATE_DIAGNOSTIC] %s records no verdict", directory)
        return None
    try:
        receipt = ValidationVerdictReceipt.from_payload(verdict)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[GATE_DIAGNOSTIC] %s records an unreadable verdict: %s", directory, exc
        )
        return None
    if receipt.suite != suite or not receipt.covers(head_sha):
        # The name said one thing and the payload says another. Returning it
        # would be the exact mislabelling the candidate binding exists to make
        # impossible, one layer up: an explanation read as being about A′.
        logger.warning(
            "[GATE_DIAGNOSTIC] %s describes %s@%s, not %s@%s",
            directory,
            receipt.suite,
            receipt.head_sha[:12],
            suite,
            head_sha[:12],
        )
        return None
    exit_code = payload.get("exit_code")
    return DurableGateFailure(
        directory=directory,
        receipt=receipt,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        timed_out=payload.get("timed_out") is True,
        # The file names are the store's own constants rather than the payload's
        # ``stdout_log``/``stderr_log`` strings: those exist to tell a human
        # reader what to open, and following them would let a bundle name a path
        # outside itself.
        stdout=_read_log(directory / STDOUT_FILE_NAME),
        stderr=_read_log(directory / STDERR_FILE_NAME),
    )


def _read_log(path: Path) -> GateFailureLog:
    """The tail of one stream, bounded by :data:`FAILURE_LOG_TAIL_BYTES`.

    Seeks rather than reading the whole file, because a publish contract's
    stdout is routinely large and this runs inside a reconciliation pass. A
    stream that cannot be read at all reports itself as empty and keeps its
    path: the bundle's other stream may still explain the failure, and the
    caller decides what "no output at all" means.
    """
    truncated = False
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > FAILURE_LOG_TAIL_BYTES:
                handle.seek(size - FAILURE_LOG_TAIL_BYTES)
                truncated = True
            raw = handle.read()
    except OSError as exc:
        logger.warning("[GATE_DIAGNOSTIC] could not read %s: %s", path, exc)
        return GateFailureLog(path=path, tail="", truncated=False)
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        # The seek landed mid-line and mid-character. Dropping through the first
        # newline costs one line and removes both, so what is shown is text the
        # log actually contains rather than a mangled prefix of it.
        _, newline, remainder = text.partition("\n")
        if newline:
            text = remainder
    return GateFailureLog(path=path, tail=text, truncated=truncated)


__all__ = [
    "DIAGNOSTIC_FILE_NAME",
    "FAILURE_LOG_TAIL_BYTES",
    "GATE_FAILURES_DIR",
    "GATE_FAILURE_SCHEMA_VERSION",
    "STDERR_FILE_NAME",
    "STDOUT_FILE_NAME",
    "CandidateGateDiagnostics",
    "DurableGateFailure",
    "GateFailureDiagnostics",
    "GateFailureLog",
    "GateFailureOutput",
    "needs_durable_diagnostic",
]
