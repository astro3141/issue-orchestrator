"""Ordinary rework, admitted in-process when a continuation exits to it (#297).

``ContinuationPhase.EXHAUSTED`` says in its own documentation that the candidate
"returns to ordinary rework with its evidence history intact", and #296 measured
what actually happened: reconciliation dropped every non-live phase from
ownership and produced nothing. The lease was released, the projection stopped
excluding the issue — and no rework was ever admitted, because the only producer
of one was a scan over PRs already carrying ``needs-rework``. A PR-backed
candidate that failed canonical publication validation and spent its same-SHA
allowance sat stranded until a human intervened, in an engine whose ordinary
rework lane was working the whole time.

This module is the missing producer, and it is deliberately the smallest thing
that can be one.

**It owns no policy of its own.** The phase stays a derived predicate
(:mod:`..domain.continuation_phase`); the PR identity and branch come from the
existing open-PR owner (:func:`..control.completion_pr_collision.get_open_pr_for_issue`);
the cycle number and the ceiling come from the shared rework-cycle owner
(:mod:`.rework_cycle_policy`), which the ordinary scanner decides through too.
What is left here is assembly: which exits are PR-backed, what evidence travels
with them, and where the resulting fact is filed.

**It is a fact producer, not an actuator.** It appends ``DiscoveredRework`` and
``DiscoveredEscalation`` exactly as the awaiting-merge reconciler does, and the
planner turns those into the same ``QueueReworkAction`` and
``EscalateToHumanAction`` the ordinary lane produces. No label is authority
here: the admission is derived from the continuation's durable facts and the
open PR, and removing a projection cannot change it.

**#195's PR-backed shield is untouched.** Nothing here releases a session-history
claim, and ``QueueCache.abandoned_candidates()`` still excludes the issue for
exactly the reason it did before. The transition this produces is
``continuation -> ordinary rework on the same PR lineage``, never
``continuation -> issue release``: the rework runs against the open PR's own
branch, and no fresh coding session is created for the issue.

**Repetition is bounded by what already bounds rework.** The same exit is
re-derived on every reconciliation for as long as the durable facts stand, so
the handoff asks the cycle owner whether anything already holds the issue
before it asks for anything else — a queued rework, a discovered one, or a
running session all refuse. Once a cycle actually starts, the rework launcher
writes ``rework-cycle-N`` to the PR, and that durable label is what makes a
restart re-derive the same bounded state instead of a second cycle.

The exit itself is not consumed, and deliberately so: consuming it would be the
stored continuation state machine the phase predicate exists to avoid. It stops
being derived when the durable facts change — a newer refused candidate
supersedes the recorded intent, or the corrected candidate publishes and the
board carries ``pr-pending`` again, which settles the phase. Between a
correction publishing and that label landing there is a window in which the
exit is still derivable, and the bound on it is the one the issue names: the
existing rework-cycle ceiling, counted by the same owner, escalating through
today's path. No new budget is introduced to close it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.models import DiscoveredEscalation, DiscoveredRework
from .completion_pr_collision import get_open_pr_for_issue
from .rework_cycle_policy import (
    ReworkAdmission,
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.attempt import Attempt
    from ..domain.models import OrchestratorState
    from ..ports.pull_request_tracker import PRInfo
    from .completion_pr_collision import CompletionPrAdapter
    from .continuation_live_truth import ContinuationReworkExit

logger = logging.getLogger(__name__)

CONTINUATION_EXIT_SOURCE = "continuation_rework_exit"
"""Names this producer on every fact it files.

Distinct from ``review_label`` and ``post_publish_validation`` so the rework
launcher's prompt, the dashboard and any later reader can tell which lane
handed the candidate over, without either of the other two sources changing
meaning.
"""


@dataclass(frozen=True, slots=True)
class ContinuationHandoffOutcome:
    """What the handoff decided about one exit, and why.

    Every exit produces one of these, including the refusals. A handoff that
    reported only its admissions would be indistinguishable from one that never
    ran, which is precisely the failure #296 measured.
    """

    issue_number: int
    verdict: ReworkAdmissionVerdict
    reason: str
    pr_number: int = 0
    rework_cycle: int = 0


@dataclass(frozen=True, slots=True)
class ContinuationHandoffResult:
    """Everything one handoff pass decided and filed."""

    outcomes: tuple[ContinuationHandoffOutcome, ...] = ()
    reworks: tuple[DiscoveredRework, ...] = ()
    escalations: tuple[DiscoveredEscalation, ...] = ()

    @property
    def admitted_issue_numbers(self) -> tuple[int, ...]:
        return tuple(rework.issue_number for rework in self.reworks)


class ContinuationReworkHandoff:
    """Admits ordinary rework for PR-backed candidates a continuation left."""

    def __init__(
        self,
        *,
        state: "OrchestratorState",
        pull_requests: "CompletionPrAdapter",
        budget: ReworkCycleBudget,
    ) -> None:
        #: The engine's live facts. Held rather than passed per call because
        #: what already holds an issue changes between reconciliations within
        #: one tick, and an owner deciding from a snapshot taken earlier would
        #: admit a second rework for a candidate it just admitted one for.
        self._state = state
        self._pull_requests = pull_requests
        self._budget = budget

    def admit(
        self, exits: Sequence["ContinuationReworkExit"]
    ) -> ContinuationHandoffResult:
        """File ordinary rework for every PR-backed exit that may take a cycle."""
        if not exits:
            return ContinuationHandoffResult()
        outcomes: list[ContinuationHandoffOutcome] = []
        reworks: list[DiscoveredRework] = []
        escalations: list[DiscoveredEscalation] = []
        for exit_ in exits:
            outcome = self._admit_one(exit_, reworks, escalations)
            outcomes.append(outcome)
        return ContinuationHandoffResult(
            outcomes=tuple(outcomes),
            reworks=tuple(reworks),
            escalations=tuple(escalations),
        )

    def _admit_one(
        self,
        exit_: "ContinuationReworkExit",
        reworks: list[DiscoveredRework],
        escalations: list[DiscoveredEscalation],
    ) -> ContinuationHandoffOutcome:
        issue = exit_.issue
        issue_number = issue.number
        # Asked first, and asked of the cycle owner rather than re-implemented
        # here: it needs nothing but process-local facts, and it is what stops
        # a re-derived exit from paying for a PR read on every reconciliation
        # once a rework is already queued or running.
        held = self._budget.already_held(
            issue_number,
            queued_issue_numbers=self._claimed_issue_numbers(),
            active_issue_numbers=self._active_issue_numbers(),
        )
        if held is not None:
            return _refused(issue_number, held)

        pr = get_open_pr_for_issue(self._pull_requests, issue_number)
        if pr is None:
            # The non-PR-backed exit. It is not this producer's case: with no
            # open PR there is no lineage to correct, and #195's own no-PR
            # recovery path is the owner of what happens next. Behaviour there
            # is unchanged precisely because nothing is filed here.
            return ContinuationHandoffOutcome(
                issue_number=issue_number,
                verdict=ReworkAdmissionVerdict.SKIP,
                reason="no_open_pr",
            )

        agent_type = issue.agent_type
        if not agent_type:
            # The same refusal the ordinary scanner makes, for the same reason:
            # a rework has to be launched as some agent, and guessing one would
            # run the wrong prompt against a real PR.
            logger.warning(
                "[CONTINUATION] issue #%d exits to rework but carries no agent "
                "label; PR #%d left for the ordinary lane",
                issue_number,
                pr.number,
            )
            return ContinuationHandoffOutcome(
                issue_number=issue_number,
                verdict=ReworkAdmissionVerdict.SKIP,
                reason="no_agent_label",
                pr_number=pr.number,
            )

        admission = self._budget.admit(
            issue_number=issue_number,
            pr_labels=pr.labels,
            issue_labels=list(issue.labels),
            queued_issue_numbers=self._claimed_issue_numbers(),
            active_issue_numbers=self._active_issue_numbers(),
        )
        if admission.verdict is ReworkAdmissionVerdict.SKIP:
            logger.info(
                "[CONTINUATION] not admitting rework for issue #%d PR #%d: %s",
                issue_number,
                pr.number,
                admission.reason,
            )
            return _refused(issue_number, admission, pr_number=pr.number)
        if admission.escalates:
            escalation = DiscoveredEscalation(
                issue_number=issue_number,
                pr_number=pr.number,
                rework_cycle=admission.rework_cycle,
            )
            # Through the collection's own owner, which carries the "once per
            # issue per tick" rule. Belt and braces with the budget's refusal
            # above, deliberately: the two answer at different moments, and the
            # one that cannot be skipped is the one on the write.
            if self._state.record_discovered_escalation(escalation):
                escalations.append(escalation)
            logger.info(
                "[CONTINUATION] issue #%d PR #%d has spent its rework cycles "
                "(next would be %d); escalating",
                issue_number,
                pr.number,
                admission.rework_cycle,
            )
            return _refused(issue_number, admission, pr_number=pr.number)

        rework = DiscoveredRework(
            issue_number=issue_number,
            pr_number=pr.number,
            branch_name=pr.branch,
            agent_type=agent_type,
            rework_cycle=admission.rework_cycle,
            source=CONTINUATION_EXIT_SOURCE,
            feedback=build_continuation_rework_feedback(
                pr=pr, attempt=exit_.attempt, phase_reason=exit_.phase.value
            ),
        )
        if not self._state.record_discovered_rework(rework):
            return ContinuationHandoffOutcome(
                issue_number=issue_number,
                verdict=ReworkAdmissionVerdict.SKIP,
                reason="already_queued",
                pr_number=pr.number,
                rework_cycle=admission.rework_cycle,
            )
        reworks.append(rework)
        logger.info(
            "[CONTINUATION] admitting ordinary rework for issue #%d on PR #%d "
            "(cycle %d) after the continuation exited at %s",
            issue_number,
            pr.number,
            admission.rework_cycle,
            exit_.phase.value,
        )
        return ContinuationHandoffOutcome(
            issue_number=issue_number,
            verdict=ReworkAdmissionVerdict.QUEUE,
            reason=admission.reason,
            pr_number=pr.number,
            rework_cycle=admission.rework_cycle,
        )

    def _claimed_issue_numbers(self) -> set[int]:
        """Issues a rework is already queued or discovered for.

        Both collections, because they are the same claim at two ages: the
        planner has not yet turned this tick's discovered facts into pending
        ones, and a second reconciliation inside the same tick would otherwise
        file a duplicate for a candidate it just filed one for.
        """
        claimed: set[int] = set()
        for pending in self._state.pending_reworks:
            issue_number = pending.resolve_issue_number()
            if issue_number is not None:
                claimed.add(issue_number)
        claimed.update(
            discovered.issue_number for discovered in self._state.discovered_reworks
        )
        claimed.update(
            escalation.issue_number
            for escalation in self._state.discovered_escalations
        )
        return claimed

    def _active_issue_numbers(self) -> set[int]:
        return {session.issue.number for session in self._state.active_sessions}


def _refused(
    issue_number: int, admission: ReworkAdmission, *, pr_number: int = 0
) -> ContinuationHandoffOutcome:
    return ContinuationHandoffOutcome(
        issue_number=issue_number,
        verdict=admission.verdict,
        reason=admission.reason,
        pr_number=pr_number,
        rework_cycle=admission.rework_cycle,
    )


def build_continuation_rework_feedback(
    *, pr: "PRInfo", attempt: "Attempt", phase_reason: str
) -> str:
    """The correction context that travels with an admitted rework.

    Everything here is copied from a durable record, never re-derived: the
    candidate SHA is the attempt's own key, the publish command and verdict come
    from the receipt the gate filed for that exact commit, the evidence path is
    the record the gate wrote, and the intent is the descriptor copied from the
    agent's completion record. The reviewer's own comments on the PR are NOT
    repeated here — the rework launcher already fetches and appends them for
    every cycle, and a second copy would drift from the first.

    A missing part is named as missing rather than omitted. An agent told
    "publish validation failed" with no command and no artifact would go looking
    for both; an agent told the artifact was not recorded knows not to.
    """
    receipt = attempt.latest_publication_evaluation
    lines = [
        "The control continuation for this candidate has ended without "
        f"publishing it (phase: {phase_reason}). The work is yours to correct "
        "on this same pull request; nothing has been pushed or merged on your "
        "behalf.",
        "",
        f"- PR: #{pr.number} {pr.url}".rstrip(),
        f"- Branch: {pr.branch}",
        f"- Failed candidate commit: {attempt.key.head_sha}",
    ]
    if receipt is not None:
        lines.extend(
            [
                f"- Publication gate command: {receipt.command}",
                f"- Publication gate verdict: {receipt.verdict.value} "
                f"(suite {receipt.suite}, profile {receipt.profile})",
            ]
        )
    else:
        lines.append(
            "- Publication gate: no verdict was recorded for this commit."
        )
    lines.append(
        f"- Failure evidence: {attempt.validation_record_path}"
        if attempt.validation_record_path
        else "- Failure evidence: no validation record path was recorded."
    )
    descriptor = attempt.continuation_descriptor
    if descriptor is not None:
        lines.extend(
            [
                "",
                "What the previous agent recorded for this candidate:",
                "",
                f"Implementation: {descriptor.implementation}",
                f"Problems: {descriptor.problems}",
            ]
        )
    lines.extend(
        [
            "",
            "Fix the cause of the publication failure on this branch, then "
            "complete through the ordinary rework contract. Do not treat the "
            "failed commit above as validated.",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "CONTINUATION_EXIT_SOURCE",
    "ContinuationHandoffOutcome",
    "ContinuationHandoffResult",
    "ContinuationReworkHandoff",
    "build_continuation_rework_feedback",
]
