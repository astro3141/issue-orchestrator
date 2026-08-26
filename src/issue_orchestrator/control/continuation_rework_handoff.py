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
existing open-PR owner (:func:`..control.completion_pr_collision.look_up_open_pr_for_issue`);
the cycle number and the ceiling come from the shared rework-cycle owner
(:mod:`.rework_cycle_policy`), which the ordinary scanner decides through too.
What is left here is assembly: which exits are PR-backed, what evidence travels
with them, and where the resulting fact is filed. Composing the correction
prompt out of that evidence is a separate concern and lives in
:mod:`.continuation_rework_feedback`; this module decides WHETHER a candidate
may take a cycle, that one decides what the agent taking it is told.

**Reconciliation visibility is not work-admission authority.** The exits arrive
from a derivation that is board-wide by design and must stay that way: ownership
release names every lease the live set does not, so an engine that stopped
LOOKING at an issue would report another engine's running operation as finished
and free it. #303 measured what follows if the producer at the end of that
sequence inherits the visibility as permission — an engine started with
``--issue 301`` admitted, queued and launched rework for a held issue #293,
created its worktree and rebased its branch. So the first question asked about
any exit is the engine's own actuation scope, taken from the scope owner
:class:`~.queue_cache.QueueCache` asks (:class:`~.issue_scope.EngineIssueScope`)
rather than re-derived here from ``--issue``, labels or branch names. An exit
outside it is reported and dropped: reconciled, never worked.

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
can reach from facts it already holds is reached before the PR read *unless
something outranks it* — the board issue arrives with the exit, so its blocking
labels and its agent label are free. (The one free refusal deliberately asked
*after* the read is ``missing_failure_evidence``; see the paragraph on the
evidence gate below for why the ceiling has to outrank it.) The refusal that
genuinely needs the read is "there is no open PR", and a negative answer there
is not cached anywhere downstream, so it is remembered here per candidate rather
than re-searched on every reconciliation for the rest of the candidate's life.

**Only an answer may be remembered, never a failure to ask.** The PR port can
refuse to answer — a rate-limited ``/search/issues`` call, a timeout, a blip —
and the open-PR owner reports that as :attr:`OpenPrLookup.read_failed` rather
than folding it into "there is no PR". Memoising a read that failed would strand
the candidate for the life of the process: the exit keeps being derived, the
memo keeps short-circuiting, and #296's gap would be reachable from a recoverable
error. So a failed read refuses this pass only, records nothing, and the next
reconciliation reads again.

**A refusal that repeats is logged every time and published once.** The strands
below are permanent by construction: nothing downstream retries them, and the
exit keeps being derived until a human acts. Re-publishing the same
``rework.skipped`` on every reconciliation forever would make the event stream
say something new when nothing is, so the announcement is remembered per
(candidate, reason) — for exactly as long as the memo above lives, and dropped
by the same pruning. The log line still fires every pass; it is the human's
channel, and repetition there is how an operator sees a candidate is still
stuck.

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

**That evidence gate is the last question asked, not the first.** It guards the
*spending* of a cycle, so it is asked only once the cycle owner has granted one
— after ``ReworkCycleBudget.admit``, not before it. A candidate that is both at
the ceiling and missing its explanation is at the ceiling first: #297 says that
at exhaustion today's escalation path fires, and an evidence refusal reached
earlier would divert that candidate to ``missing_failure_evidence`` and quietly
replace an escalation a human is waiting on with one nothing produces. The
inverse costs nothing to keep: a candidate with a cycle available and no
explanation is still refused before any rework is filed, so no cycle is spent
on a prompt nobody can act on. The price of asking in this order is that the
refusal follows the PR read — a *positive* answer, which ``AdapterCache`` does
cache, so a permanently unexplainable candidate pays what an admitted one pays
rather than the uncached search ``no_open_pr`` would have cost.

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
from .completion_pr_collision import look_up_open_pr_for_issue
from .continuation_rework_feedback import build_continuation_rework_feedback
from .rework_cycle_policy import (
    ReworkAdmission,
    ReworkAdmissionVerdict,
    ReworkCycleBudget,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.models import OrchestratorState
    from ..ports import EventSink
    from ..ports.pull_request_tracker import PRInfo
    from .completion_pr_collision import CompletionPrAdapter
    from .continuation_live_truth import ContinuationReworkExit
    from .gate_failure_diagnostics import DurableGateFailure, GateFailureDiagnostics
    from .issue_scope import EngineIssueScope

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
class _LaunchableExit:
    """What the free phase established about an exit it did not refuse.

    One field, and it earns its type: ``Issue.agent_type`` is ``str | None``,
    "this exit names an agent to launch as" is settled before the PR is read,
    and a later phase re-deriving it from the same optional would be a second
    answer to a question already asked — with a fallback branch that can never
    run and can never be tested. Carrying the proof instead is what this repo's
    fail-fast rule asks for.
    """

    agent_type: str


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
        scope: "EngineIssueScope",
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
        #: The engine's actuation scope (#304). Required, with no default: a
        #: handoff assembled without one is the engine #303 measured, and it
        #: looks entirely healthy right up to the moment it rebases a held
        #: issue's branch. The OWNER, never a ``Config`` — this module must not
        #: be able to form its own opinion about what ``--issue`` means.
        self._scope = scope
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
        #: Only an ANSWER from the PR port enters here — never a read that
        #: failed, which is the difference between a fact and an outage.
        self._settled_without_pr: set[tuple[int, str]] = set()
        #: Refusals this handoff has already announced, keyed by
        #: (issue, candidate SHA, reason). See :meth:`_announce_once`.
        self._announced: set[tuple[int, str, str]] = set()

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
        """One exit, decided in the phases this module's docstring names.

        The PR read is the seam, and it is why phases 1-3 are separate: what
        comes before it must cost nothing, and what comes after it is the
        budget's decision and this producer's own last question about it.

        Phase 0 is not about the exit at all. It asks whether this engine may
        act on the issue, and it is asked first because every other question
        here presumes an answer of yes.
        """
        # Phase 0: authority. The board this exit was derived from is complete
        # by design; that is what makes the ownership release correct, and it is
        # also what puts an issue this engine was never started for in front of
        # this producer (#304).
        if self._scope.excludes(exit_.issue):
            return self._refuse_outside_scope(exit_)

        # Phase 1: everything this exit's own facts already settle.
        free = self._refuse_before_the_pr_read(exit_)
        if isinstance(free, ContinuationHandoffOutcome):
            return free

        # Phase 2: the one read, and the two different answers it can fail with.
        lookup = look_up_open_pr_for_issue(self._pull_requests, exit_.issue.number)
        if lookup.read_failed:
            # Not a fact about the candidate — a fact about GitHub, this pass.
            # Nothing is remembered, so the next reconciliation reads again and
            # a candidate whose search happened to be rate-limited is not
            # stranded for the life of the process.
            return self._refuse_unreadable_pr(exit_)
        if lookup.pr is None:
            self._settle_without_pr(exit_)
            return self._strand(
                exit_,
                "no_open_pr",
                "[CONTINUATION] issue #%d exits to rework but has no open PR; "
                "left for the no-PR recovery path",
            )

        # Phase 3: the cycle owner decides, and only then is the cycle spent.
        return self._admit_against_pr(
            exit_, free.agent_type, lookup.pr, reworks, escalations
        )

    def _refuse_before_the_pr_read(
        self, exit_: "ContinuationReworkExit"
    ) -> "ContinuationHandoffOutcome | _LaunchableExit":
        """Every refusal reachable from facts already in hand, or what they proved.

        Free in the sense that matters: no GitHub call. The board issue arrives
        with the exit, so its labels and its agent label cost nothing, and this
        is what stops a re-derived exit from paying a search-API read on every
        reconciliation for a refusal it will make forever.
        """
        issue = exit_.issue
        issue_number = issue.number
        # Decided by the cycle owner rather than re-implemented here.
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
                exit_,
                "no_agent_label",
                "[CONTINUATION] issue #%d exits to rework but carries no agent "
                "label; left for the ordinary lane",
            )

        # The non-PR-backed exit. It is not this producer's case: with no open
        # PR there is no lineage to correct, and #195's own no-PR recovery path
        # is the owner of what happens next. Behaviour there is unchanged
        # precisely because nothing is filed here. Answered from the memo, so
        # the search that established it is paid once per candidate.
        if self._no_open_pr_is_settled(exit_):
            return ContinuationHandoffOutcome(
                issue_number=issue_number,
                verdict=ReworkAdmissionVerdict.SKIP,
                reason="no_open_pr",
            )
        return _LaunchableExit(agent_type=agent_type)

    def _admit_against_pr(
        self,
        exit_: "ContinuationReworkExit",
        agent_type: str,
        pr: "PRInfo",
        reworks: list[DiscoveredRework],
        escalations: list[DiscoveredEscalation],
    ) -> ContinuationHandoffOutcome:
        """Spend a cycle on this open PR, or say which refusal stopped it."""
        issue = exit_.issue
        issue_number = issue.number
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

        # A cycle has been granted. Whether it is SPENT is this producer's own
        # last question, and it is asked here rather than earlier so that the
        # ceiling keeps its precedence: a candidate at the ceiling has already
        # taken today's escalation path above, and must not be diverted into an
        # evidence refusal that produces nothing for the human waiting on it.
        # Below the ceiling the answer still costs no cycle — nothing has been
        # filed yet, and the granted cycle is granted again next pass.
        evidence = self._durable_failure(exit_)
        if evidence.missing:
            return self._strand(
                exit_,
                "missing_failure_evidence",
                "[CONTINUATION] issue #%d exits to rework after a publication "
                "failure whose durable output cannot be resolved; refusing to "
                "hand over a correction nobody can act on",
                pr_number=pr.number,
                rework_cycle=admission.rework_cycle,
            )

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

        A bundle that resolves but carries no output on either stream is not an
        explanation — it repeats the receipt and adds nothing — and the store's
        own reader is what enforces that, falling through to an older bundle for
        the same ``(issue, SHA, suite)`` exactly as it does for one it cannot
        read. So ``None`` here means "nothing filed for this candidate explains
        anything", not "the newest thing filed happened to be empty".
        """
        refusal = exit_.attempt.publication_refusal
        if refusal is None:
            return PublicationFailureEvidence(required=False)
        failure = self._diagnostics.for_candidate(exit_.issue.key).latest_failure(
            head_sha=exit_.attempt.key.head_sha, suite=refusal.suite
        )
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

        What may enter is narrow, and that is the whole safety of it: an
        ANSWER of "there is no open PR", never a read that failed. The two are
        kept apart by :class:`~.completion_pr_collision.OpenPrLookup` rather
        than inferred from a bare ``None`` here, because a memoised outage is
        indistinguishable from a memoised fact and only one of them is true.
        """
        return self._exit_identity(exit_) in self._settled_without_pr

    def _settle_without_pr(self, exit_: "ContinuationReworkExit") -> None:
        self._settled_without_pr.add(self._exit_identity(exit_))

    def _forget_exits_no_longer_derived(
        self, exits: Sequence["ContinuationReworkExit"]
    ) -> None:
        """Drop memos for candidates this pass no longer sees in the exit."""
        derived = {self._exit_identity(exit_) for exit_ in exits}
        self._settled_without_pr.intersection_update(derived)
        self._announced = {
            announced
            for announced in self._announced
            if announced[:2] in derived
        }

    def _announce_once(
        self, exit_: "ContinuationReworkExit", reason: str
    ) -> None:
        """Publish this refusal the first time this candidate reaches it.

        Per this repo's events-vs-logs rule a UI cannot read the log line that
        says a candidate is stuck, so the refusal is published. But the exit is
        re-derived on every reconciliation for as long as the durable facts
        stand, and a refusal nothing retries is reached again every single pass:
        publishing each time would put an unbounded stream of identical events
        in front of a consumer for which nothing has changed. So the
        announcement is remembered per (candidate, reason), and dropped by
        :meth:`_forget_exits_no_longer_derived` with the rest — a candidate that
        leaves the exit and comes back is news again, and so is one a restarted
        process meets for the first time.
        """
        issue_number = exit_.issue.number
        announced = (*self._exit_identity(exit_), reason)
        if announced in self._announced:
            return
        self._announced.add(announced)
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

    def _refuse_outside_scope(
        self, exit_: "ContinuationReworkExit"
    ) -> ContinuationHandoffOutcome:
        """Refuse an exit this engine reconciled but is not allowed to work.

        Not a strand, and that is the whole difference in how it is reported.
        The three strands leave a candidate sitting until a human looks, so they
        are published; this candidate is not stuck at all. It belongs to another
        engine's scope, or to this operator's next unscoped run, and its exit
        stays derivable until whoever owns it admits it. Publishing
        ``rework.skipped`` for it — on every reconciliation, from an engine
        narrowed to a different issue — would put exactly the cross-issue
        traffic #304 removes back into the stream a consumer reads, and would
        make an operator's dashboard claim an issue is being refused by an
        engine that was never asked about it.

        The OUTCOME is still reported, and reported for every out-of-scope exit
        rather than filtered out of the result. "Reconciled but not
        work-admissible" is a state the caller has to be able to see: a handoff
        that silently dropped these would be indistinguishable from one whose
        scope owner had accidentally been given the whole board.

        Nothing is remembered either. The memos here exist to save a GitHub read
        or a repeated announcement, and this refusal costs neither — it is
        decided from the board issue the exit arrived with, before any read and
        before any collection this handoff can write to is touched.
        """
        logger.debug(
            "[CONTINUATION] issue #%d exits to rework but is outside this "
            "engine's issue scope; reconciled only, not admitted",
            exit_.issue.number,
        )
        return ContinuationHandoffOutcome(
            issue_number=exit_.issue.number,
            verdict=ReworkAdmissionVerdict.SKIP,
            reason="outside_engine_scope",
        )

    def _refuse_unreadable_pr(
        self, exit_: "ContinuationReworkExit"
    ) -> ContinuationHandoffOutcome:
        """Refuse this pass because GitHub could not be asked, and remember nothing.

        The one refusal here that is about the engine's surroundings rather
        than about the candidate, and therefore the one that must not settle
        anything: the very next reconciliation reads again. It is published on
        the same channel as the strands because a search budget that stays
        exhausted keeps a candidate from moving just as effectively — and once,
        for the same reason they are.
        """
        issue_number = exit_.issue.number
        logger.warning(
            "[CONTINUATION] could not read the open PR for issue #%d; leaving "
            "the exit for the next reconciliation",
            issue_number,
        )
        self._announce_once(exit_, "pr_read_failed")
        return ContinuationHandoffOutcome(
            issue_number=issue_number,
            verdict=ReworkAdmissionVerdict.SKIP,
            reason="pr_read_failed",
        )

    def _strand(
        self,
        exit_: "ContinuationReworkExit",
        reason: str,
        log_message: str,
        *,
        pr_number: int = 0,
        rework_cycle: int = 0,
    ) -> ContinuationHandoffOutcome:
        """Refuse an exit in a way that leaves the candidate for a human.

        ``no_open_pr``, ``no_agent_label`` and ``missing_failure_evidence`` are
        the refusals nothing downstream retries: the candidate sits until
        somebody looks. So they are published as well as logged — a UI that
        could only read the log text would be parsing it, which this repo's
        events-vs-logs rule forbids. The published payload is the same three
        fields for all three, deliberately: two of them are reached before the
        PR is read, and a key that appeared for only one of them would be a
        shape a consumer has to branch on.

        ``pr_number`` and ``rework_cycle`` are the outcome's own report, and
        follow this module's "0 means not read" convention rather than
        "unknown". The evidence refusal knows both — it is asked after the PR
        read and after the cycle owner granted the cycle it is declining to
        spend — and saying 0 there would understate what was decided.

        Stranding is the fail-closed direction for the third, not a
        second-best. The alternative is a rework whose prompt asks an agent to
        rediscover a failure nobody kept, which spends a cycle to arrive back
        here; this spends none and says exactly what is missing.
        """
        logger.warning(log_message, exit_.issue.number)
        self._announce_once(exit_, reason)
        return ContinuationHandoffOutcome(
            issue_number=exit_.issue.number,
            verdict=ReworkAdmissionVerdict.SKIP,
            reason=reason,
            pr_number=pr_number,
            rework_cycle=rework_cycle,
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


__all__ = [
    "CONTINUATION_EXIT_SOURCE",
    "ContinuationHandoffOutcome",
    "ContinuationHandoffResult",
    "ContinuationReworkHandoff",
    "PublicationFailureEvidence",
]
