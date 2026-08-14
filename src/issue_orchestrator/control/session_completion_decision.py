"""Binding a finished session's completion decision.

The tick thread decides *what* a terminated session's outcome call will be;
running it is slow (git, GitHub, the validation gate) and may happen off-thread
through the completion dispatcher. This module owns that binding, and with it
the one piece of policy the binding carries: which issue identity the decision's
attempt-scoped evidence is filed under.

Split out of ``session_completion``, which owns applying the resulting outcome
— same split as ``session_completion_diagnostics``.
"""

import logging
from typing import TYPE_CHECKING, Callable

from ..domain.issue_key import IssueKey, github_issue_key
from ..domain.models import Session
from ..infra.config import Config

if TYPE_CHECKING:
    from ..observation.observation import SessionObservationResult
    from .session_controller import SessionController, SessionDecision

logger = logging.getLogger(__name__)


def validation_issue_key(session: Session, config: Config) -> IssueKey | None:
    """The validation attempt's issue identity, derived the one canonical way.

    This is the attempt identity validation evidence is filed under, so it must
    be the key every other attempt-scoped record for the same issue uses. It
    therefore goes through ``github_issue_key`` — the one owner of the rule
    (#34) — rather than spelling out a number-only key: for a title carrying a
    stable-id prefix the two spellings disagree, and validation evidence would
    land under ``(repo, "38", A)`` while the review and execution-principal
    halves of the same attempt land under ``(repo, "M1-011", A)`` (#40).
    """
    repo = session.issue.repo or config.repo
    if repo:
        return github_issue_key(
            repo=repo,
            number=session.issue.number,
            title=session.issue.title,
        )
    if config.is_validation_enabled():
        logger.info(
            "[COMPLETION] Validation attempt identity unavailable: repo is unset "
            "for issue %s",
            session.issue.number,
        )
    return None


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
    issue_key = validation_issue_key(session, config)
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
