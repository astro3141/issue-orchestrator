"""Validation gates for the orchestrator.

- ``ValidationRunner`` executes a validation command and produces a record
- ``ValidationGate`` runs one contract of a profile, cache-aware
- ``AgentGate`` runs the quick contract at agent completion

Every gate is constructed from a :class:`ValidationGateContract`, never from a
free-form command plus a separately chosen suite label. That pairing is what
allowed a record to read ``suite=publish_gate`` while executing the quick
selector (#25); with the contract as the single input, the suite a record
claims and the command it ran are two projections of one value.

Record storage and cache-reuse rules live in
:mod:`.validation_record_cache`; ``ValidationRecordStore``,
``ValidationCache`` and ``VALIDATION_SCHEMA_VERSION`` are re-exported here so
existing importers keep working.
"""

import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..domain.attempt import AttemptKey
from ..domain.session_run import (
    VALIDATION_RECORD_NAME,
    VALIDATION_STDERR_NAME,
    VALIDATION_STDOUT_NAME,
    ValidationArtifactPaths,
)
from ..domain.validation_profile import AGENT_GATE_SUITE
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
from .isolation import build_runtime_tool_env
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


def _failure_reason(record: ValidationRecord) -> str:
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

    def __init__(self, store: ValidationRecordStore, command_runner: CommandRunner):
        """Initialize runner with a record store.

        Args:
            store: Store for writing validation records
            command_runner: Adapter for running commands
        """
        self.store = store
        self.command_runner = command_runner

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
        """
        if attempt_key is not None and attempt_store is None:
            raise ValueError("attempt_key requires attempt_store")
        self.worktree = worktree
        self.command_runner = command_runner
        self.working_copy = working_copy
        self.contract = contract
        self.attempt_store = attempt_store
        self.attempt_key = attempt_key
        self.store = ValidationRecordStore(worktree, contract.kind)
        self.cache = ValidationCache(self.store)
        self.runner = ValidationRunner(self.store, command_runner)

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

    def _get_head_sha(self) -> Optional[str]:
        """Get the current HEAD SHA."""
        head_sha = _normalize_head_sha(self.working_copy.get_head_sha(self.worktree))
        if not head_sha:
            logger.warning("Failed to get HEAD SHA in %s", self.worktree)
        return head_sha

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

    def _read_record_file(self, path: Path) -> ValidationRecord | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                logger.warning("Validation cache record must be an object: %s", path)
                return None
            return ValidationRecord.from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("Failed to read validation cache record at %s: %s", path, exc)
            return None

    def _resolve_attempt_validation_record_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return self.worktree / path

    def _record_matches_request(
        self,
        record: ValidationRecord,
        *,
        head_sha: str,
        cache_source: str,
    ) -> bool:
        if record.schema_version != VALIDATION_SCHEMA_VERSION:
            logger.debug(
                "%s: %s cache miss for %s: schema version mismatch (%d != %d)",
                self.suite,
                cache_source,
                head_sha[:8],
                record.schema_version,
                VALIDATION_SCHEMA_VERSION,
            )
            return False
        if record.head_sha != head_sha:
            logger.debug(
                "%s: %s cache miss for %s: record SHA mismatch (%s)",
                self.suite,
                cache_source,
                head_sha[:8],
                record.head_sha[:8],
            )
            return False
        # The contract the cached run executed, not the caller that produced
        # it: the agent gate runs the same quick contract as this gate, so its
        # record is reusable, while a record from the *other* contract never
        # is — that reuse is exactly how a quick result could satisfy a
        # publish request (#25).
        if not self.contract.kind.produced(record.suite):
            logger.debug(
                "%s: %s cache miss for %s: contract mismatch (cached suite=%s)",
                self.suite,
                cache_source,
                head_sha[:8],
                record.suite,
            )
            return False
        if self.command and record.command != self.command:
            logger.debug(
                "%s: %s cache miss for %s: command mismatch",
                self.suite,
                cache_source,
                head_sha[:8],
            )
            return False
        if record.profile != self.profile:
            logger.debug(
                "%s: %s cache miss for %s: profile mismatch "
                "(cached='%s', requested='%s')",
                self.suite,
                cache_source,
                head_sha[:8],
                record.profile,
                self.profile,
            )
            return False
        return True

    def _attempt_cached_record(self, head_sha: str) -> ValidationRecord | None:
        if self.attempt_store is None or self.attempt_key is None:
            return None
        attempt = self.attempt_store.for_key(self.attempt_key)
        if attempt is None or not attempt.validation_record_path:
            logger.debug("%s: attempt cache miss for %s", self.suite, head_sha[:8])
            return None
        record_path = self._resolve_attempt_validation_record_path(
            attempt.validation_record_path
        )
        if not record_path.exists():
            logger.debug(
                "%s: attempt cache miss for %s; record missing at %s",
                self.suite,
                head_sha[:8],
                record_path,
            )
            return None
        record = self._read_record_file(record_path)
        if record is None:
            return None
        if not self._record_matches_request(
            record,
            head_sha=head_sha,
            cache_source="attempt",
        ):
            return None
        logger.debug("%s: attempt cache hit for %s", self.suite, head_sha[:8])
        return record

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

    def _store_attempt_validation_record(
        self,
        record: ValidationRecord,
        session_output_dir: Path | None,
    ) -> None:
        if self.attempt_store is None or self.attempt_key is None:
            return
        record_path = self._attempt_record_path_for(record, session_output_dir)
        self.attempt_store.update(
            self.attempt_key,
            lambda attempt: replace(
                attempt,
                validation_record_path=str(record_path.resolve()),
            ),
        )

    def check(self, session_output_dir: Optional[Path] = None) -> PublishGateResult:
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
        head_sha = self._get_head_sha()
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
        if self.attempt_key is not None:
            cached = self._attempt_cached_record(head_sha)
            cache_hit_prefix = "attempt_"
        else:
            cached = self.cache.lookup(head_sha, command, self.profile)
            # The store is already contract-scoped, so a record found here was
            # written under this contract. Re-checking is deliberate: it keeps
            # one predicate answering "may this record satisfy this request"
            # for both cache sources, rather than two rules that can drift.
            if cached is not None and not self._record_matches_request(
                cached, head_sha=head_sha, cache_source="sha"
            ):
                cached = None
            cache_hit_prefix = ""
        if cached is not None and cached.passed:
            cache_lookup = f"{cache_hit_prefix}hit_passed"
            logger.info("%s: cache hit (passed) for %s", self.suite, head_sha[:8])
            # Materialize the cached record into the session run dir so
            # downstream consumers (manifest, review-exchange predicate, UI)
            # see the gate's authoritative result. Without this, a stale
            # ``validation-record.json`` from an earlier inline run remains
            # in place and silently contradicts the cache hit.
            self._materialize_cached_record(cached, session_output_dir)
            self._store_attempt_validation_record(cached, session_output_dir)
            return finish(
                PublishGateResult(
                    allowed=True,
                    reason=f"Cached validation passed for {head_sha[:8]}",
                    record=cached,
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
        # sidecar below is the authoritative cross-run cache record.
        self._store_attempt_validation_record(record, session_output_dir)

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
                    reason=_failure_reason(record),
                    record=record,
                    cache_hit=False,
                )
            )


@dataclass
class AgentGateResult:
    """Result of an agent gate check."""

    passed: bool
    reason: str
    record: Optional[ValidationRecord] = None
    record_path: Optional[str] = None  # Path where validation record was written


class AgentGate:
    """Validation gate for agent completion.

    Unlike :class:`ValidationGate` this runs unconditionally (no cache) and
    records the result for informational purposes.

    It runs the profile's *quick* contract and records its own suite label, so
    a reader can tell an agent-side run from the orchestrator's own quick gate
    while both remain honestly labelled as the quick contract. Handing it a
    publish contract is rejected rather than mislabelled (#25).
    """

    SUITE_NAME = AGENT_GATE_SUITE

    def __init__(
        self,
        worktree: Path,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
        contract: ValidationGateContract,
    ):
        """Initialize agent gate for a worktree.

        Args:
            worktree: Path to the git worktree
            contract: The profile's quick contract. An unconfigured contract
                (no ``cmd``) means the gate is disabled.

        Raises:
            ValueError: when handed a contract other than the quick one.
        """
        if not contract.is_quick:
            raise ValueError(
                "AgentGate runs the quick contract; "
                f"got {contract.kind.value!r}"
            )
        self.worktree = worktree
        self.command_runner = command_runner
        self.working_copy = working_copy
        self.contract = contract
        self.command = contract.cmd
        self.timeout_seconds = contract.timeout_seconds
        self.profile = contract.profile
        self.store = ValidationRecordStore(worktree, contract.kind)
        self.runner = ValidationRunner(self.store, command_runner)

    def _get_head_sha(self) -> Optional[str]:
        """Get the current HEAD SHA."""
        head_sha = _normalize_head_sha(self.working_copy.get_head_sha(self.worktree))
        if not head_sha:
            logger.warning("Failed to get HEAD SHA in %s", self.worktree)
        return head_sha

    def run(self, session_output_dir: Path) -> AgentGateResult:
        """Run the agent gate validation.

        Unlike ValidationGate.check(), this always runs the validation
        (no cache lookup) because we want to capture the result at
        the specific point in time when the completion command is called.

        Args:
            session_output_dir: Directory to write validation output

        Returns:
            AgentGateResult with validation status
        """
        # Gate disabled if no command
        if not self.command:
            logger.debug("Agent gate disabled (no command configured)")
            return AgentGateResult(
                passed=True,
                reason="Agent gate disabled (no command configured)",
            )

        # Get HEAD SHA
        head_sha = self._get_head_sha()
        if not head_sha:
            return AgentGateResult(
                passed=False,
                reason="Cannot determine HEAD SHA",
            )

        # Run validation
        logger.info(
            "Agent gate: running validation for %s [profile=%s]",
            head_sha[:8],
            self.profile,
        )
        record = self.runner.run(
            suite=self.SUITE_NAME,
            head_sha=head_sha,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            session_output_dir=session_output_dir,
            profile=self.profile,
        )

        # Get the path where the record was written
        record_path = str(self.store.get_record_path(head_sha))

        if record.passed:
            return AgentGateResult(
                passed=True,
                reason=f"Validation passed for {head_sha[:8]}",
                record=record,
                record_path=record_path,
            )
        else:
            return AgentGateResult(
                passed=False,
                reason=_failure_reason(record),
                record=record,
                record_path=record_path,
            )
