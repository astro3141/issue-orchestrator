"""The trusted owner that validates a Tech Lead completion (#385).

This is the implementation of
:class:`~..ports.tech_lead_completion_validation.TechLeadCompletionValidator`,
and it exists because of a measured defect rather than a preference. A bounded
Codex Tech Lead reached its completion protocol and was required to run
``prepush-check --dirty-only -v``. That command records timings under
``<git-common-dir>/issue-orchestrator/validate-timings.jsonl``, which is outside
the session's sandbox write roots, so it failed with ``Operation not
permitted`` and the run could not complete at all.

The command did not go away; its OWNER changed. This adapter runs in the
orchestrator's process, where the host and shared-repository effects are
legitimate:

* it asks the ONE dirty-tree guard (:mod:`.dirty_tree_guard`) the same question
  ``prepush-check --dirty-only`` asks, under the repository's own
  ``validation.publish.dirty_check`` policy, so there is no second rule about
  what "publishable" means;
* it appends the run's timing record to the shared git common dir — the very
  write the model could not make, made by the owner that may make it;
* it files the verdict durably in the orchestrator-owned state directory,
  outside every session write root, keyed by the exact run, session and
  candidate commit.

**Durable and create-once.** A verdict for one exact ``(run, session,
candidate)`` is written once and re-read afterwards. A publish retry that
re-enters completion processing reads the same evidence rather than
manufacturing a second, possibly kinder one; a genuinely new candidate is a new
key and gets its own verdict.

**Every failure is a recorded verdict, never a silent pass.** A checkout that
moved under the validation, an enumeration that failed, a timing write that
could not land, a durable record that could not be written or read back — each
one produces evidence whose status refuses the completion. Nothing in this
module can return ``PASSED`` without having actually checked and actually
filed.

**Operator note: a refusal is durable, so "wait and retry" is not the remedy.**
Create-once applies to every status, including the ``UNAVAILABLE`` a momentary
shared-git-dir timing-write failure produces. A retry on the same run and the
same commit re-reads that filed verdict rather than re-running the check, so the
refusal stands until the KEY changes. This is deliberate — a second, kinder
verdict for one candidate is exactly what create-once exists to prevent — but it
means there are only two ways forward, and neither is waiting:

1. land a new candidate commit (a new ``candidate_head_sha`` is a new key, and
   the ordinary way a rejected run proceeds); or
2. if the ``UNAVAILABLE`` was environmental and the same commit must be
   re-judged, delete that verdict file — a file per
   ``(run_id, session_name, candidate_head_sha)`` under
   ``<repo>/.issue-orchestrator/state/tech-lead-completion-validation/`` — and
   re-run completion, which re-files it from scratch. Deleting evidence is an
   operator action taken deliberately, which is why it is not automated here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ..domain.tech_lead_completion_validation import (
    TechLeadCompletionValidation,
    TechLeadCompletionValidationStatus,
)
from ..ports.working_copy import WorkingCopy
from .atomic_io import atomic_create_bytes
from .dirty_tree_guard import (
    DEFAULT_DIRTY_CHECK_MODE,
    resolve_publish_dirty_check_mode,
    run_dirty_tree_guard,
)
from .repo_identity import state_dir
from .validation_timings import append_validation_timing, build_timing_envelope

logger = logging.getLogger(__name__)

__all__ = [
    "TECH_LEAD_COMPLETION_VALIDATION_DIRNAME",
    "TECH_LEAD_COMPLETION_VALIDATION_TIMING_KIND",
    "TrustedTechLeadCompletionValidator",
]

#: Where the durable verdicts live, under the per-repo state directory.
TECH_LEAD_COMPLETION_VALIDATION_DIRNAME = "tech-lead-completion-validation"

#: The ``kind`` stamped on the shared-git-dir timing record. Distinct from
#: ``prepush_gate_summary`` because it is a different owner running a different
#: contract, and an operator reading the JSONL must be able to tell them apart.
TECH_LEAD_COMPLETION_VALIDATION_TIMING_KIND = "tech_lead_completion_validation"


class TrustedTechLeadCompletionValidator:
    """Runs a Tech Lead run's completion validation outside the model sandbox."""

    def __init__(self, *, working_copy: WorkingCopy, repo_root: Path) -> None:
        """
        Args:
            working_copy: The VCS port used for the head and dirty-file reads.
                Injected, so this owner has no process-local git adapter of its
                own and tests can drive every failure direction.
            repo_root: The primary checkout whose state directory holds the
                durable verdicts. Deliberately NOT the session worktree: a
                disposable scratch checkout is reaped, and evidence a session
                could write is not evidence about that session.
        """
        self._working_copy = working_copy
        self._repo_root = repo_root

    # -- port ---------------------------------------------------------------

    def validate_completion(
        self,
        *,
        run_id: str,
        session_name: str,
        worktree: Path,
        candidate_head_sha: str,
    ) -> TechLeadCompletionValidation:
        """Validate one run, file the evidence, and return what was filed."""
        existing = self._load(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
        )
        if existing is not None:
            logger.info(
                "[TECH_LEAD] reusing durable completion validation for %s/%s@%s:"
                " status=%s",
                run_id,
                session_name,
                candidate_head_sha,
                existing.status.value,
            )
            return existing

        wall_started_at = datetime.now(timezone.utc)
        monotonic_started_at = time.monotonic()
        status, detail, mode = self._execute(
            worktree=worktree, candidate_head_sha=candidate_head_sha
        )
        timing_failure = self._record_timing(
            worktree=worktree,
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
            status=status,
            detail=detail,
            mode=mode,
            wall_started_at=wall_started_at,
            monotonic_started_at=monotonic_started_at,
        )
        if timing_failure is not None:
            # The shared-git-dir write IS part of the contract this owner took
            # over. An owner that could not make it has not run the contract,
            # so it must not report a pass it cannot evidence.
            status = TechLeadCompletionValidationStatus.UNAVAILABLE
            detail = (
                "the trusted owner could not record the completion-validation"
                f" timing under the repository's shared git dir: {timing_failure}"
            )

        validation = TechLeadCompletionValidation.concluded(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
            status=status,
            detail=detail,
        )
        return self._persist(validation)

    # -- the check itself ---------------------------------------------------

    def _execute(
        self, *, worktree: Path, candidate_head_sha: str
    ) -> tuple[TechLeadCompletionValidationStatus, str, str]:
        """Run the guard, and say what happened, in the owner's own words."""
        try:
            observed_head = self._working_copy.get_head_sha(worktree)
            if not observed_head:
                return (
                    TechLeadCompletionValidationStatus.UNAVAILABLE,
                    f"the commit {worktree} stands at could not be read",
                    DEFAULT_DIRTY_CHECK_MODE,
                )
            if observed_head != candidate_head_sha:
                return (
                    TechLeadCompletionValidationStatus.FAILED,
                    "the checkout moved while the completion validation ran:"
                    f" {candidate_head_sha} -> {observed_head}",
                    DEFAULT_DIRTY_CHECK_MODE,
                )
            mode = resolve_publish_dirty_check_mode(worktree)
            result = run_dirty_tree_guard(
                worktree, mode=mode, working_copy=self._working_copy
            )
        except TimeoutError as exc:
            return (
                TechLeadCompletionValidationStatus.TIMED_OUT,
                f"the completion validation for {worktree} timed out: {exc}",
                DEFAULT_DIRTY_CHECK_MODE,
            )
        except (OSError, ValueError) as exc:
            return (
                TechLeadCompletionValidationStatus.UNAVAILABLE,
                f"the completion validation for {worktree} could not run: {exc}",
                DEFAULT_DIRTY_CHECK_MODE,
            )
        if result.publishable:
            return (
                TechLeadCompletionValidationStatus.PASSED,
                result.detail,
                result.mode,
            )
        return (TechLeadCompletionValidationStatus.FAILED, result.detail, result.mode)

    def _record_timing(
        self,
        *,
        worktree: Path,
        run_id: str,
        session_name: str,
        candidate_head_sha: str,
        status: TechLeadCompletionValidationStatus,
        detail: str,
        mode: str,
        wall_started_at: datetime,
        monotonic_started_at: float,
    ) -> str | None:
        """Append the shared-git-dir timing record; return the failure, if any."""
        record: dict[str, object] = {
            "kind": TECH_LEAD_COMPLETION_VALIDATION_TIMING_KIND,
            "run_id": run_id,
            "session_name": session_name,
            "head_sha": candidate_head_sha,
            "dirty_check": mode,
            "status": status.value,
            "detail": detail,
            **build_timing_envelope(
                wall_started_at=wall_started_at,
                monotonic_started_at=monotonic_started_at,
            ),
        }
        try:
            append_validation_timing(worktree, record)
        except OSError as exc:
            logger.warning(
                "[TECH_LEAD] completion-validation timing write failed for"
                " %s/%s: %s",
                run_id,
                session_name,
                exc,
            )
            return str(exc)
        return None

    # -- durable evidence ---------------------------------------------------

    def _evidence_path(
        self, *, run_id: str, session_name: str, candidate_head_sha: str
    ) -> Path:
        """One stable file per exact ``(run, session, candidate)``.

        Hashed rather than composed from the parts, because a session name is
        operator-influenced text and a path built from it could collide with a
        neighbour's or escape the directory. The identity is carried INSIDE the
        payload and re-checked on read, so the file name is only a lookup key.
        """
        digest = hashlib.sha256(
            "\0".join((run_id, session_name, candidate_head_sha)).encode("utf-8")
        ).hexdigest()
        return (
            state_dir(self._repo_root)
            / TECH_LEAD_COMPLETION_VALIDATION_DIRNAME
            / f"{digest}.json"
        )

    def _load(
        self, *, run_id: str, session_name: str, candidate_head_sha: str
    ) -> TechLeadCompletionValidation | None:
        """Read an already-filed verdict for this exact candidate.

        ``None`` means nothing is filed. A file that exists but does not parse,
        or that names a different run/session/candidate, is treated as nothing
        filed for THIS candidate — the caller then runs the validation and
        files it, and a genuinely unwritable record ends as ``UNAVAILABLE``.
        """
        path = self._evidence_path(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[TECH_LEAD] durable completion validation at %s is unreadable"
                " (%s); it will be re-validated",
                path,
                exc,
            )
            return None
        try:
            validation = TechLeadCompletionValidation.from_payload(payload)
        except ValueError as exc:
            logger.warning(
                "[TECH_LEAD] durable completion validation at %s is not usable"
                " (%s); it will be re-validated",
                path,
                exc,
            )
            return None
        if not validation.binds_to(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
        ):
            logger.warning(
                "[TECH_LEAD] durable completion validation at %s names"
                " %s/%s@%s, not %s/%s@%s; it is not evidence about this"
                " candidate",
                path,
                validation.run_id,
                validation.session_name,
                validation.candidate_head_sha,
                run_id,
                session_name,
                candidate_head_sha,
            )
            return None
        return validation

    def _persist(
        self, validation: TechLeadCompletionValidation
    ) -> TechLeadCompletionValidation:
        """File the verdict create-once, then return what the record holds.

        Returning the READ-BACK value is what makes an unwritable or unreadable
        record an ``UNAVAILABLE`` verdict instead of an unnoticed no-op: a
        completion is gated on what is durably filed, never on what this
        process happened to compute.
        """
        path = self._evidence_path(
            run_id=validation.run_id,
            session_name=validation.session_name,
            candidate_head_sha=validation.candidate_head_sha,
        )
        payload = json.dumps(validation.to_payload(), sort_keys=True).encode("utf-8")
        try:
            atomic_create_bytes(path, payload)
        except FileExistsError:
            # Another writer won the race; its verdict is the durable one.
            pass
        except OSError as exc:
            logger.error(
                "[TECH_LEAD] could not file completion validation for %s/%s@%s:"
                " %s",
                validation.run_id,
                validation.session_name,
                validation.candidate_head_sha,
                exc,
            )
            return TechLeadCompletionValidation.concluded(
                run_id=validation.run_id,
                session_name=validation.session_name,
                candidate_head_sha=validation.candidate_head_sha,
                status=TechLeadCompletionValidationStatus.UNAVAILABLE,
                detail=(
                    "the trusted completion-validation verdict could not be"
                    f" filed durably at {path}: {exc}"
                ),
            )
        filed = self._load(
            run_id=validation.run_id,
            session_name=validation.session_name,
            candidate_head_sha=validation.candidate_head_sha,
        )
        if filed is not None:
            return filed
        return TechLeadCompletionValidation.concluded(
            run_id=validation.run_id,
            session_name=validation.session_name,
            candidate_head_sha=validation.candidate_head_sha,
            status=TechLeadCompletionValidationStatus.UNAVAILABLE,
            detail=(
                "the trusted completion-validation verdict was written but"
                f" could not be read back from {path}"
            ),
        )
