"""Attaching a gate's validation evidence to the run it belongs to.

One owner for the completion path's answer to "where does this gate's record
live, and what does the run manifest say about it". It was three private
methods on ``CompletionProcessor``, which meant the rule lived inside a class
that is about something else entirely and could only be exercised through it.

The precedence rule is the whole of the policy, and it is stated once here:
when a caller supplies a source record, that source becomes the run
directory's authoritative record; a pre-existing run-directory file is used
*only* when no source was supplied. Refusing the caller's source and silently
publishing a stale local snapshot is the #6017 P2 path-leak class in reverse.

Copying is symlink-safe by construction: the source is opened under the
worktree with ``O_NOFOLLOW`` on every path component and never reopened by
path string (#6017 re-review-4 P2).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.session_run import ValidationArtifactPaths
from ..ports.session_output import SessionOutput, ValidationRecord
from .validation_record_cache import contract_record_path
from .validation_record_containment import (
    copy_from_fd,
    open_contained_validation_record,
)

logger = logging.getLogger(__name__)


class CompletionValidationEvidence:
    """Records one gate's validation artifacts on its run's manifest."""

    def __init__(self, session_output: SessionOutput) -> None:
        self._session_output = session_output

    def attach(
        self,
        worktree: Path,
        validation_artifacts: ValidationArtifactPaths,
        record: ValidationRecord | None = None,
        record_path: Path | None = None,
    ) -> None:
        """Attach a gate's record and logs to the run they belong to.

        ``validation_artifacts`` comes from the gate that produced them, so a
        gate writing into its own subdirectory still has its record paired
        with its *own* stdout and stderr rather than another gate's (#25).
        """
        run_dir = validation_artifacts.run_dir
        if record_path is None and record is not None:
            record_path = contract_record_path(worktree, record)
        run_dir_record_path = validation_artifacts.record_path
        effective_record_path = self._materialize_record(
            worktree=worktree,
            record_path=record_path,
            run_dir_record_path=run_dir_record_path,
        )
        if effective_record_path is not None:
            self._session_output.update_manifest(
                run_dir,
                {"validation_record_path": str(effective_record_path)},
            )
            try:
                (run_dir / "validation-record.path").write_text(
                    str(effective_record_path)
                )
            except OSError:
                logger.debug("Failed to write validation pointer for %s", run_dir)

        # Update manifest with validation output paths (files written by validation)
        updates: dict[str, str] = {}
        stdout_path = validation_artifacts.stdout_path
        stderr_path = validation_artifacts.stderr_path

        if stdout_path.exists():
            updates["validation_stdout"] = str(stdout_path)
        if stderr_path.exists():
            updates["validation_stderr"] = str(stderr_path)

        if updates:
            self._session_output.update_manifest(run_dir, updates)

    def _materialize_record(
        self,
        *,
        worktree: Path,
        record_path: Path | None,
        run_dir_record_path: Path,
    ) -> Path | None:
        """Resolve the run-dir record's authoritative content and return its path.

        Returns ``None`` when nothing can be attached. See the module docstring
        for why the caller's source outranks a pre-existing run-dir file.
        """
        if record_path is None or not record_path.exists():
            return run_dir_record_path if run_dir_record_path.exists() else None
        # Source/destination identity check. ``copy_from_fd`` opens ``dst``
        # with ``open(dst, "wb")`` which truncates the file before reading
        # completes, so a same-file copy ends up as empty JSON. When the caller
        # already wrote the authoritative record into run_dir (the common case
        # post-PublishGate fix), there's nothing to copy — just attach.
        try:
            same_file = (
                record_path.resolve(strict=False)
                == run_dir_record_path.resolve(strict=False)
            )
        except OSError:
            same_file = False
        if same_file:
            return run_dir_record_path
        src_fd = open_contained_validation_record(str(record_path), worktree)
        if src_fd is not None and copy_from_fd(src_fd, run_dir_record_path):
            return run_dir_record_path
        return None


__all__ = ["CompletionValidationEvidence"]
