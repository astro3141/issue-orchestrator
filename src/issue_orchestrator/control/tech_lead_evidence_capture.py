"""Preserve a tech_lead run's staged evidence before its worktree is reaped (#360).

Everything a tech_lead run is judged on is staged inside its own worktree, under
``<run_dir>/tech-lead-data``: the assignment, the batch manifest, the staged
exact-candidate reviewer evidence (#345) and leaf contracts, each candidate's
materialized diff or the recorded reason there is none (#359), the board
snapshot, the evidence map, and the agent's own decision/report pair. Supported
teardown removes that worktree — a focused run's scratch checkout is
force-removed the moment it completes, regardless of the cleanup config — so
after disposal the run's own files no longer exist anywhere. R29 proof #354 hit
exactly that: the live tech-lead worktree for anchor #358 was reaped before its
manifest, candidate evidence, candidate contracts, decision artifact and the
corrupt candidate diff could be read, and the proof could only preserve
paraphrase.

This module is the single owner of the repair, and it is deliberately small:

* **When.** The capture runs from the completion handoff, BEFORE completion
  processing touches anything and long before the planner turns the cleanup
  fact into a removal. Running it there rather than at teardown is what makes
  it cover the runs that need it most — a FAILED or TIMED_OUT tech_lead
  session, which never reaches the decision-artifact seams at all, still
  staged a full set of launch inputs worth keeping.
* **Where.** :func:`~..domain.tech_lead_evidence_capture.tech_lead_evidence_capture_dir`,
  under the HOST repository rather than any worktree, keyed by the run's own
  identity so two runs of one anchor issue — or the several sessions one
  worktree hosts — never overwrite each other.
* **What teardown it changes.** None. The capture is a read of the worktree and
  a write outside it; no disposal is withheld, no worktree is retained, and a
  capture that fails does not fail the session.
* **How a failure is told apart from a success.** By construction, not by
  convention. A :class:`~..domain.tech_lead_evidence_capture.TechLeadEvidenceCapture`
  cannot both hold no artifacts and record no failure, the receipt is written to
  the durable location on BOTH paths, the log line is ERROR on the failing one,
  and the trace event carries ``preserved`` either way. Nothing here reports a
  capture that did not happen as evidence preserved.

The source tree is agent-writable, which shapes two rules the copy will not
bend: entries that are not regular files are recorded and skipped rather than
followed, and a staged tree above :data:`MAX_CAPTURE_BYTES` is refused outright
instead of copied into the host repository.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import Session
from ..domain.tech_lead_evidence_capture import (
    CAPTURE_RECEIPT_FILENAME,
    CapturedArtifact,
    TechLeadEvidenceCapture,
    TechLeadEvidenceCaptureError,
    tech_lead_evidence_capture_dir,
    tech_lead_evidence_root,
)
from ..domain.tech_lead_session import TECH_LEAD_DATA_DIRNAME
from ..events import EventName
from ..ports import EventSink, make_trace_event
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)

#: Upper bound on the staged tree a single run may have preserved. Real
#: tech-lead-data directories are manifests, JSON descriptors and a handful of
#: diffs — megabytes at the outside. The cap exists because the source is
#: agent-writable: without it, one session writing a huge file into its own
#: staging directory would have the orchestrator copy it into the host
#: repository. Exceeding it is an explicit capture failure, never a silent
#: truncation.
MAX_CAPTURE_BYTES = 256 * 1024 * 1024

_DIGEST_CHUNK_BYTES = 1024 * 1024


def capture_tech_lead_session_evidence(
    *,
    config: "Config",
    session: Session,
    events: EventSink | None,
) -> TechLeadEvidenceCapture | None:
    """Preserve *session*'s staged tech-lead evidence, if it is a tech_lead run.

    Returns ``None`` for every session no tech_lead owner governs — a coder's, a
    reviewer's — which is the overwhelming majority and costs them a single
    identity check. Identity comes from
    :func:`~.tech_lead_session_policy.is_tech_lead_session`, the same owner the
    launch and completion seams ask, so a run captured here is exactly a run
    those seams call a tech_lead run.

    Never raises: a capture failure is reported through the returned value, the
    receipt, the ERROR log and the trace event, and must not take a completing
    session down with it.
    """
    governed = is_tech_lead_session(config.tech_lead_review_agent, session.agent_label)
    if not governed:
        return None
    run = session.run_assets.identity
    source_dir = session.run_assets.run_dir / TECH_LEAD_DATA_DIRNAME
    build = _CaptureBuilder(
        session_name=run.session_name,
        run_id=run.run_id,
        issue_number=session.issue.number,
        source_dir=source_dir,
    )
    try:
        destination = tech_lead_evidence_capture_dir(
            config.repo_root, session_name=run.session_name, run_id=run.run_id
        )
    except TechLeadEvidenceCaptureError as exc:
        # The run identity itself is unusable, so no keyed directory exists to
        # file a receipt in — writing one anywhere else would put an unkeyed
        # capture.json where the next unkeyed failure overwrites it. The ERROR
        # log and the event are the whole report on this path.
        capture = build.failed(tech_lead_evidence_root(config.repo_root), str(exc))
        _announce(capture, events)
        return capture
    try:
        artifacts, skipped = _copy_staged_tree(source_dir, destination)
    except Exception as exc:
        capture = build.failed(destination, f"{type(exc).__name__}: {exc}")
    else:
        capture = build.preserved(destination, artifacts, skipped)
    _write_receipt(capture)
    _announce(capture, events)
    return capture


@dataclass(frozen=True, slots=True)
class _CaptureBuilder:
    """The identity half of the outcome, so both branches state it identically."""

    session_name: str
    run_id: str
    issue_number: int
    source_dir: Path

    def failed(self, destination: Path, failure: str) -> TechLeadEvidenceCapture:
        return self._build(destination, failure=failure)

    def preserved(
        self,
        destination: Path,
        artifacts: tuple[CapturedArtifact, ...],
        skipped: tuple[str, ...],
    ) -> TechLeadEvidenceCapture:
        return self._build(destination, artifacts=artifacts, skipped=skipped)

    def _build(
        self,
        destination: Path,
        *,
        artifacts: tuple[CapturedArtifact, ...] = (),
        skipped: tuple[str, ...] = (),
        failure: str = "",
    ) -> TechLeadEvidenceCapture:
        return TechLeadEvidenceCapture(
            session_name=self.session_name,
            run_id=self.run_id,
            issue_number=self.issue_number,
            source_dir=self.source_dir,
            destination=destination,
            captured_at=datetime.now(timezone.utc).isoformat(),
            artifacts=artifacts,
            skipped=skipped,
            failure=failure,
        )


def _copy_staged_tree(
    source_dir: Path, destination: Path
) -> tuple[tuple[CapturedArtifact, ...], tuple[str, ...]]:
    """Copy every regular file under *source_dir*, digesting as it goes.

    Raises :class:`TechLeadEvidenceCaptureError` when there is nothing to
    preserve or when the staged tree is too large to hold, so the caller records
    a failure rather than an empty success.
    """
    if not source_dir.is_dir():
        raise TechLeadEvidenceCaptureError(
            f"no staged tech-lead data to preserve at {source_dir}"
        )
    files, skipped, total_bytes = _survey(source_dir)
    if not files:
        raise TechLeadEvidenceCaptureError(
            f"staged tech-lead data holds no regular files: {source_dir}"
        )
    if total_bytes > MAX_CAPTURE_BYTES:
        raise TechLeadEvidenceCaptureError(
            f"staged tech-lead data is {total_bytes} bytes, above the"
            f" {MAX_CAPTURE_BYTES}-byte capture budget: {source_dir}"
        )
    captured: list[CapturedArtifact] = []
    for relative_path, path in files:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        captured.append(
            CapturedArtifact(
                relative_path=relative_path.as_posix(),
                size_bytes=target.stat().st_size,
                sha256=_digest(target),
            )
        )
    return tuple(captured), skipped


def _survey(source_dir: Path) -> tuple[list[tuple[Path, Path]], tuple[str, ...], int]:
    """Enumerate the regular files to copy, what to skip, and the total size.

    Walks WITHOUT following links, in both directions that matters: a symlinked
    subdirectory is never descended into, and a symlinked file is recorded as
    skipped rather than resolved. The staging directory is agent-writable, so a
    capture that followed either would copy arbitrary host content into the
    host repository under this run's name.
    """
    files: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(source_dir, followlinks=False):
        here = Path(dirpath)
        for name in sorted(dirnames):
            entry = here / name
            if entry.is_symlink():
                skipped.append(entry.relative_to(source_dir).as_posix())
        # Do not descend into linked directories; ``followlinks=False`` covers
        # the descent, and pruning keeps them out of the walk entirely.
        dirnames[:] = sorted(
            name for name in dirnames if not (here / name).is_symlink()
        )
        for name in sorted(filenames):
            entry = here / name
            relative_path = entry.relative_to(source_dir)
            if entry.is_symlink() or not entry.is_file():
                skipped.append(relative_path.as_posix())
                continue
            files.append((relative_path, entry))
            total_bytes += entry.stat().st_size
    return files, tuple(skipped), total_bytes


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DIGEST_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_receipt(capture: TechLeadEvidenceCapture) -> None:
    """Write the capture receipt into the durable location, on both paths.

    A receipt that says ``preserved: false`` is the point: an operator who finds
    the run's directory must be told it holds nothing, rather than left to infer
    it from a directory that merely looks thin.
    """
    receipt = capture.destination / CAPTURE_RECEIPT_FILENAME
    try:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(capture.to_payload(), indent=2) + "\n", encoding="utf-8"
        )
    except Exception as exc:
        logger.error(
            "[tech_lead] Could not write the evidence-capture receipt for run %s"
            " at %s: %s",
            capture.run_id,
            receipt,
            exc,
        )


def _announce(capture: TechLeadEvidenceCapture, events: EventSink | None) -> None:
    """Log and publish what the capture did, at a level that matches it."""
    if capture.preserved:
        logger.info(
            "[tech_lead] Captured %d evidence file(s) (%d bytes) for run %s"
            " before teardown: %s",
            len(capture.artifacts),
            capture.total_bytes,
            capture.run_id,
            capture.destination,
        )
    else:
        logger.error(
            "[tech_lead] Evidence capture FAILED for run %s (issue #%d); this"
            " run's staged artifacts will be lost at teardown: %s",
            capture.run_id,
            capture.issue_number,
            capture.failure,
        )
    if events is None:
        return
    try:
        events.publish(make_trace_event(
            EventName.TECH_LEAD_EVIDENCE_CAPTURED,
            {
                "issue_number": capture.issue_number,
                "session_name": capture.session_name,
                "run_id": capture.run_id,
                "preserved": capture.preserved,
                "destination": str(capture.destination),
                "artifact_count": len(capture.artifacts),
                "total_bytes": capture.total_bytes,
                "skipped": list(capture.skipped),
                "failure": capture.failure,
            },
        ))
    except Exception as exc:
        logger.warning(
            "[tech_lead] Evidence-capture event publish failed for run %s: %s",
            capture.run_id,
            exc,
        )


__all__ = ["MAX_CAPTURE_BYTES", "capture_tech_lead_session_evidence"]
