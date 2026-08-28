"""Whether an ordinary run that finished has anything to publish to a branch.

An ordinary work item does not have to change code to be finished. A
measurement, an audit, a read-only investigation filed against the product repo
produces a RESULT and no commit — and the completion CLI hands every ordinary
``completed`` the same publication intent, ``push_branch`` then ``create_pr``
then ``post_comment``. A run with no commit cannot complete ``create_pr``: the
forge refuses to open a pull request for a branch that adds nothing. The
completion is then marked failed, and ``post_comment`` — third in the tuple, and
the ONLY action that publishes the result — never executes.

That is the whole of the measured defect (#336): an already-produced, validated,
independently reviewed RESULT, durably recorded against the exact candidate, and
unable to reach the issue because publishing it was sequenced behind an action a
zero-commit run can never complete.

This module answers one question — *does this run offer a branch anything?* —
and, when the answer is a proven no, removes the publication intent the run
never meant. ``post_comment`` is deliberately NOT removed: unlike the tech_lead
planning lane, whose prompts promise the orchestrator posts no comment, the
issue comment is exactly what an ordinary evidence run exists to deliver.

**The answer must be PROVEN, never assumed.** Five facts are required, and the
absence of any one of them is a refusal rather than a benefit of the doubt:

1. the run reported ``COMPLETED``. Every other outcome is either already
   comment-bearing without a PR or is a report of failure, and neither is this
   lane's business;
2. it still carries publication intent — there is something to drop;
3. the completion-time HEAD read succeeded;
4. the tracked-dirt enumeration succeeded and found nothing;
5. the branch-vs-base commit count read succeeded and is zero.

Unobservable is never read as zero-code, for the reason
:mod:`.candidate_integrity` states about the same reads: a checkout whose state
could not be read is not a checkout that was proven unchanged.

**Fact 5 is the ordinary analogue of the planning lane's launch-base equality,
not a copy of it.** :mod:`.tech_lead_zero_code` proves a planning run stood
still by comparing its HEAD against the base the ORCHESTRATOR launched it on —
a record that exists because a tech_lead run is admitted under a launch
authority. An ordinary run has no such record, and inventing one would make the
proof depend on a launch-time write surviving a restart. Asking the branch what
it contributes over the base the pull request would target is both available
without any new durable state and strictly closer to the question: ``0`` is
precisely the condition under which ``create_pr`` cannot succeed.

**Ordering is load-bearing and belongs to the caller.** The intent is dropped
only AFTER everything that judges the candidate has already run and passed —
the publish gate, the independent review exchange, the pre-publish gate. A
completion settled out of the publication path before those would buy itself a
skipped gate and an unreviewed result, which is the opposite of what this lane
is for: the evidence must be reviewed and its receipts durable, and only the
branch write it never needed is dropped.

**Neither vocabulary this module uses is re-answered here.** *Which actions
publish* belongs to :data:`~..domain.models.PUBLICATION_ACTIONS`, and the
dropping is stated as intent via
:func:`~..domain.models.without_publication_intent`, so an action that joins the
publication family joins it for this lane too, in one edit. *What counts as
dirt* belongs to ``list_dirty_files`` and is asked for
:data:`~.candidate_integrity.CANDIDATE_DIRT_MODE`, the same tracked-content
question the candidate postflight asks — untracked files are excluded at the
source, because a session legitimately writes untracked artifacts into its own
checkout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from ..domain.models import (
    CompletionOutcome,
    PUBLICATION_ACTIONS,
    RequestedAction,
    without_publication_intent,
)
from ..ports.working_copy import BranchCommitsResult
from .candidate_integrity import CANDIDATE_DIRT_MODE
from .review_publish_pipeline import PublishPipelinePlan
from .zero_code_reads import ZeroCodeWorktreeReader, summarise_dirt

logger = logging.getLogger(__name__)


class OrdinaryZeroCodeReader(ZeroCodeWorktreeReader, Protocol):
    """The planning lane's two reads, plus the one this lane decides on.

    Declared as an EXTENSION rather than a second flat protocol so the two
    shared reads have one definition, and so the read this lane adds is visible
    as the extra thing it is. Satisfied by the completion path's existing git
    adapter, like its parent.
    """

    def commits_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchCommitsResult:
        """How many commits the branch adds over ``base_ref``."""
        ...


@dataclass(frozen=True, slots=True)
class ZeroCodeOrdinarySettlement:
    """What a finished ordinary run's publication intent settles to.

    ``requested_actions`` is what the caller should carry forward: the shaped
    tuple when the zero-code lane applies, and the caller's own tuple untouched
    when it does not. ``detail`` always says why, so an operator reading the log
    of a run that took the ordinary path sees which fact was missing — and the
    log of one that did not says which commit it stood at and against what.
    """

    zero_code: bool
    detail: str
    requested_actions: tuple[RequestedAction, ...]


def settle_zero_code_ordinary_completion(
    *,
    outcome: CompletionOutcome,
    requested_actions: tuple[RequestedAction, ...],
    worktree: Path,
    base_ref: str,
    worktree_reader: OrdinaryZeroCodeReader,
) -> ZeroCodeOrdinarySettlement:
    """Decide the lane for one ordinary run that has cleared every gate.

    Args:
        outcome: What the run reported. Only ``COMPLETED`` can settle here.
        requested_actions: What is still to be executed, after the review
            exchange and every publication precondition have passed.
        worktree: The run's checkout, read as it is right now.
        base_ref: The ref a pull request for this branch would target, as the
            caller resolves it (``origin/<base branch>``). Supplied rather than
            derived, because which base a candidate publishes onto is the
            completion path's answer and must not be guessed twice.
        worktree_reader: The three reads above.

    Returns:
        The settlement. Anything not fully proven gets its requested actions
        back unchanged and keeps today's behaviour, including every kind of run
        this lane does not govern.
    """
    refusal = _zero_code_refusal(
        outcome,
        requested_actions,
        worktree=worktree,
        base_ref=base_ref,
        worktree_reader=worktree_reader,
    )
    if refusal is not None:
        return ZeroCodeOrdinarySettlement(False, refusal, requested_actions)
    return ZeroCodeOrdinarySettlement(
        True,
        (
            f"the branch in {worktree} adds no commit over {base_ref} and its"
            " tracked content is clean; the run offers no code candidate, so"
            " its branch publication intent is dropped and its reviewed result"
            " is published to the issue"
        ),
        without_publication_intent(requested_actions),
    )


def _zero_code_refusal(
    outcome: CompletionOutcome,
    requested_actions: tuple[RequestedAction, ...],
    *,
    worktree: Path,
    base_ref: str,
    worktree_reader: OrdinaryZeroCodeReader,
) -> str | None:
    """The first missing fact, or ``None`` when all five are in hand."""
    landed = outcome is CompletionOutcome.COMPLETED
    if not landed:
        return (
            f"run outcome is {outcome.value}; the ordinary zero-code lane is"
            f" {CompletionOutcome.COMPLETED.value} only"
        )
    publishes = bool(PUBLICATION_ACTIONS & set(requested_actions))
    if not publishes:
        return "the run asks for no branch publication, so there is none to drop"
    head = worktree_reader.get_head_sha(worktree)
    if head is None:
        return f"the commit {worktree} stands at could not be read"
    dirt = worktree_reader.list_dirty_files(worktree, CANDIDATE_DIRT_MODE)
    if dirt is None:
        return f"the tracked changes in {worktree} could not be enumerated"
    if dirt:
        return f"tracked content is modified in {worktree}: {summarise_dirt(dirt)}"
    commits = worktree_reader.commits_against_base(worktree, base_ref)
    if not commits.success:
        return (
            f"what {worktree} adds over {base_ref} could not be counted:"
            f" {commits.error or 'unknown git error'}"
        )
    if commits.count:
        return (
            f"the branch at {head} adds {commits.count} commit(s) over"
            f" {base_ref}; it offers a code candidate"
        )
    return None


def settle_ordinary_publication_plan(
    *,
    plan: PublishPipelinePlan,
    outcome: CompletionOutcome,
    worktree: Path,
    base_ref: str,
    worktree_reader: OrdinaryZeroCodeReader,
    governed_elsewhere: bool,
    issue_number: int,
) -> PublishPipelinePlan:
    """The execution plan this run may still carry out, and the trace of why.

    The command form of :func:`settle_zero_code_ordinary_completion` for the
    one caller that holds an execution plan: it shapes the plan's actions
    rather than the completion record, because the record is the agent's
    reported intent and every earlier phase — the publish gate, the review
    exchange, the pre-publish gate — was keyed off it. Rewriting it here would
    retroactively change what those phases were judged against; the plan is
    what is still to happen, and that is what a settlement may change.

    ``governed_elsewhere`` is the caller's statement that another owner already
    settled this run's publication intent — the tech_lead lane, at the
    pre-action seam. Such a run is handed its plan back untouched: a clean
    batch audit's empty-branch push and the benign ``create_pr`` refusal that
    follows it are that lane's settled behaviour, not this one's to re-decide.
    The flag is passed rather than derived so tech_lead role policy stays with
    its owner instead of being restated here.
    """
    if governed_elsewhere:
        return plan
    settlement = settle_zero_code_ordinary_completion(
        outcome=outcome,
        requested_actions=plan.ordered_actions,
        worktree=worktree,
        base_ref=base_ref,
        worktree_reader=worktree_reader,
    )
    logger.info(
        "Ordinary publication lane for issue #%d: zero_code=%s (%s)",
        issue_number,
        settlement.zero_code,
        settlement.detail,
    )
    if not settlement.zero_code:
        return plan
    return replace(plan, ordered_actions=settlement.requested_actions)


__all__ = [
    "OrdinaryZeroCodeReader",
    "ZeroCodeOrdinarySettlement",
    "settle_ordinary_publication_plan",
    "settle_zero_code_ordinary_completion",
]
