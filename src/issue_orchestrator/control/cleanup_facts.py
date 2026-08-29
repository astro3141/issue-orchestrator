"""Own the reading of "what cleanup does this state have?".

Cleanup comes in two shapes, and the difference is the whole point of this
module. A **deferred** cleanup is waiting on a live review-workflow question —
has this PR been reviewed yet? — which only the repository host can answer. An
**immediate** cleanup is the terminal disposal a finished session already
earned, and it needs no question asked of anyone.

Both readings share one interpretation of the cleanup configuration, so a
disposal can never obey a different tab/worktree policy depending on which
reading produced it. And because a paused engine may act on the immediate half
alone (#167), the immediate reading is offered on its own — absent the deferred
queue by construction rather than by filtering, and performing no repository
read at all, so a tick that fetches nothing still gets an answer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple, Optional

from ..ports.repository_host import RepositoryHostError
from .tech_lead_artifact_retention import tech_lead_problem_artifact_hold_issue_numbers

if TYPE_CHECKING:
    from ..domain.models import CleanupFacts, OrchestratorState
    from ..infra.config import Config
    from ..ports.repository_host import RepositoryHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


class CleanupPolicy(NamedTuple):
    """How this repository's configuration says cleanup behaves."""

    #: Every label that means "the review workflow has finished with this PR".
    #: A tuple rather than one label because under tech_lead there are two, and
    #: a gate that waited on the merge-facing one alone would wait forever on
    #: every candidate a batch stopped or refused (#345).
    reviewed_labels: tuple[str, ...]
    close_tabs: bool
    remove_worktrees: bool


def cleanup_policy(config: "Config") -> CleanupPolicy:
    """Read the workflow's cleanup configuration exactly once.

    Which review workflow is configured decides both the labels a deferred
    cleanup waits for and the tab/worktree settings every cleanup obeys.

    The tech_lead labels are ASKED of
    :class:`~.tech_lead_candidate_policy.TechLeadCandidatePolicy` rather than
    read from config here. That owner already decides which labels take a pull
    request out of the batch watch set; deriving "tech_lead is done with this
    PR" a second time is how the per-tick cleanup gate and the disposition
    planner would come to disagree about the same pull request.
    """
    from .tech_lead_candidate_policy import TechLeadCandidatePolicy

    if config.tech_lead_enabled:
        return CleanupPolicy(
            tuple(
                label
                for label in TechLeadCandidatePolicy.terminal_labels_for(config)
                if label
            ),
            config.cleanup.with_tech_lead.close_ai_session_tabs,
            config.cleanup.with_tech_lead.remove_worktrees,
        )
    without = config.cleanup.without_tech_lead
    if config.code_review_agent:
        return CleanupPolicy(
            tuple(label for label in (config.code_reviewed_label,) if label),
            without.close_ai_session_tabs,
            without.remove_worktrees,
        )
    # No review workflow: nothing to defer on, defaults for immediate cleanup.
    return CleanupPolicy((), without.close_ai_session_tabs, without.remove_worktrees)


def gather_cleanup_facts(
    state: "OrchestratorState",
    config: "Config",
    repository_host: "RepositoryHost",
    tech_lead_authority: "TechLeadAuthorityStore | None" = None,
) -> Optional["CleanupFacts"]:
    """Both cleanup readings, for the Planner to decide on.

    Returns immutable facts; performs no cleanup — that is the Planner's job,
    and the applier's after it. ``None`` when there is nothing to clean up.
    """
    from ..domain.models import CleanupFacts

    has_pending = bool(state.pending_cleanups)
    if not has_pending and not state.immediate_cleanups:
        return None

    policy = cleanup_policy(config)
    reviewed_pr_numbers: frozenset[int] = frozenset()
    if has_pending and policy.reviewed_labels:
        reviewed_pr_numbers = _reviewed_pr_numbers(
            repository_host, policy.reviewed_labels
        )

    return CleanupFacts(
        pending_cleanups=tuple(
            (c.issue_number, c.pr_number, c.terminal_id, str(c.worktree_path))
            for c in state.pending_cleanups
        ),
        reviewed_pr_numbers=reviewed_pr_numbers,
        close_tabs=policy.close_tabs,
        remove_worktrees=policy.remove_worktrees,
        immediate_cleanups=tuple(state.immediate_cleanups),
        held_issue_numbers=tech_lead_problem_artifact_hold_issue_numbers(
            state, config, tech_lead_authority
        ),
    )


def gather_terminal_disposal_facts(
    state: "OrchestratorState",
    config: "Config",
    tech_lead_authority: "TechLeadAuthorityStore | None" = None,
) -> Optional["CleanupFacts"]:
    """ONLY the terminal disposal a finished session already earned (#167).

    The immediate half of :func:`gather_cleanup_facts`, and deliberately
    nothing else: this is what a paused engine may act on, and the deferred
    queue's live review-workflow question stays behind the pause gate.
    """
    from ..domain.models import CleanupFacts

    if not state.immediate_cleanups:
        return None

    policy = cleanup_policy(config)
    return CleanupFacts(
        pending_cleanups=(),
        reviewed_pr_numbers=frozenset(),
        close_tabs=policy.close_tabs,
        remove_worktrees=policy.remove_worktrees,
        immediate_cleanups=tuple(state.immediate_cleanups),
        held_issue_numbers=tech_lead_problem_artifact_hold_issue_numbers(
            state, config, tech_lead_authority
        ),
    )


def _reviewed_pr_numbers(
    repository_host: "RepositoryHost", reviewed_labels: tuple[str, ...]
) -> frozenset[int]:
    """PRs carrying ANY cleanup label; an unreadable board reviews nothing.

    One read per label, and only on a tick that already has deferred cleanups
    waiting, so the tech_lead pair costs one extra read over the single-label
    form — bounded, and the alternative is a queue entry that can never drain.

    A repository-host failure propagates — the caller's resilience owns it —
    while any other failure leaves the deferred cleanups waiting rather than
    releasing them on an answer nobody obtained.
    """
    numbers: set[int] = set()
    for reviewed_label in reviewed_labels:
        try:
            prs = repository_host.get_prs_with_label(reviewed_label)
        except RepositoryHostError:
            raise
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch PRs with label {reviewed_label}: {e}")
            return frozenset()
        numbers.update(pr.number for pr in prs)
    return frozenset(numbers)
