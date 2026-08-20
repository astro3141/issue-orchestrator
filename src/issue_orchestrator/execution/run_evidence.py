"""Run-scoped evidence catalog.

The run manifest is the contract for operator-facing evidence. Producers record
artifacts here once; UI routes and diagnostics consume the recorded contract
instead of re-discovering files from configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..domain.artifact_contracts import ValidationOutcome
from ..domain.run_manifest import RunManifest
from ..domain.session_run import ValidationArtifactPaths
from ..infra.e2e_reports import discover_report_artifacts
from ..infra.validation_junit_paths import validation_record_junit_modified_after
from ..ports.session_output import SessionOutput, ValidationRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordedRunEvidence:
    """Paths recorded for one run evidence update."""

    validation_record_path: str | None = None
    validation_stdout_path: str | None = None
    validation_stderr_path: str | None = None
    junit_xml_paths: tuple[str, ...] = ()


class RunEvidenceRecorder:
    """Owner for writing run evidence into ``manifest.json``."""

    def __init__(self, session_output: SessionOutput) -> None:
        self._session_output = session_output

    def record_gate_result(
        self,
        *,
        artifacts: ValidationArtifactPaths,
        worktree: Path,
        outcome: ValidationOutcome,
        record: ValidationRecord | None,
        store_record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        """Record everything one gate run leaves on its run's manifest.

        See :meth:`~...ports.run_evidence.ValidationEvidenceRecorder.
        record_gate_result` for what this owns and why one caller may not
        record a subset of it.

        The run-scoped record wins over the contract-scoped store's copy when
        both exist, because it is the one every run-scoped consumer resolves
        against — the dialog, the audit and the review exchange's mirror all
        start from the run directory.

        The outcome is written through the typed API, which owns the three
        legacy fields and clears a previous run's reason; the paths go through
        :meth:`record_validation_evidence`, which owns the pointers. Two
        writes, two owners, no third spelling of either.
        """
        resolved = _first_existing(artifacts.record_path, store_record_path)
        if record is None and resolved is None:
            # A gate that executed no command: disabled, or refused before it
            # could run. It produced no account of this run, and a manifest
            # saying "validation passed" for it would be an account this
            # recorder invented.
            return None
        self._session_output.update_validation_outcome(artifacts.run_dir, outcome)
        self.record_validation_evidence(
            run_dir=artifacts.run_dir,
            worktree=worktree,
            record=record,
            record_path=resolved,
            junit_xml_paths=junit_xml_paths,
        )
        return resolved

    def record_validation_evidence(
        self,
        *,
        run_dir: Path,
        worktree: Path,
        record: ValidationRecord | None,
        record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Record validation logs and structured test artifacts for a run."""
        evidence = _validation_evidence(
            worktree=worktree,
            record=record,
            record_path=record_path,
            junit_xml_paths=junit_xml_paths,
        )
        updates: dict[str, Any] = {}
        if evidence.validation_record_path:
            updates["validation_record_path"] = evidence.validation_record_path
        if evidence.validation_stdout_path:
            updates["validation_stdout"] = evidence.validation_stdout_path
        if evidence.validation_stderr_path:
            updates["validation_stderr"] = evidence.validation_stderr_path
        artifacts = _merged_artifacts(
            self._session_output.read_manifest(run_dir) or {},
            junit_xml_paths=evidence.junit_xml_paths,
        )
        if artifacts:
            updates["artifacts"] = artifacts
        if updates:
            self._session_output.update_manifest(run_dir, updates)


def _first_existing(*candidates: Path | None) -> Path | None:
    """The first candidate path that is actually on disk."""
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    return None


def recorded_junit_xml_paths(run_dir: Path) -> tuple[str, ...]:
    """Return JUnit XML paths recorded in a run manifest."""
    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        return ()
    return manifest.junit_xml_paths()


def recorded_validation_junit_xml_paths(run_dir: Path) -> tuple[str, ...]:
    """Return validation JUnit XML paths recorded in a run manifest."""
    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        return ()
    return manifest.junit_xml_paths(key_prefix="validation_junit_xml_")


def _validation_evidence(
    *,
    worktree: Path,
    record: ValidationRecord | None,
    record_path: Path | None,
    junit_xml_paths: tuple[str, ...] | list[str],
) -> RecordedRunEvidence:
    resolved_record_path = (
        _resolve_record_path(worktree, str(record_path)) if record_path else None
    )
    stdout_path = _resolve_record_path(worktree, record.stdout_path) if record else None
    stderr_path = _resolve_record_path(worktree, record.stderr_path) if record else None
    return RecordedRunEvidence(
        validation_record_path=(
            str(resolved_record_path)
            if resolved_record_path and resolved_record_path.exists()
            else None
        ),
        validation_stdout_path=str(stdout_path) if stdout_path and stdout_path.exists() else None,
        validation_stderr_path=str(stderr_path) if stderr_path and stderr_path.exists() else None,
        junit_xml_paths=_discover_junit_paths(
            worktree,
            junit_xml_paths,
            modified_after=validation_record_junit_modified_after(record),
        ),
    )


def _resolve_record_path(worktree: Path, value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else worktree / candidate


def _discover_junit_paths(
    worktree: Path,
    junit_xml_paths: tuple[str, ...] | list[str],
    *,
    modified_after: float | None = None,
) -> tuple[str, ...]:
    paths = tuple(path for path in junit_xml_paths if path)
    if not paths:
        return ()
    try:
        _, artifacts = discover_report_artifacts(
            worktree,
            junit_xml_paths=paths,
            artifact_paths=(),
            modified_after=modified_after,
        )
    except ValueError as exc:
        logger.debug("No validation JUnit evidence recorded under %s: %s", worktree, exc)
        return ()
    return tuple(artifact.path for artifact in artifacts if artifact.kind == "junit_xml")


def _merged_artifacts(
    manifest: dict[str, Any],
    *,
    junit_xml_paths: tuple[str, ...],
) -> dict[str, Any]:
    artifacts_raw = manifest.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
    artifacts = {
        key: value
        for key, value in artifacts.items()
        if not (
            isinstance(value, dict)
            and value.get("kind") == "junit_xml"
            and str(key).startswith("validation_junit_xml_")
        )
    }
    for path in sorted(junit_xml_paths):
        artifacts[f"validation_junit_xml_{_artifact_key_suffix(path)}"] = {
            "kind": "junit_xml",
            "path": path,
            "content_type": "application/xml",
        }
    return artifacts


def _artifact_key_suffix(path: str) -> str:
    return sha256(path.encode("utf-8")).hexdigest()[:12]
