"""Ports for recording run-scoped evidence."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .session_output import ValidationRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.artifact_contracts import ValidationOutcome
    from ..domain.session_run import ValidationArtifactPaths


@runtime_checkable
class ValidationEvidenceRecorder(Protocol):
    """Records validation evidence for a session run."""

    def record_validation_evidence(
        self,
        *,
        run_dir: Path,
        worktree: Path,
        record: ValidationRecord | None,
        record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Record validation artifacts and structured test reports."""
        ...

    def record_gate_result(
        self,
        *,
        artifacts: "ValidationArtifactPaths",
        worktree: Path,
        outcome: "ValidationOutcome",
        record: ValidationRecord | None,
        store_record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        """Record everything one gate run leaves on its run's manifest.

        The whole of it, in one call: the typed outcome, the record the gate
        wrote, and the logs and structured reports that explain it. Every
        caller that runs a gate against a run owes the run all four, and the
        two that do — an agent's ``coding-done`` and the continuation's
        system-run preparation — recorded different subsets of them for as long
        as each did it itself. A run whose outcome is missing is not "a run
        with less detail": ``load_validation_failure_summary`` reads the
        outcome first and returns nothing without it, so every validation
        surface in the product goes dark for that run.

        Returns:
            The record path recorded on the manifest, or ``None`` when the run
            produced neither a record nor a path to one — a gate that executed
            no command, which has nothing to say about a run rather than
            something green to say about it.
        """
        ...


class NullValidationEvidenceRecorder:
    """No-op recorder used by tests that do not exercise evidence wiring."""

    def record_validation_evidence(
        self,
        *,
        run_dir: Path,
        worktree: Path,
        record: ValidationRecord | None,
        record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> None:
        _ = (run_dir, worktree, record, record_path, junit_xml_paths)

    def record_gate_result(
        self,
        *,
        artifacts: "ValidationArtifactPaths",
        worktree: Path,
        outcome: "ValidationOutcome",
        record: ValidationRecord | None,
        store_record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        _ = (artifacts, worktree, outcome, record, store_record_path, junit_xml_paths)
        return None
