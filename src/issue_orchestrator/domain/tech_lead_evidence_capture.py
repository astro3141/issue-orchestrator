"""What a durable capture of a tech_lead run's staged evidence IS (#360).

A tech_lead run stages everything it is judged on inside its own worktree, in
``<run_dir>/tech-lead-data``: the assignment, the batch manifest, the staged
exact-candidate reviewer evidence and leaf contracts, each candidate's
materialized diff (or the recorded reason there is none), the board snapshot,
and finally the agent's own decision/report pair. The worktree is disposable —
a focused run's scratch checkout is force-removed the moment it completes — so
once teardown runs, every one of those files is gone and the only surviving
account of the run is prose written about it afterwards. R29 proof #354 lost
exactly that set for anchor #358.

This module owns the *shape* of the answer, not the copying:

* :func:`tech_lead_evidence_capture_dir` — WHERE a run's capture lives, keyed by
  the run's own identity so two runs of the same issue (or of the same
  worktree) can never overwrite each other;
* :class:`CapturedArtifact` / :class:`TechLeadEvidenceCapture` — WHAT was
  captured, with each file's size and digest, so a later reader can prove the
  bytes it holds are the bytes the run staged;
* the invariant that makes silence impossible: a capture either lists artifacts
  it preserved or records the failure that stopped it. "Preserved nothing, said
  nothing" is unrepresentable, which is what keeps a failed capture from
  reading as evidence held.

Copying and hashing live in :mod:`..control.tech_lead_evidence_capture`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Root directory name, under the host repository's ``.issue-orchestrator``,
#: that holds every captured tech_lead run. Deliberately its own root rather
#: than a corner of ``state/``: an operator looking for a reaped run's evidence
#: should find it by name, not by knowing where the SQLite stores live.
TECH_LEAD_EVIDENCE_DIRNAME = "tech-lead-evidence"

#: The receipt written beside a capture's copied tree. Present even when the
#: capture failed — that is the whole point of writing it.
CAPTURE_RECEIPT_FILENAME = "capture.json"


class TechLeadEvidenceCaptureError(RuntimeError):
    """A tech_lead run's staged evidence could not be preserved."""


def tech_lead_evidence_root(repo_root: Path) -> Path:
    """The host-repository directory every captured tech_lead run lives under."""
    return Path(repo_root) / ".issue-orchestrator" / TECH_LEAD_EVIDENCE_DIRNAME


def tech_lead_evidence_capture_dir(
    repo_root: Path, *, session_name: str, run_id: str
) -> Path:
    """Where THIS run's capture lives.

    Keyed by session name and run id together, both taken from the run's own
    :class:`~.session_run.SessionRunIdentity`. A tech_lead run launches as an
    ``issue-{N}`` session, so the session name alone would collide across every
    run aimed at the same anchor — including the batch review that re-fires
    against the same anchor issue an hour later, and the several sessions a
    single worktree hosts over its life. The run id is what makes each capture
    distinct; the session name is what makes the tree navigable.

    Both segments are validated rather than sanitized: they come from the
    orchestrator's own run identity, so anything that is not a plain path
    segment means the identity itself is wrong, and quietly rewriting it would
    file the run's evidence under a name nothing can find it by.
    """
    return (
        tech_lead_evidence_root(repo_root)
        / _identity_segment(session_name, "session_name")
        / _identity_segment(run_id, "run_id")
    )


def _identity_segment(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise TechLeadEvidenceCaptureError(
            f"tech_lead evidence capture requires a non-empty {field_name}"
        )
    if value in {".", ".."} or any(ch in value for ch in ("/", "\\", "\0")):
        raise TechLeadEvidenceCaptureError(
            f"tech_lead evidence capture {field_name} is not a path segment:"
            f" {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class CapturedArtifact:
    """One preserved file, with the proof it is the file that was staged."""

    relative_path: str
    size_bytes: int
    sha256: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class TechLeadEvidenceCapture:
    """What one attempt to preserve a tech_lead run's evidence produced.

    ``failure`` and ``artifacts`` are the two halves of a single answer, and the
    constructor refuses the combination that would let a capture pass as
    evidence it never held: an empty failure with no artifacts. A capture that
    preserved nothing MUST say why, in words a reader months later can act on.
    """

    session_name: str
    run_id: str
    issue_number: int
    source_dir: Path
    destination: Path
    captured_at: str
    artifacts: tuple[CapturedArtifact, ...] = ()
    #: Entries under the staged directory that were NOT regular files (symlinks
    #: above all). Recorded rather than followed: the source tree is
    #: agent-writable, and a capture that resolved a symlink would copy whatever
    #: it pointed at into the host repository under the run's name.
    skipped: tuple[str, ...] = ()
    failure: str = ""

    def __post_init__(self) -> None:
        if not self.failure and not self.artifacts:
            raise ValueError(
                "TechLeadEvidenceCapture preserved no artifacts and recorded no"
                " failure; a capture that held nothing must say why"
            )

    @property
    def preserved(self) -> bool:
        """True when this run's staged evidence is durably held."""
        return not self.failure

    @property
    def total_bytes(self) -> int:
        return sum(artifact.size_bytes for artifact in self.artifacts)

    def to_payload(self) -> dict[str, Any]:
        """The receipt written beside the copied tree."""
        return {
            "session_name": self.session_name,
            "run_id": self.run_id,
            "issue_number": self.issue_number,
            "source_dir": str(self.source_dir),
            "destination": str(self.destination),
            "captured_at": self.captured_at,
            "preserved": self.preserved,
            "failure": self.failure,
            "artifacts": [artifact.to_payload() for artifact in self.artifacts],
            "skipped": list(self.skipped),
        }
