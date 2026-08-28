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

**The order the five are asked in is deliberate, and it is not cheapest-first.**
Facts 3 and 4 are two local git reads that a code-bearing completion now pays
before fact 5 refuses it, and putting the commit count first would save them.
It would also cost the answer: a checkout with uncommitted tracked work is
refused for that reason and the operator is told which files, whereas asked
after a non-zero count the same run would be reported as merely code-bearing.
The reads are local and sub-second; the refusal an operator has to act on is
not, so the message wins.

**A settlement that shaped a plan and told nobody what it proved would be half
a settlement.** These five facts are the strongest statement the completion
pipeline can make about a run — clean tracked content AND zero commits over the
base the pull request would target — and two phases downstream decide from
weaker evidence unless they are told: the code-validation gate would run over a
commit this run did not produce, and the completion planner would release the
run's claim label with nothing to take its place. So the proof leaves here on
:class:`~.completion_types.CompletionSettlement`, carried on the existing
contract rather than re-derived from a role name or the shape of a completion
record that says the opposite.

**A sixth fact is required, and it cannot be known when the lane is chosen.**
The five above prove the run had nothing but a comment to deliver; they do NOT
prove the comment WAS delivered, and the difference decides whether a work item
may be closed. The settler runs before the actions — it is what shapes them —
so :func:`confirm_result_only_delivery` holds the settled lane to the same
standard afterwards, and an unconfirmed delivery is a refusal there exactly as
an unreadable checkout is here.

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
from .completion_types import (
    CodeCandidateSettlement,
    CompletionSettlement,
    ERROR_PREFIX_RESULT_UNDELIVERED,
    ResultOnlyDelivery,
)
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

    The two properties below are what the phases AFTER execution must be told.
    They are not extra derivations: a settlement that shaped a plan and told
    nobody what it proved would leave the pipeline's strongest statement about
    this run — clean tracked content and zero commits over the PR base — as a
    log line, and both readers would go on deciding from the shape of a
    completion record that says the opposite.
    """

    zero_code: bool
    detail: str
    requested_actions: tuple[RequestedAction, ...]

    @property
    def settlement(self) -> CompletionSettlement:
        """Everything this proof tells the phases downstream of execution."""
        return CompletionSettlement(
            code_candidate=self.code_candidate,
            result_only=self.result_only,
        )

    @property
    def code_candidate(self) -> CodeCandidateSettlement:
        """What a downstream code-validation gate is left to judge (#328).

        The very contract the tech_lead completion owner produces, from
        STRONGER evidence: that owner proves a planning run stood at its launch
        base, and this one proves clean tracked content AND zero commits over
        the base a pull request would target. Carried on the one contract
        rather than restated, so a settled run cannot have candidate-shaped
        PASS evidence written for a commit it did not produce — nor be flipped
        into the validation-retry path and relaunched as a coder retry against
        an empty branch, which is the failure this whole lane repairs.
        """
        if self.zero_code:
            return CodeCandidateSettlement.settled_zero_code(self.detail)
        return CodeCandidateSettlement.presented()

    @property
    def result_only(self) -> ResultOnlyDelivery:
        """Whether the comment this run posts is its whole delivery (#337).

        A settled run publishes no branch and opens no pull request, so nothing
        downstream would ever stamp the ``pr-pending`` that holds a finished
        issue. Told, the completion planner can give the run a terminal
        disposition; untold, it releases the claim label and the finished work
        item falls straight back into the schedulable pool.
        """
        if self.zero_code:
            return ResultOnlyDelivery.settled(self.detail)
        return ResultOnlyDelivery.none()


@dataclass(frozen=True, slots=True)
class SettledOrdinaryPublication:
    """What this run may still EXECUTE, and what the settling PROVED.

    Both halves leave the owner together because both are consequences of the
    same five facts. Returning only the plan is what an earlier round of #337
    did, and the proof was logged and dropped at the door.
    """

    plan: PublishPipelinePlan
    settlement: CompletionSettlement


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
) -> SettledOrdinaryPublication:
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

    A run governed elsewhere settles to :meth:`CompletionSettlement.unsettled`
    — NOT to a proof of its own. Its own owner has already answered both
    questions this settlement carries, and answering them a second time here
    would be exactly the cross-path drift the carried contract exists to
    prevent.
    """
    if governed_elsewhere:
        return SettledOrdinaryPublication(plan, CompletionSettlement.unsettled())
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
        return SettledOrdinaryPublication(plan, settlement.settlement)
    return SettledOrdinaryPublication(
        replace(plan, ordered_actions=settlement.requested_actions),
        settlement.settlement,
    )


@dataclass(frozen=True, slots=True)
class ConfirmedResultDelivery:
    """The settlement as EXECUTION left it, and the failure to report if any.

    ``publish_failure`` is ``None`` on every path but one: a run that took the
    zero-code lane and whose comment did not land. The string is returned
    rather than appended to a caller's error list here, so this module stays a
    decision and the caller keeps ownership of its own accumulators.
    """

    settlement: CompletionSettlement
    publish_failure: str | None


def confirm_result_only_delivery(
    settlement: CompletionSettlement,
    *,
    result_delivered: bool,
    issue_number: int,
) -> ConfirmedResultDelivery:
    """Hold the settled lane to the standard the rest of this module keeps.

    Every one of the five facts above is PROVEN before the lane is taken. The
    sixth is not knowable then: whether the comment the lane preserved actually
    reached the issue can only be answered after the actions run, and the
    settler cannot wait for that — it is what shapes those actions.

    So it is answered here, and an unproven answer is a refusal exactly as it
    is upstream. Three reachable ways a settled run delivers nothing, none of
    which raise past the completion:

    - ``add_comment`` fails. The forge rejects a body over its comment size
      limit, rate-limits, or 5xxs. ``post_comment`` is not a publication
      action, so the failure neither halts the remaining actions nor counts as
      critical anywhere today;
    - the record requests ``post_comment`` and carries no ``comment_body``.
      Nothing requires one, and a completion record is untrusted agent input by
      this repository's own principle — "the CLI always sets it" is not a
      guard. The action then posts nothing and reports nothing;
    - the record never requested ``post_comment`` at all. Dropping the two
      publication actions leaves an EMPTY plan, and an empty plan trivially
      "succeeds".

    In all three the run is finished, has published nothing, and would be
    handed the terminal disposition — closing a work item whose deliverable
    never arrived, which is the inverse of what this lane exists to do and
    worse than the bounded failure it replaced. Withdrawing the disposition
    alone would only put such a run back in the schedulable pool unbounded, so
    the failure is named as what it is: on this lane the comment IS the
    publication, and its loss routes to the bounded publish-failure owner that
    counts failures and escalates to ``needs-human``.

    ``code_candidate`` is deliberately left standing — see
    :meth:`~.completion_types.CompletionSettlement.undelivered`.
    """
    if not settlement.result_only.delivered or result_delivered:
        return ConfirmedResultDelivery(settlement, None)
    failure = (
        f"{ERROR_PREFIX_RESULT_UNDELIVERED}: the run for issue #{issue_number}"
        " offered no code candidate, so its issue comment was its whole"
        " delivery, and that comment did not reach the issue; nothing was"
        " published"
    )
    logger.error(
        "Result-only delivery failed for issue #%d; withdrawing the terminal"
        " disposition and reporting a publish failure (%s)",
        issue_number,
        settlement.result_only.detail,
    )
    return ConfirmedResultDelivery(settlement.undelivered(), failure)


__all__ = [
    "ConfirmedResultDelivery",
    "OrdinaryZeroCodeReader",
    "SettledOrdinaryPublication",
    "ZeroCodeOrdinarySettlement",
    "confirm_result_only_delivery",
    "settle_ordinary_publication_plan",
    "settle_zero_code_ordinary_completion",
]
