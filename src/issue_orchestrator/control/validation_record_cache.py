"""Where validation records live, and when one may be reused.

Split out of :mod:`.validation` so the gates keep their own file: this module
answers *storage and reuse* questions (where a record for a SHA lives, whether
a stored record still satisfies the request), while ``validation.py`` owns the
gates that decide what to run.

Storage location: ``.issue-orchestrator/validation/<kind>/<HEAD_SHA>.json``
"""

import json
import logging
from pathlib import Path
from typing import Optional

from ..domain.validation_profile import ValidationGateKind
from ..infra.atomic_json import atomic_write_json
from ..infra.emit import emit_event
from ..ports.session_output import ValidationRecord

logger = logging.getLogger(__name__)

# Schema version for validation records
VALIDATION_SCHEMA_VERSION = 1


class ValidationRecordStore:
    """Reads and writes validation records to disk.

    Storage layout — one location per contract per SHA::

        <worktree>/.issue-orchestrator/validation/<kind>/<sha>.json

    Callers running the *same* contract share one location, so the agent-side
    gate at ``coding-done`` and the orchestrator's own quick gate still reuse
    each other's result. Callers running *different* contracts do not: a
    single per-SHA file let a quick run overwrite the publish gate's record
    and destroy the only evidence of what the publish contract actually did
    (#25). Storage identity is the contract, never the caller.
    """

    VALIDATION_DIR = ".issue-orchestrator/validation"

    def __init__(self, worktree: Path, kind: ValidationGateKind):
        """Initialize store for a specific worktree and contract.

        Args:
            worktree: Path to the git worktree
            kind: Which of the profile's contracts these records belong to
        """
        self.worktree = worktree
        self.kind = kind
        self.base_dir = worktree / self.VALIDATION_DIR / kind.value

    def get_record_path(self, sha: str) -> Path:
        """Get the path for a validation record (one per contract per SHA)."""
        return self.base_dir / f"{sha}.json"

    def write(self, record: ValidationRecord) -> Path:
        """Write a validation record to disk atomically.

        Atomicity matters because two callers running the same contract (the
        agent gate and the orchestrator's quick gate) may write the same
        per-SHA file concurrently in different threads, and
        readers (cache lookups, the review-exchange predicate) parse the
        file as JSON — a torn write would surface as JSONDecodeError or,
        worse, a partial-but-syntactically-valid prefix.

        Args:
            record: The validation record to write

        Returns:
            Path to the written file
        """
        path = self.get_record_path(record.head_sha)
        atomic_write_json(path, record.to_dict())
        logger.debug("Wrote validation record to %s", path)
        return path

    def read(self, sha: str) -> Optional[ValidationRecord]:
        """Read a validation record from disk.

        Args:
            sha: The HEAD SHA

        Returns:
            ValidationRecord if found, None otherwise
        """
        path = self.get_record_path(sha)

        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)
            return ValidationRecord.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to read validation record at %s: %s", path, e)
            return None


def contract_record_path(worktree: Path, record: ValidationRecord) -> Optional[Path]:
    """Where ``record`` lives in the store, or ``None`` if it never lived there.

    Not every ``ValidationRecord`` is a stored gate result. The pre-push hook
    gate synthesizes one to report a hook failure, and it belongs to no
    profile contract. Handing back a store path for it would name a file that
    either does not exist or — worse — holds an unrelated contract's result.
    """
    try:
        kind = ValidationGateKind.from_suite(record.suite)
    except ValueError:
        logger.debug(
            "Validation record suite %r is not a profile contract; it has no "
            "record-store location",
            record.suite,
        )
        return None
    return ValidationRecordStore(worktree, kind).get_record_path(record.head_sha)


class ValidationCache:
    """Cache lookup for validation results.

    The cache is command-aware: a cached result is valid if it's for the same
    SHA AND the same command. This allows callers running the same contract to
    share validation results.

    It is also profile-aware (#7059). Command equality alone is not the same
    question as contract equality — two profiles can share a command today and
    diverge tomorrow, and a run must never be able to prove it satisfied a
    contract it did not execute. The profile is therefore part of the reuse
    key, not a derived attribute of the command.
    """

    def __init__(self, store: ValidationRecordStore):
        """Initialize cache with a record store.

        Args:
            store: Store for reading validation records
        """
        self.store = store

    def lookup(
        self,
        sha: str,
        command: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[ValidationRecord]:
        """Look up a cached validation record.

        Args:
            sha: The HEAD SHA
            command: If provided, only return record if command matches
            profile: If provided, only return record if the cached run
                executed that validation profile

        Returns:
            ValidationRecord if found and valid, None otherwise
        """
        record = self.store.read(sha)

        if record is None:
            logger.debug("Cache miss for %s", sha)
            emit_event(
                "validation.cache_miss",
                {
                    "sha": sha,
                },
            )
            return None

        # Validate schema version
        if record.schema_version != VALIDATION_SCHEMA_VERSION:
            logger.debug(
                "Cache miss for %s: schema version mismatch (%d != %d)",
                sha,
                record.schema_version,
                VALIDATION_SCHEMA_VERSION,
            )
            emit_event(
                "validation.cache_miss",
                {
                    "sha": sha,
                    "reason": "schema_version_mismatch",
                },
            )
            return None

        # If command specified, check it matches
        if command and record.command != command:
            logger.debug(
                "Cache miss for %s: command mismatch (cached='%s', requested='%s')",
                sha,
                record.command,
                command,
            )
            emit_event(
                "validation.cache_miss",
                {
                    "sha": sha,
                    "reason": "command_mismatch",
                },
            )
            return None

        if profile and record.profile != profile:
            logger.debug(
                "Cache miss for %s: profile mismatch (cached='%s', requested='%s')",
                sha,
                record.profile,
                profile,
            )
            emit_event(
                "validation.cache_miss",
                {
                    "sha": sha,
                    "reason": "profile_mismatch",
                    "profile": profile,
                },
            )
            return None

        logger.debug("Cache hit for %s (passed=%s)", sha, record.passed)
        emit_event(
            "validation.cache_hit",
            {
                "sha": sha,
                "passed": record.passed,
                "command": record.command,
                "profile": record.profile,
            },
        )
        return record

    def is_valid_hit(
        self,
        sha: str,
        command: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> bool:
        """Check if there's a valid passing cache entry.

        Args:
            sha: The HEAD SHA
            command: If provided, only match if command is the same
            profile: If provided, only match if the profile is the same

        Returns:
            True if there's a passing cache entry for this SHA (and command)
        """
        record = self.lookup(sha, command, profile)
        return record is not None and record.passed
