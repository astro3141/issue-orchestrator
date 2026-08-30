"""Test doubles for the trusted Tech Lead completion validator (#385).

Shared so every suite that drives a tech_lead completion asks the SAME port the
production seam asks, and so the fail-closed directions (failed, timed out,
unavailable, bound to another candidate) are expressed as data rather than
re-invented per test.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.domain.tech_lead_completion_validation import (
    TechLeadCompletionValidation,
    TechLeadCompletionValidationStatus,
)


class StubTechLeadCompletionValidator:
    """Returns a scripted verdict and records what it was asked.

    ``status`` drives the verdict for the candidate it was asked about.
    ``bind_to`` (when set) makes the returned evidence name a DIFFERENT commit,
    which is how the drift direction is forced without touching the checkout.
    ``raises`` makes the owner blow up, the "cannot produce a verdict" case.
    """

    def __init__(
        self,
        *,
        status: TechLeadCompletionValidationStatus = (
            TechLeadCompletionValidationStatus.PASSED
        ),
        detail: str = "stub verdict",
        bind_to: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        self.bind_to = bind_to
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def validate_completion(
        self,
        *,
        run_id: str,
        session_name: str,
        worktree: Path,
        candidate_head_sha: str,
    ) -> TechLeadCompletionValidation:
        self.calls.append(
            {
                "run_id": run_id,
                "session_name": session_name,
                "worktree": worktree,
                "candidate_head_sha": candidate_head_sha,
            }
        )
        if self.raises is not None:
            raise self.raises
        return TechLeadCompletionValidation.concluded(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=self.bind_to or candidate_head_sha,
            status=self.status,
            detail=self.detail,
        )


def passing_completion_validator() -> StubTechLeadCompletionValidator:
    """The ordinary case: the trusted owner ran the gate and it passed."""
    return StubTechLeadCompletionValidator(
        detail="the checkout is clean (dirty_check='tracked')"
    )
