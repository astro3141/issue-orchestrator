"""The fail-closed gate a Tech Lead completion passes before it settles (#385).

#370 established that a Tech Lead's mandatory *repository* validation belongs to
the orchestrator. The completion protocol the model was handed had not caught
up: it still required the session to run ``prepush-check --dirty-only -v``,
whose timing record lands under the repository's shared git common dir — a
write a bounded Tech Lead sandbox does not grant. The measured consequence was
not a weaker gate but no completion at all.

Moving the command off the model is only half a repair. Deleting an instruction
would be a waiver, so this module is the other half: the completion is still
gated, on evidence a TRUSTED owner produced outside the model sandbox, and the
completion cannot settle without it.

**Nothing here validates anything.** The check itself, and the host/common-dir
effects it owns, belong to
:class:`~..ports.tech_lead_completion_validation.TechLeadCompletionValidator`.
What lives here is the one question the control plane asks — *may this
completion settle on what that owner filed?* — and the single direction it
fails in.

**Every way of not knowing refuses.** A head that cannot be read, an owner that
is not wired, an owner that failed to produce a verdict, evidence that names a
different run or a different commit, and evidence that did not pass are all one
answer: refuse. The alternative — admitting a Tech Lead completion whose
validation is merely unproven — is the exact thing a merge-facing PASS must
never rest on.

That is not the same as swallowing everything: see :data:`_NO_VERDICT_ERRORS`
for which failures are "no evidence" and which are a composition bug that must
still crash.

**The model cannot manufacture the answer.** The evidence is filed by the
trusted owner into orchestrator-owned state outside the session's write roots,
and it is compared against the head the ORCHESTRATOR read, not against anything
the completion record claims. A session that rewrites its own worktree copies
changes nothing this gate reads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.tech_lead_completion_validation import (
    TechLeadCompletionValidation,
    TechLeadCompletionValidationStatus,
)
from ..ports.tech_lead_completion_validation import TechLeadCompletionValidator
from .completion_types import ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION
from .zero_code_reads import ZeroCodeWorktreeReader

logger = logging.getLogger(__name__)

__all__ = [
    "UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR",
    "UnwiredTechLeadCompletionValidator",
    "require_trusted_completion_validation",
]


class UnwiredTechLeadCompletionValidator:
    """The default when no trusted owner is wired: refuse, do not fail-safe.

    A null object rather than an optional, and an ``UNAVAILABLE`` verdict
    rather than a raise. The verdict is the honest description of the state —
    the validation was not produced — and it routes through exactly the same
    refusal the other unavailable causes take, so a composition that forgot the
    owner behaves like one whose owner could not answer instead of like one
    whose owner said yes.
    """

    def validate_completion(
        self,
        *,
        run_id: str,
        session_name: str,
        worktree: Path,
        candidate_head_sha: str,
    ) -> TechLeadCompletionValidation:
        return TechLeadCompletionValidation.concluded(
            run_id=run_id,
            session_name=session_name,
            candidate_head_sha=candidate_head_sha,
            status=TechLeadCompletionValidationStatus.UNAVAILABLE,
            detail=(
                "no trusted Tech Lead completion-validation owner is wired"
                " (entrypoints/bootstrap.py), so the mandatory completion"
                " validation was never executed"
            ),
        )


UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR: TechLeadCompletionValidator = (
    UnwiredTechLeadCompletionValidator()
)


#: What a trusted owner failing to produce a verdict looks like from here.
#: Deliberately NOT a blind ``except Exception``: an owner that could not reach
#: the repository, could not parse what it read, timed out, or reported a bad
#: internal state has produced "no evidence", which is a refusal. Anything else
#: — a ``TypeError`` from a mis-wired port, say — is a bug in the composition,
#: and this repo's fail-fast stance says a bug must crash where it is rather
#: than be laundered into a governed verdict.
_NO_VERDICT_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


def _refusal(failure: str, detail: str) -> str:
    """One tagged error shape, parsed back by the terminal-effects path."""
    return f"{ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION}: {failure}: {detail}"


def require_trusted_completion_validation(
    *,
    validator: TechLeadCompletionValidator,
    run_id: str,
    session_name: str,
    worktree: Path,
    worktree_reader: ZeroCodeWorktreeReader,
) -> str | None:
    """The tagged refusal for this completion, or ``None`` when it may settle.

    Args:
        validator: The trusted owner that executes the validation outside the
            model sandbox and files durable evidence.
        run_id: The run this completion belongs to.
        session_name: The session within that run.
        worktree: The checkout the run finished in.
        worktree_reader: The orchestrator's own read of that checkout. The head
            it reports is the candidate the evidence must be bound to — the
            completion record is never asked what commit it stands on.

    Returns:
        ``None`` when a trusted, exactly-bound PASS is in hand; otherwise the
        tagged error the caller records as the completion's rejection.
    """
    head = worktree_reader.get_head_sha(worktree)
    if not head:
        return _refusal(
            "candidate_unreadable",
            f"the commit {worktree} stands at could not be read, so no trusted"
            " completion validation can be bound to a candidate",
        )
    try:
        validation = validator.validate_completion(
            run_id=run_id,
            session_name=session_name,
            worktree=worktree,
            candidate_head_sha=head,
        )
    except _NO_VERDICT_ERRORS as exc:
        logger.warning(
            "[TECH_LEAD] trusted completion validation raised for run %s/%s: %s",
            run_id,
            session_name,
            exc,
        )
        return _refusal(
            "validation_unavailable",
            "the trusted completion-validation owner failed to produce a"
            f" verdict for {run_id}/{session_name}: {exc}",
        )
    if not validation.binds_to(
        run_id=run_id, session_name=session_name, candidate_head_sha=head
    ):
        return _refusal(
            "candidate_drift",
            "the trusted completion validation is bound to"
            f" {validation.run_id}/{validation.session_name}@"
            f"{validation.candidate_head_sha}, not to the"
            f" {run_id}/{session_name}@{head} this completion settles",
        )
    if not validation.permits_completion:
        return _refusal(
            f"validation_{validation.status.value}",
            f"the trusted completion validation for {run_id}/{session_name}@"
            f"{head} did not pass: {validation.detail}",
        )
    logger.info(
        "[TECH_LEAD] trusted completion validation PASSED for %s/%s@%s: %s",
        run_id,
        session_name,
        head,
        validation.detail,
    )
    return None
