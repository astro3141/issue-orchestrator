"""Validation gates for the orchestrator.

- ``ValidationRunner`` executes a validation command and produces a record
- ``ValidationGate`` runs one contract of a profile, cache-aware

The agent-side gate is its own owner in :mod:`.agent_gate`: it runs
unconditionally, holds no cache and no attempt identity, and is run by the
completion command and by the continuation rather than by the orchestrator's
own pipeline. What both gates share is here — the runner above,
:func:`read_gate_head_sha` and :func:`gate_failure_reason`.

Every gate is constructed from a :class:`ValidationGateContract`, never from a
free-form command plus a separately chosen suite label. That pairing is what
allowed a record to read ``suite=publish_gate`` while executing the quick
selector (#25); with the contract as the single input, the suite a record
claims and the command it ran are two projections of one value.

A run's stdout and stderr are written into the session run directory, which
lives inside the worktree and dies with it. A gate given a
``failure_diagnostics`` destination therefore writes a *failed* run's output to
that durable destination too, at the moment it has the bytes in hand — see
:mod:`.gate_failure_diagnostics` for why it is not copied out afterwards (#94).

A gate given an attempt identity consults that candidate's durable evaluation
history rather than a path into the run directory (#139): the receipts live in
the primary checkout, so "this exact contract already decided about this exact
commit" stays answerable after the worktree is reaped. A completed run appends
its own verdict to that history; reuse appends nothing.

Record storage and cache-reuse rules live in
:mod:`.validation_record_cache`; ``ValidationRecordStore``,
``ValidationCache`` and ``VALIDATION_SCHEMA_VERSION`` are re-exported here so
existing importers keep working.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..domain.attempt import AttemptKey, CorruptAttemptEvidence
from ..domain.session_run import (
    VALIDATION_RECORD_NAME,
    VALIDATION_STDERR_NAME,
    VALIDATION_STDOUT_NAME,
    ValidationArtifactPaths,
)
from ..infra import validation_timings as timings
from ..infra.atomic_json import atomic_write_json
from ..infra.emit import emit_event
from ..infra.validation_profiles import (
    DEFAULT_VALIDATION_PROFILE,
    ValidationGateContract,
)
from ..ports import CommandRunner, CommandResult, WorkingCopy
from ..ports.attempt_store import AttemptStore
from ..ports.session_output import ValidationRecord
from .candidate_evaluations import CandidateEvaluations, PriorEvaluation
from .isolation import build_runtime_tool_env
from .gate_failure_diagnostics import (
    CandidateGateDiagnostics,
    GateFailureOutput,
    needs_durable_diagnostic,
)
from .validation_record_cache import (
    VALIDATION_SCHEMA_VERSION as VALIDATION_SCHEMA_VERSION,
    ValidationCache as ValidationCache,
    ValidationRecordStore as ValidationRecordStore,
)

logger = logging.getLogger(__name__)


def _normalize_head_sha(head_sha: str | None) -> str | None:
    if not head_sha:
        return None
    normalized = head_sha.strip().lower()
    return normalized or None


def read_gate_head_sha(working_copy: WorkingCopy, worktree: Path) -> str | None:
    """The commit a gate is about to judge, normalized.

    Shared by both gates rather than reimplemented per gate: what "the commit
    this record names" means must not depend on which gate wrote the record,
    since a downstream reader compares the two spellings against each other.

    Returns:
        The normalized SHA, or ``None`` when HEAD cannot be read — which every
        caller treats as a refusal, never as a run at an unknown commit.
    """
    head_sha = _normalize_head_sha(working_copy.get_head_sha(worktree))
    if not head_sha:
        logger.warning("Failed to get HEAD SHA in %s", worktree)
    return head_sha


def gate_failure_reason(record: ValidationRecord) -> str:
    """Human-facing reason for a failed gate run.

    Names the validation profile whenever it is not the default one, so a
    failure report says *which contract* rejected the work rather than
    leaving the reader to guess (#7059).
    """
    sha = record.head_sha[:8]
    suffix = (
        "" if record.profile == DEFAULT_VALIDATION_PROFILE
        else f" [profile={record.profile}]"
    )
    if record.timed_out:
        return f"Validation timed out for {sha}{suffix}"
    return f"Validation failed for {sha} (exit_code={record.exit_code}){suffix}"


def _is_session_run_dir(path: Path, worktree: Path) -> bool:
    """Return True when path is under .issue-orchestrator/sessions/ in this worktree."""
    try:
        rel = path.resolve().relative_to(worktree.resolve())
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) >= 3 and parts[:2] == (".issue-orchestrator", "sessions")


@dataclass
class ValidationResult:
    """Result of running a validation command."""

    exit_code: int
    passed: bool
    timed_out: bool
    stdout: str
    stderr: str
    started_at: datetime
    ended_at: datetime
    command: str


class ValidationRunner:
    """Runs validation commands and produces records."""

    def __init__(
        self,
        store: ValidationRecordStore,
        command_runner: CommandRunner,
        failure_diagnostics: CandidateGateDiagnostics | None = None,
    ):
        """Initialize runner with a record store.

        Args:
            store: Store for writing validation records
            command_runner: Adapter for running commands
            failure_diagnostics: Where a failed run's output is kept so it
                outlives the worktree (#94). ``None`` for a run whose CALLER
                holds no canonical issue identity to file under — an agent-side
                ``coding-done`` knows only its worktree, and prepush runs
                outside a managed candidate entirely. It is not a property of
                the gate KIND: the publication gate always supplies one, and so
                does any orchestrator-side caller holding the candidate, which
                since #173 includes an agent gate the continuation runs. That
                one needs it most — it destroys the checkout the moment its
                gate refuses, so a destination inside it would keep nothing.
        """
        self.store = store
        self.command_runner = command_runner
        self.failure_diagnostics = failure_diagnostics

    def run(
        self,
        suite: str,
        head_sha: str,
        command: str,
        timeout_seconds: int = 1800,
        cwd: Optional[Path] = None,
        session_output_dir: Optional[Path] = None,
        profile: str = DEFAULT_VALIDATION_PROFILE,
    ) -> ValidationRecord:
        """Run a validation command and return a record.

        Args:
            suite: The validation suite name (e.g., "publish_gate")
            head_sha: The HEAD SHA to record
            command: The command to run
            timeout_seconds: Timeout in seconds
            cwd: Working directory (defaults to store's worktree)
            session_output_dir: Directory to write stdout/stderr (required)
            profile: Named validation profile this run executed (#7059)

        Returns:
            ValidationRecord with results

        Raises:
            ValueError: If session_output_dir is not provided
        """
        if session_output_dir is None:
            raise ValueError("session_output_dir is required")
        cwd = cwd or self.store.worktree
        started_at = datetime.now(timezone.utc)

        logger.info("Running validation suite '%s': %s", suite, command)

        # Emit validation started event
        emit_event(
            "validation.started",
            {
                "suite": suite,
                "sha": head_sha,
                "command": command,
                "timeout_seconds": timeout_seconds,
                "profile": profile,
            },
        )

        try:
            result = self.command_runner.run(
                command,
                shell=True,
                cwd=cwd,
                env=build_runtime_tool_env(self.store.worktree),
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Validation command runner failed")
            result = CommandResult(
                returncode=-1,
                stdout="",
                stderr=f"Validation runner error: {exc}",
                timed_out=False,
            )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        timed_out = result.timed_out
        if timed_out:
            stderr += f"\n\n[TIMEOUT after {timeout_seconds}s]"
            logger.warning("Validation command timed out after %ds", timeout_seconds)

        ended_at = datetime.now(timezone.utc)
        passed = exit_code == 0

        # Write stdout/stderr files to session output dir
        session_output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = session_output_dir / VALIDATION_STDOUT_NAME
        stderr_path = session_output_dir / VALIDATION_STDERR_NAME
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        logger.debug("Wrote validation output to session dir: %s", session_output_dir)

        # Store paths - relative to worktree if possible, otherwise absolute
        # (prepush_check uses a temp dir outside the worktree)
        try:
            stdout_path_str = str(stdout_path.relative_to(self.store.worktree))
            stderr_path_str = str(stderr_path.relative_to(self.store.worktree))
        except ValueError:
            # Output dir is not under worktree (e.g., prepush temp dir)
            stdout_path_str = str(stdout_path)
            stderr_path_str = str(stderr_path)

        # Create record
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite=suite,
            head_sha=head_sha,
            passed=passed,
            exit_code=exit_code,
            command=command,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            timed_out=timed_out,
            stdout_path=stdout_path_str,
            stderr_path=stderr_path_str,
            profile=profile,
        )

        # A failure's output is written to its durable destination here, from
        # the same in-memory bytes the run directory just received — not copied
        # out of the worktree later (#94). Every path in the record above dies
        # with the worktree, and cleanup has twice won that race.
        self._keep_failure_output(record, stdout, stderr)

        # Write record
        self.store.write(record)
        # Persist run-scoped validation record only for real session run dirs.
        if _is_session_run_dir(session_output_dir, self.store.worktree):
            atomic_write_json(
                session_output_dir / VALIDATION_RECORD_NAME,
                record.to_dict(),
            )

        logger.info(
            "Validation suite '%s' [profile=%s] %s (exit_code=%d)",
            suite,
            profile,
            "passed" if passed else "failed",
            exit_code,
        )

        # Emit validation completed event
        duration_seconds = (ended_at - started_at).total_seconds()
        emit_event(
            "validation.completed",
            {
                "suite": suite,
                "sha": head_sha,
                "passed": passed,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_seconds": duration_seconds,
                "profile": profile,
            },
        )
        timings.record_gate_timings(suite, self.store.worktree, command, stdout, stderr)

        return record

    def _keep_failure_output(
        self,
        record: ValidationRecord,
        stdout: str,
        stderr: str,
    ) -> None:
        """Hand a failed run's output to the durable destination, if there is one."""
        if self.failure_diagnostics is None:
            return
        if not needs_durable_diagnostic(record):
            return
        self.failure_diagnostics.record_failure(
            GateFailureOutput(record=record, stdout=stdout, stderr=stderr)
        )


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """A gate's validation record together with the paths that gate wrote.

    The two travel as one value because they are only meaningful together:
    the publish contract writes into ``publish-gate/`` while the quick
    contract writes into the run root, so a record attached beside another
    gate's stdout and stderr describes a run that never happened (#25). A
    caller cannot pair a record with some other gate's paths without
    saying so out loud.
    """

    record: ValidationRecord | None
    paths: ValidationArtifactPaths


@dataclass
class PublishGateResult:
    """Result of a validation gate check."""

    allowed: bool
    reason: str
    record: Optional[ValidationRecord] = None
    cache_hit: bool = False


class ValidationGate:
    """Runs one contract of a validation profile, cache-aware.

    Combines cache lookup and runner to provide a single check method. The
    contract decides everything the record will claim: which command runs,
    with which timeout, under which suite label and profile name.

    Used for both contracts. ``kind=PUBLISH`` is the orchestrator's
    pre-publication gate; ``kind=QUICK`` is the completion/rework gate. A
    caller cannot ask for one and execute the other, because there is no
    command parameter to disagree with the kind (#25).
    """

    def __init__(
        self,
        worktree: Path,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
        contract: ValidationGateContract,
        attempt_store: AttemptStore | None = None,
        attempt_key: AttemptKey | None = None,
        failure_diagnostics: CandidateGateDiagnostics | None = None,
    ):
        """Initialize a gate for a worktree and one profile contract.

        Args:
            worktree: Path to the git worktree
            contract: The profile contract this gate executes. An unconfigured
                contract (no ``cmd``) means the gate is disabled.
            attempt_store: Attempt-scoped cache store. When provided with
                attempt_key, validation cache hits are scoped by issue identity
                plus HEAD SHA rather than by SHA alone.
            attempt_key: Stable issue-at-HEAD identity for cache lookup.
            failure_diagnostics: Durable destination for a failed run's output
                (#94), passed straight through to the runner that produces it.
                The gate does not consult it: an artefact this gate wrote is
                evidence for a human, never an input to a later decision.
        """
        if attempt_key is not None and attempt_store is None:
            raise ValueError("attempt_key requires attempt_store")
        self.worktree = worktree
        self.command_runner = command_runner
        self.working_copy = working_copy
        self.contract = contract
        self.attempt_store = attempt_store
        self.attempt_key = attempt_key
        # The candidate's durable evaluation history, as *this* contract sees
        # it (#139). Built once, here, so the gate never reaches into attempt
        # internals: it asks one owner what was already decided and tells the
        # same owner what it decided.
        self.evaluations = (
            None
            if attempt_store is None or attempt_key is None
            else CandidateEvaluations(
                attempt_store, attempt_key, contract=contract, worktree=worktree
            )
        )
        self.store = ValidationRecordStore(worktree, contract.kind)
        self.cache = ValidationCache(self.store)
        self.runner = ValidationRunner(
            self.store, command_runner, failure_diagnostics=failure_diagnostics
        )

    @property
    def suite(self) -> str:
        """Suite label records from this gate carry."""
        return self.contract.suite

    @property
    def command(self) -> str | None:
        """The command this gate runs; ``None`` when the gate is disabled."""
        return self.contract.cmd

    @property
    def timeout_seconds(self) -> int:
        return self.contract.timeout_seconds

    @property
    def profile(self) -> str:
        return self.contract.profile

    def _record_summary(
        self,
        *,
        wall_started_at: datetime,
        monotonic_started_at: float,
        head_sha: str | None,
        cache_lookup: str,
        result: PublishGateResult,
    ) -> None:
        """Append an outer publish-gate timing record."""
        record = result.record
        payload: dict[str, object] = {
            "kind": "validation_gate_summary",
            "gate": self.suite,
            "profile": self.profile,
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "head_sha": head_sha,
            "cache_lookup": cache_lookup,
            "cache_hit": result.cache_hit,
            "allowed": result.allowed,
            "reason": result.reason,
            "record_passed": record.passed if record else None,
            "record_exit_code": record.exit_code if record else None,
            "record_timed_out": record.timed_out if record else None,
        }
        payload.update(
            timings.build_timing_envelope(
                wall_started_at=wall_started_at,
                monotonic_started_at=monotonic_started_at,
            )
        )
        timings.append_validation_timing(self.worktree, payload)

    def _validate_attempt_key_head(self, head_sha: str) -> None:
        if self.attempt_key is None:
            return
        if self.attempt_key.head_sha != head_sha:
            raise ValueError(
                "attempt_key.head_sha must match the current validation HEAD"
            )

    def _record_matches_request(
        self,
        record: ValidationRecord,
        *,
        head_sha: str,
    ) -> bool:
        """Whether a record found in the SHA-scoped store answers this request.

        The attempt-scoped source asks the same question of a durable receipt
        through :class:`~.candidate_evaluations.CandidateEvaluations`, and both
        end at ``result_mismatch`` below, so the two cache sources cannot drift
        about which contract a stored result belongs to.
        """
        if record.schema_version != VALIDATION_SCHEMA_VERSION:
            logger.debug(
                "%s: sha cache miss for %s: schema version mismatch (%d != %d)",
                self.suite,
                head_sha[:8],
                record.schema_version,
                VALIDATION_SCHEMA_VERSION,
            )
            return False
        if record.head_sha != head_sha:
            logger.debug(
                "%s: sha cache miss for %s: record SHA mismatch (%s)",
                self.suite,
                head_sha[:8],
                record.head_sha[:8],
            )
            return False
        # The contract the cached run executed, not the caller that produced
        # it: the agent gate runs the same quick contract as this gate, so its
        # record is reusable, while a record from the *other* contract never
        # is — that reuse is exactly how a quick result could satisfy a
        # publish request (#25). Asked of the contract itself so review
        # admission, which asks the same question of a durable receipt, cannot
        # answer it differently (#45).
        mismatch = self.contract.result_mismatch(
            suite=record.suite,
            command=record.command,
            profile=record.profile,
        )
        if mismatch is not None:
            logger.debug(
                "%s: sha cache miss for %s: %s mismatch "
                "(cached suite=%s profile='%s', requested profile='%s')",
                self.suite,
                head_sha[:8],
                mismatch,
                record.suite,
                record.profile,
                self.profile,
            )
            return False
        return True

    def _materialize_cached_record(
        self,
        record: ValidationRecord,
        session_output_dir: Path | None,
    ) -> None:
        if session_output_dir is None or not _is_session_run_dir(
            session_output_dir, self.store.worktree
        ):
            return
        atomic_write_json(
            session_output_dir / VALIDATION_RECORD_NAME,
            record.to_dict(),
        )

    def _attempt_record_path_for(
        self,
        record: ValidationRecord,
        session_output_dir: Path | None,
    ) -> Path:
        if session_output_dir is not None and _is_session_run_dir(
            session_output_dir, self.store.worktree
        ):
            return session_output_dir / VALIDATION_RECORD_NAME
        return self.store.get_record_path(record.head_sha)

    def _file_evaluation(
        self,
        record: ValidationRecord,
        session_output_dir: Path | None,
        *,
        completed: bool,
    ) -> None:
        """Hand this run's result to the candidate's evaluation history (#139)."""
        if self.evaluations is None:
            return
        self.evaluations.file(
            record,
            self._attempt_record_path_for(record, session_output_dir),
            completed=completed,
        )

    def check(self, session_output_dir: Optional[Path] = None) -> PublishGateResult:
        """Decide this candidate, refusing rather than crashing on damage (#378).

        The containment boundary for corrupt durable evidence, and it is here
        rather than at either caller because BOTH gates consult and append to
        the candidate's evaluation history: a rule enforced only by the
        publication gate would leave the quick gate raising through the same
        door on the next pass.

        A damaged record refuses THIS candidate and nothing else. It is never
        read as a cache miss (which would silently re-run and then re-file
        against a record the store cannot rewrite), never as a pass, and never
        as an identity to synthesize — the three readings #378 forbids. The
        refusal carries the store's own attribution, so the operator reading a
        gate failure learns which file, which attempt and why.
        """
        try:
            return self._check(session_output_dir)
        except CorruptAttemptEvidence as exc:
            logger.error(
                "%s: refusing %s — durable evidence is corrupt: %s",
                self.suite,
                exc.attempt_ref,
                exc,
            )
            return PublishGateResult(
                allowed=False,
                reason=(
                    f"{self.suite} cannot decide {exc.attempt_ref}: its durable "
                    f"attempt evidence at {exc.path} is corrupt ({exc.reason}). "
                    "Corrupt evidence is not an absent gate result, so this "
                    "candidate cannot be published on it."
                ),
            )

    def _check(self, session_output_dir: Optional[Path]) -> PublishGateResult:
        """Check if publishing is allowed.

        This method:
        1. Returns allowed=True if no command is configured (gate disabled)
        2. Gets the current HEAD SHA
        3. Checks cache for existing passing result
        4. Runs validation if no cache hit
        5. Returns the result

        Args:
            session_output_dir: If provided, write validation output directly here
                instead of validation/output/. Keeps all session artifacts together.

        Returns:
            PublishGateResult with allowed status and reason
        """
        wall_started_at = datetime.now(timezone.utc)
        monotonic_started_at = time.monotonic()
        head_sha: str | None = None
        cache_lookup = "not_checked"

        def finish(result: PublishGateResult) -> PublishGateResult:
            self._record_summary(
                wall_started_at=wall_started_at,
                monotonic_started_at=monotonic_started_at,
                head_sha=head_sha,
                cache_lookup=cache_lookup,
                result=result,
            )
            return result

        # Gate disabled if no command
        command = self.command
        if not command:
            logger.debug("%s disabled (no command configured)", self.suite)
            cache_lookup = "disabled"
            return finish(
                PublishGateResult(
                    allowed=True,
                    reason=f"{self.suite} disabled (no command configured)",
                )
            )

        # Get HEAD SHA
        head_sha = read_gate_head_sha(self.working_copy, self.worktree)
        if not head_sha:
            cache_lookup = "head_sha_missing"
            return finish(
                PublishGateResult(
                    allowed=False,
                    reason="Cannot determine HEAD SHA",
                )
            )
        self._validate_attempt_key_head(head_sha)

        # Check cache - only trust cached passes, not failures
        # Failures might be due to flaky tests or transient issues, so always re-run
        if self.evaluations is not None:
            cached = self.evaluations.prior(head_sha)
            cache_hit_prefix = "attempt_"
        else:
            record_cached = self.cache.lookup(head_sha, command, self.profile)
            # The store is already contract-scoped, so a record found here was
            # written under this contract. Re-checking is deliberate: it keeps
            # one predicate answering "may this record satisfy this request"
            # for both cache sources, rather than two rules that can drift.
            if record_cached is not None and not self._record_matches_request(
                record_cached, head_sha=head_sha
            ):
                record_cached = None
            cached = (
                None
                if record_cached is None
                else PriorEvaluation(
                    passed=record_cached.passed, record=record_cached
                )
            )
            cache_hit_prefix = ""
        if cached is not None and cached.passed:
            cache_lookup = f"{cache_hit_prefix}hit_passed"
            logger.info("%s: cache hit (passed) for %s", self.suite, head_sha[:8])
            # Materialize the cached record into the session run dir so
            # downstream consumers (manifest, review-exchange predicate, UI)
            # see the gate's authoritative result. Without this, a stale
            # ``validation-record.json`` from an earlier inline run remains
            # in place and silently contradicts the cache hit. A durable
            # verdict whose record died with its worktree has nothing to
            # materialise, and says so by carrying no record rather than by
            # reading as a miss (#139).
            if cached.record is not None:
                self._materialize_cached_record(cached.record, session_output_dir)
                self._file_evaluation(
                    cached.record, session_output_dir, completed=False
                )
            return finish(
                PublishGateResult(
                    allowed=True,
                    reason=f"Cached validation passed for {head_sha[:8]}",
                    record=cached.record,
                    cache_hit=True,
                )
            )
        elif cached is not None:
            cache_lookup = f"{cache_hit_prefix}hit_failed_rerun"
            # Cached failure - log it but re-run validation
            logger.info(
                "%s: cached failure for %s, re-running validation",
                self.suite,
                head_sha[:8],
            )
        else:
            cache_lookup = f"{cache_hit_prefix}miss"

        # Run validation
        logger.info(
            "%s: running validation for %s [profile=%s]",
            self.suite,
            head_sha[:8],
            self.profile,
        )
        record = self.runner.run(
            suite=self.suite,
            head_sha=head_sha,
            command=command,
            timeout_seconds=self.timeout_seconds,
            session_output_dir=session_output_dir,
            profile=self.profile,
        )
        # ValidationRunner still populates the legacy SHA cache for callers
        # without attempt identity. When attempt_key is present, the attempt
        # sidecar below is the authoritative cross-run cache record — and this
        # run just reached a verdict, so it is appended to the candidate's
        # evaluation history (#139).
        self._file_evaluation(record, session_output_dir, completed=True)

        if record.passed:
            return finish(
                PublishGateResult(
                    allowed=True,
                    reason=f"Validation passed for {head_sha[:8]}",
                    record=record,
                    cache_hit=False,
                )
            )
        else:
            return finish(
                PublishGateResult(
                    allowed=False,
                    reason=gate_failure_reason(record),
                    record=record,
                    cache_hit=False,
                )
            )

