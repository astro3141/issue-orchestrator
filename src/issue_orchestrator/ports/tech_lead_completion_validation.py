"""Port for the trusted Tech Lead completion-validation owner (#385).

The Tech Lead completion protocol used to hand the model a command whose
correctness depends on writes to the repository's shared git common dir, which
a bounded Tech Lead sandbox does not grant. This port is the seam that owns
that work instead: the control plane asks it for a verdict on one run, and the
implementation — which lives outside the model session, in the orchestrator's
own process — executes the check, owns its host/common-dir effects, and files
durable evidence bound to the exact candidate.

Constructed once at the composition root (``entrypoints/bootstrap.py``) and
injected into the completion processor. Tests mock this protocol; the real
implementation lives in ``infra/tech_lead_completion_validation.py``.

**The port never raises to express a verdict.** Every way the check can end —
including "the owner could not run it" — is one of the statuses on
:class:`~..domain.tech_lead_completion_validation.
TechLeadCompletionValidationStatus`, so a caller cannot mistake a failure to
validate for an absence of anything to validate. An implementation that raises
anyway is still safe: the control-plane policy treats an exception as
``UNAVAILABLE`` and refuses the completion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.tech_lead_completion_validation import TechLeadCompletionValidation

__all__ = ["TechLeadCompletionValidator"]


class TechLeadCompletionValidator(Protocol):
    """Executes a Tech Lead run's completion validation outside the sandbox."""

    def validate_completion(
        self,
        *,
        run_id: str,
        session_name: str,
        worktree: Path,
        candidate_head_sha: str,
    ) -> TechLeadCompletionValidation:
        """Validate one completed Tech Lead run and file durable evidence.

        Args:
            run_id: The orchestrator-allocated run identity.
            session_name: The session within that run.
            worktree: The checkout the run finished in — read by the trusted
                owner, never by the model.
            candidate_head_sha: The commit the ORCHESTRATOR observed the
                checkout at. Passed in rather than re-derived so the evidence
                is bound to the candidate the caller is about to settle; an
                implementation that reads a different head must say so with a
                non-``PASSED`` status rather than quietly rebind.

        Returns:
            Evidence as it stands in the durable record after this call —
            which is what makes "the record could not be written or read back"
            an ``UNAVAILABLE`` verdict rather than an unnoticed no-op.
        """
        ...
