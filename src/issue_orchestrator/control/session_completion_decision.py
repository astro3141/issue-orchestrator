"""Binding a finished session's completion decision.

The tick thread decides *what* a terminated session's outcome call will be;
running it is slow (git, GitHub, the validation gate) and may happen off-thread
through the completion dispatcher. This module owns that binding, and with it
the one piece of policy the binding carries: which issue identity the decision's
attempt-scoped evidence is filed under.

Split out of ``session_completion``, which owns applying the resulting outcome
— same split as ``session_completion_diagnostics``.
"""

from typing import TYPE_CHECKING, Callable

from ..domain.issue_key import IssueKey
from ..domain.models import Session
from ..infra.config import Config

if TYPE_CHECKING:
    from ..observation.observation import SessionObservationResult
    from .session_controller import SessionController, SessionDecision


def validation_issue_key(session: Session) -> IssueKey:
    """The validation attempt's issue identity: the session's own.

    This is the key validation evidence for a candidate is filed under, so it
    must be the key every other attempt-scoped record for the same work item
    uses. ``Session`` already owns that identity in ``key.issue`` — it is what
    the session was launched under, what its durable ``PendingWorkClaim`` row
    is keyed by, and (since #40) what restoration rebuilds it with — so the
    completion path asks the owner rather than re-deriving from a sibling
    field.

    Re-deriving from ``session.issue`` is what this replaces, and it was wrong
    for more than the spelling: on the rework and review launch paths that
    field is a *synthetic* work item (``Issue(38, "Rework #99")``, no repo), so
    a title-aware derivation over it still yields ``(repo, "38", A)`` while the
    session, its claim and the issue's coding-attempt records all use
    ``(repo, "M1-011", A)``. Rework is not ``is_review_only``, so it really does
    reach the validation gate. Asking the owner cannot drift that way: the key
    is canonical by construction at every site that builds a ``Session``.
    """
    return session.key.issue


def completion_decider(
    session_controller: "SessionController",
    session: Session,
    obs: "SessionObservationResult",
    config: Config,
) -> "Callable[[], SessionDecision]":
    """Bind a no-arg call to decide this session's outcome.

    Cheap per-session inputs (issue key, retry template) are computed now on the
    tick thread; the returned callable performs the slow git/GitHub/validation
    work and may run off-thread.
    """
    issue_key = validation_issue_key(session)
    retry_prompt_template = (
        session.agent_config.retry_prompt_template or config.retry.retry_prompt_template
    )

    def decide() -> "SessionDecision":
        return session_controller.decide_outcome(
            obs, session.worktree_path, session.issue.number,
            session.issue.title, session.terminal_id, session.completion_path,
            validation_retry_count=session.validation_retry_count,
            original_prompt=session.original_prompt,
            retry_prompt_template=retry_prompt_template,
            repo_root=config.repo_root,
            issue_key=issue_key,
            session_run_assets=session.run_assets,
            task_kind=session.key.task,
        )

    return decide
