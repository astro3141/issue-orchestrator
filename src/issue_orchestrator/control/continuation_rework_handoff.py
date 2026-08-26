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

**A re-derived exit must not cost a GitHub read.** Every refusal this module
can reach from facts it already holds is reached before the PR read: the board
issue arrives with the exit, so its blocking labels and its agent label are
free. The one refusal that genuinely needs the read is "there is no open PR",
and a negative answer there is not cached anywhere downstream, so it is
remembered here per candidate rather than re-searched on every reconciliation
for the rest of the candidate's life.

**A publication failure is handed over with its own output, or not at all.**
The receipt says the publish contract refused ``A``, and which command it ran;
it deliberately carries no output, so a correction agent given only the receipt
knows publication failed and nothing about *what* failed. The record path on the
attempt is no better — it points into the coder's run directory, inside a
worktree ordinary cleanup has usually already reaped, so it resolves to nothing
at precisely the moment a rework is launched. The durable half exists already:
#94 files a failed gate's stdout and stderr into the primary checkout at
gate-execution time, bound to ``(issue, A, suite)``. So this handoff resolves
that bundle through its owner and copies the failing output into the correction
context. If the bundle cannot be resolved, the candidate is STRANDED rather than
handed over: a rework whose prompt says "publication failed, go and find out
why" is the human relay #297 exists to remove, and refusing it loudly leaves
exactly one thing for a human to fix instead of an agent to rediscover.

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
from ..events import EventName
from ..ports import make_trace_event
from .completion_pr_collision import get_open_pr_for_issue
from .gate_failure_diagnostics import (
    DIAGNOSTIC_FILE_NAME,
    FAILURE_LOG_TAIL_BYTES,
    STDERR_FILE_NAME,
    STDOUT_FILE_NAME,
)
from .rework_cycle_policy import (
    ReworkAdmission,
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.attempt import Attempt
    from ..domain.models import OrchestratorState
    from ..ports import EventSink
    from ..ports.pull_request_tracker import PRInfo
    from .completion_pr_collision import CompletionPrAdapter
    from .continuation_live_truth import ContinuationReworkExit
    from .gate_failure_diagnostics import DurableGateFailure, GateFailureDiagnostics

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
class PublicationFailureEvidence:
    """What still explains this candidate's publication failure, if it had one.

    Three situations, and only a type can keep them apart without a caller
    re-deriving them: the candidate's publication was never refused, so there is
    nothing to explain; it was refused and the durable bundle resolves; it was
    refused and nothing survives to say why. The third is the only one that
    refuses a handoff, and :attr:`missing` is the whole predicate — a caller
    that compared ``failure is None`` would strand every exit that reached
    rework by a route other than a failed publish contract.
    """

    #: Whether the durable record says the publication contract refused this
    #: candidate, and therefore that an explanation is owed.
    required: bool
    #: The explanation, when one both is owed and survives.
    failure: "DurableGateFailure | None" = None

    @property
    def missing(self) -> bool:
        """Whether an explanation is owed and none can be produced."""
        return self.required and self.failure is None


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
        diagnostics: "GateFailureDiagnostics",
        events: "EventSink",
    ) -> None:
        #: The engine's live facts. Held rather than passed per call because
        #: what already holds an issue changes between reconciliations within
        #: one tick, and an owner deciding from a snapshot taken earlier would
        #: admit a second rework for a candidate it just admitted one for.
        self._state = state
        self._pull_requests = pull_requests
        self._budget = budget
        #: #94's durable failed-gate store, rooted in the PRIMARY checkout. The
        #: only thing that can still say why a publish contract refused this
        #: candidate once its worktree is gone, which by the time an exit is
        #: derived it usually is.
        self._diagnostics = diagnostics
        #: Refusals that strand a candidate are published, not merely logged.
        #: They are the ones a human has to act on, and per this repo's
        #: events-vs-logs rule the UI cannot react to a ``logger.warning``.
        self._events = events
        #: Candidates this handoff has already established have no open PR,
        #: keyed by (issue, candidate SHA). See :meth:`_no_open_pr_is_settled`.
        self._settled_without_pr: set[tuple[int, str]] = set()

    def admit(
        self, exits: Sequence["ContinuationReworkExit"]
    ) -> ContinuationHandoffResult:
        """File ordinary rework for every PR-backed exit that may take a cycle."""
        self._forget_exits_no_longer_derived(exits)
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
        # Everything decidable for free is decided first, and decided by the
        # cycle owner rather than re-implemented here. The board issue this
        # exit carries is already in hand, so its labels cost nothing — and
        # this is what stops a re-derived exit from paying a search-API read on
        # every reconciliation for a refusal it will make forever.
        held = self._budget.already_held(
            issue_number,
            queued_issue_numbers=self._claimed_issue_numbers(),
            active_issue_numbers=self._active_issue_numbers(),
            issue_labels=list(issue.labels),
        )
        if held is not None:
            return _refused(issue_number, held)

        agent_type = issue.agent_type
        if not agent_type:
            # The same refusal the ordinary scanner makes, for the same reason:
            # a rework has to be launched as some agent, and guessing one would
            # run the wrong prompt against a real PR. Asked before the PR read
            # because the answer is on the issue, not the PR.
            return self._strand(
                issue_number,
                "no_agent_label",
                "[CONTINUATION] issue #%d exits to rework but carries no agent "
                "label; left for the ordinary lane",
            )

        # Asked before the PR read for the same reason the two above are: it is
        # answered from the primary checkout's own filesystem, costs no API
        # budget, and a candidate whose failure can never be explained is
        # refused on every pass for the rest of its life. It is also permanent
        # in a way the PR answer is not — #94 writes at gate-execution time, so
        # a bundle that is not there now was never written and never will be —
        # which is why it needs no memo to stay cheap.
        evidence = self._durable_failure(exit_)
        if evidence.missing:
            return self._strand(
                issue_number,
                "missing_failure_evidence",
                "[CONTINUATION] issue #%d exits to rework after a publication "
                "failure whose durable output cannot be resolved; refusing to "
                "hand over a correction nobody can act on",
            )

        # The non-PR-backed exit. It is not this producer's case: with no open
        # PR there is no lineage to correct, and #195's own no-PR recovery path
        # is the owner of what happens next. Behaviour there is unchanged
        # precisely because nothing is filed here.
        if self._no_open_pr_is_settled(exit_):
            return ContinuationHandoffOutcome(
                issue_number=issue_number,
                verdict=ReworkAdmissionVerdict.SKIP,
                reason="no_open_pr",
            )
        pr = get_open_pr_for_issue(self._pull_requests, issue_number)
        if pr is None:
            self._settle_without_pr(exit_)
            return self._strand(
                issue_number,
                "no_open_pr",
                "[CONTINUATION] issue #%d exits to rework but has no open PR; "
                "left for the no-PR recovery path",
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
                pr=pr,
                attempt=exit_.attempt,
                phase_reason=exit_.phase.value,
                failure=evidence.failure,
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

    def _durable_failure(
        self, exit_: "ContinuationReworkExit"
    ) -> PublicationFailureEvidence:
        """Resolve the failure output this exit owes its correction agent.

        The obligation is read off the durable record rather than off the phase
        name: an exit owes an explanation exactly when its attempt carries a
        publication receipt that REFUSED it. That is what ``EXHAUSTED`` means,
        and it is also true of the rarer exit that carries both a failed publish
        receipt and a ``CHANGES_REQUESTED`` verdict — while an exit that reached
        rework after a PASS owes nothing here and must not be stranded for it.

        The suite comes from that receipt, so the bundle looked for is the one
        the contract that actually refused this candidate wrote; the commit is
        the attempt's own key. The issue identity is the BOARD's key, which
        :func:`~..domain.issue_key_codec.issue_key_path_part` spells identically
        to the stored one — that is the property the whole store is filed under.

        A bundle that resolves but carries no output on either stream is treated
        as no bundle. It repeats the receipt and adds nothing, and the point of
        this read is the output.
        """
        refusal = exit_.attempt.publication_refusal
        if refusal is None:
            return PublicationFailureEvidence(required=False)
        failure = self._diagnostics.for_candidate(exit_.issue.key).latest_failure(
            head_sha=exit_.attempt.key.head_sha, suite=refusal.suite
        )
        if failure is not None and not failure.explains_the_failure:
            logger.warning(
                "[CONTINUATION] the durable %s diagnostic at %s for issue #%d "
                "carries no output on either stream",
                refusal.suite,
                failure.directory,
                exit_.issue.number,
            )
            failure = None
        return PublicationFailureEvidence(required=True, failure=failure)

    def _claimed_issue_numbers(self) -> set[int]:
        """Issues already spoken for, asked of the collection's own owner.

        ``superseding_context`` because this producer holds the publication
        failure's evidence and the ordinary label sweep does not. A context-free
        fact the sweep filed earlier in the same tick is not a claim to yield
        to — it is one this handoff's fact replaces on the way in — and yielding
        to it would make the correction context's survival depend on which
        entry point ran first.
        """
        return self._state.issues_with_claimed_rework(superseding_context=True)

    def _active_issue_numbers(self) -> set[int]:
        return {session.issue.number for session in self._state.active_sessions}

    @staticmethod
    def _exit_identity(exit_: "ContinuationReworkExit") -> tuple[int, str]:
        return (exit_.issue.number, exit_.attempt.key.head_sha)

    def _no_open_pr_is_settled(self, exit_: "ContinuationReworkExit") -> bool:
        """Whether "this candidate has no open PR" is already established.

        The one negative answer worth remembering. ``AdapterCache`` caches only
        positive PR answers, so an issue with no open PR is a full
        ``get_prs_for_issue`` search — on a 30 req/min budget — every time the
        exit is re-derived, which is every reconciliation, forever, for a
        refusal that records nothing and changes nothing.

        The memo is keyed by the candidate, not the issue, and that is what
        bounds it. A PR appearing for this issue means a session ran, which
        means the issue was in ``active_sessions`` while it ran (refused above,
        never reaching here) and a NEWER attempt was recorded when it finished
        — a different SHA, a different key, and a fresh read. Entries for exits
        this engine no longer derives are dropped by
        :meth:`_forget_exits_no_longer_derived`, so the memo never outgrows the
        set of candidates currently sitting in the exit.
        """
        return self._exit_identity(exit_) in self._settled_without_pr

    def _settle_without_pr(self, exit_: "ContinuationReworkExit") -> None:
        self._settled_without_pr.add(self._exit_identity(exit_))

    def _forget_exits_no_longer_derived(
        self, exits: Sequence["ContinuationReworkExit"]
    ) -> None:
        """Drop memos for candidates this pass no longer sees in the exit."""
        self._settled_without_pr.intersection_update(
            self._exit_identity(exit_) for exit_ in exits
        )

    def _strand(
        self, issue_number: int, reason: str, log_message: str
    ) -> ContinuationHandoffOutcome:
        """Refuse an exit in a way that leaves the candidate for a human.

        ``no_open_pr``, ``no_agent_label`` and ``missing_failure_evidence`` are
        the refusals nothing downstream retries: the candidate sits until
        somebody looks. So they are published as well as logged — a UI that
        could only read the log text would be parsing it, which this repo's
        events-vs-logs rule forbids.

        Stranding is the fail-closed direction for the third, not a
        second-best. The alternative is a rework whose prompt asks an agent to
        rediscover a failure nobody kept, which spends a cycle to arrive back
        here; this spends none and says exactly what is missing.
        """
        logger.warning(log_message, issue_number)
        self._events.publish(
            make_trace_event(
                EventName.REWORK_SKIPPED,
                {
                    "reason": reason,
                    "issue_number": issue_number,
                    "source": CONTINUATION_EXIT_SOURCE,
                },
            )
        )
        return ContinuationHandoffOutcome(
            issue_number=issue_number,
            verdict=ReworkAdmissionVerdict.SKIP,
            reason=reason,
        )


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
    *,
    pr: "PRInfo",
    attempt: "Attempt",
    phase_reason: str,
    failure: "DurableGateFailure | None",
) -> str:
    """The correction context that travels with an admitted rework.

    Everything here is copied from a durable record, never re-derived: the
    candidate SHA is the attempt's own key, the publish command and verdict come
    from the receipt the gate filed for that exact commit, the failing output
    and its location come from #94's durable bundle for that same commit and
    suite, and the intent is the descriptor copied from the agent's completion
    record. The reviewer's own comments on the PR are NOT repeated here — the
    rework launcher already fetches and appends them for every cycle, and a
    second copy would drift from the first.

    ``failure`` is ``None`` only for an exit whose publication was never
    refused — a candidate handed back after a reviewer asked for changes on a
    commit that passed. A publication failure with no resolvable output never
    reaches here at all: its handoff is refused upstream, because "publication
    failed, go and find out why" is a prompt that needs a human to answer it.

    A missing part is named as missing rather than omitted. An agent told
    "publish validation failed" with no command would go looking for one; an
    agent told no verdict was recorded knows not to.
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
    if failure is not None:
        lines.extend(_durable_failure_lines(failure))
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


def _durable_failure_lines(failure: "DurableGateFailure") -> list[str]:
    """The failing run's own output, plus where the whole of it still lives.

    Both, deliberately. The excerpt is what makes the prompt actionable without
    a second lookup; the directory is what makes it checkable and gets an agent
    to the rest of a log the excerpt is a tail of. The path is in the PRIMARY
    checkout, not in any worktree, so it resolves from wherever the rework runs.
    """
    lines = [
        "",
        "The publication gate's own output for that commit was kept before the "
        "candidate's worktree was removed, and is readable now at:",
        "",
        f"    {failure.directory}",
        "",
        f"({DIAGNOSTIC_FILE_NAME}, {STDOUT_FILE_NAME}, {STDERR_FILE_NAME}. "
        f"Exit code: {failure.exit_code}"
        f"{'; the run timed out' if failure.timed_out else ''}.)",
    ]
    for name, log in (("stdout", failure.stdout), ("stderr", failure.stderr)):
        if not log.has_output:
            continue
        heading = f"Publication gate {name}"
        if log.truncated:
            heading += (
                f" (last {FAILURE_LOG_TAIL_BYTES} bytes of {log.path.name}; "
                "read the file above for the rest)"
            )
        lines.extend(["", f"{heading}:", "", "```", log.tail.rstrip("\n"), "```"])
    return lines


__all__ = [
    "CONTINUATION_EXIT_SOURCE",
    "ContinuationHandoffOutcome",
    "ContinuationHandoffResult",
    "ContinuationReworkHandoff",
    "PublicationFailureEvidence",
    "build_continuation_rework_feedback",
]
