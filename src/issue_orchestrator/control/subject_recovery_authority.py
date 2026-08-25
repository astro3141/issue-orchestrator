"""May THIS run change its own SUBJECT's recovery state? (#136 A1/A2, #182)

A tech_lead run has two issues. Its ANCHOR is the issue the session runs in —
bookkeeping for a batch or health review. Its SUBJECT is the work item a
FOCUSED run was aimed at: a live board card the orchestrator still owes work
on. #136 gave ``planning_investigation`` a capability row that omits every
recovery kind, so the role may not propose a recovery action on its subject —
and therefore must not achieve one by malfunctioning either.

Seven completion paths would each answer that question for themselves, which is
how the boundary ended up half-enforced the first time (#136 review A1/A2):

1. the crash path — the session died;
2. the rejection path — the session completed with a refused decision;
3. ``invalid_record_actions`` — the record could not be accepted;
4. the BLOCKED completion path — the agent reported it cannot proceed;
5. the publish-failure path — the run COMPLETED and the push or PR creation
   failed, leaving ``publish-failed`` and eventually ``needs-human``;
6. the review-exchange-halt path — the exchange around that publish stopped
   without an outcome, leaving ``blocked-failed``;
7. the COMPLETION RECORD itself — ``coding-done blocked`` and ``coding-done
   needs_human`` each write a request that would retire the issue, and those
   requests reach the generic action executor without passing any of the six
   (#257).

Paths 1 and 2 are planned by :mod:`.tech_lead_terminal_effects`, which knows
the run's flavor. Paths 3 through 6 are GENERIC session machinery that never
learns whose session it is, so #182 threads the answer to them as a value
instead — :class:`SubjectRecoveryAuthority`, resolved once by the tech_lead
owner and passed in. What travels is the ANSWER, never the flavor: a generic
path that received a flavor would be one flavor comparison away from owning
this rule too.

Path 7 is the tech_lead completion seam, which holds the run's launch authority
already; it asks :meth:`SubjectRecoveryAuthority.completion_request_outcome`
and is handed back what it must carry as well as what survives, so a refused
request cannot leave the seam without something recording that it was refused.
Every outcome whose requests this door refuses has a planned twin — path 4 for
BLOCKED, :mod:`.agent_needs_human_completion` for NEEDS_HUMAN — that speaks the
same suppression in the operator's comment, which is what keeps the durable
state and the message one answer rather than two that happen to agree.

Paths 5 and 6 are the ones a run reaches by SUCCEEDING at its own job: a
focused tech_lead run publishes onto its disposable branch
(``shape_requested_actions_for_tech_lead`` deliberately keeps ``PUSH_BRANCH``
and ``CREATE_PR``), and a push that fails there lands a blocking label on
``issue-{N}`` — which for a focused flavor IS the subject (#182 review F1).
Ruled OUT deliberately: the interrupted-retry path, whose guard label bounds a
RELAUNCH rather than retiring the issue, and the provider-blocked path, where a
dead credential is an outage record rather than a verdict on the issue.

The answer is read from the capability table rather than matched by flavor, so
a future bounded role inherits every one of these seven suppressions the moment
it declares its row, and a role that later GAINS a recovery kind loses them in
the same edit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ..domain.models import (
    RequestedAction,
    without_subject_recovery_intent,
)
from ..domain.tech_lead_capabilities import TECH_LEAD_ACTION_CAPABILITIES
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .actions import Action, AddLabelAction


def _every_request_stands(
    actions: Iterable[RequestedAction],
) -> tuple[RequestedAction, ...]:
    """The permitted answer: nothing about the requested tuple changes."""
    return tuple(actions)


_SURVIVING_RECOVERY_REQUESTS: Mapping[
    bool, Callable[[Iterable[RequestedAction]], tuple[RequestedAction, ...]]
] = {
    True: _every_request_stands,
    False: without_subject_recovery_intent,
}
"""What each answer does to a completion record's requests — a table, not a branch.

Keyed by :attr:`SubjectRecoveryAuthority.may_leave_recovery_label` so the
refusal is stated once and reads as the domain's own command form
(:func:`~...domain.models.without_subject_recovery_intent`), rather than as one
more site that decides for itself what "may not leave the label" implies for a
request that asks for it.
"""


@dataclass(frozen=True, slots=True)
class SubjectRecoveryOutcome:
    """The recovery label a path may add, and the sentence describing it.

    The two travel together because they are one decision. A path that asked
    for the label separately from the note could add the label and print the
    suppression note, or withhold it and tell the operator the issue was
    marked — the exact class of drift #136 was filed for. Splice
    ``label_actions`` into the action list and put ``note`` in the comment;
    both are already consistent.
    """

    label_actions: tuple[Action, ...]
    note: str


@dataclass(frozen=True, slots=True)
class SubjectRecoveryRequestOutcome:
    """What a COMPLETION RECORD's recovery requests come to, in one value.

    The seventh door onto a subject's recovery state is the agent's own
    completion record: ``coding-done blocked`` and ``coding-done needs_human``
    each hand the orchestrator a request that would retire the issue (#257).
    Refusing those is the same decision the six planned paths make, so it is
    made here rather than by whichever seam happens to hold the tuple.

    ``suppressed`` travels beside ``requested_actions`` for the reason
    :class:`SubjectRecoveryOutcome` pairs its label with its note: a caller
    handed only the survivors could drop an escalation and leave nothing
    recording that anything was dropped — the drift #136 was filed for, in the
    shape the completion-record seam takes. A caller that must carry
    ``suppressed`` has to say so, in its trace and in the operator's comment.
    """

    requested_actions: tuple[RequestedAction, ...]
    suppressed: tuple[RequestedAction, ...]

    @property
    def detail(self) -> str:
        """One clause naming what was refused, for the operator-facing trace.

        Empty when nothing was refused, so a caller can append it unguarded and
        a run that kept its requests reads exactly as it did before.
        """
        if not self.suppressed:
            return ""
        names = ", ".join(action.value for action in self.suppressed)
        return (
            f"recovery requests dropped ({names}): this role holds no recovery"
            " authority over the issue it was sent to work on"
        )


@dataclass(frozen=True, slots=True)
class SubjectRecoveryAuthority:
    """Whether one run may leave a recovery label on its own subject.

    Carries the decided answer and the one voice that explains a suppression to
    an operator — not the flavor it was decided from. Construct it through
    :meth:`for_flavor` (a resolved tech_lead run) or :data:`SUBJECT_RECOVERY_UNBOUNDED`
    (everything else: non-tech_lead sessions, and tech_lead runs whose launch
    authority cannot be proven, where the generic behavior must stand).
    """

    may_leave_recovery_label: bool

    @classmethod
    def for_flavor(cls, flavor: TechLeadSessionFlavor) -> "SubjectRecoveryAuthority":
        """The answer for a run of *flavor*, from the capability table.

        True for a non-focused run: its "subject" is a bookkeeping anchor, not a
        work item, and the label is part of how that anchor is retired. True for
        any focused role that may propose a recovery action — its subject's
        recovery state is already its business. False only for a bounded focused
        role, whose subject admission accepted precisely because it was OPEN and
        unblocked.
        """
        if not flavor.is_issue_focused:
            return SUBJECT_RECOVERY_UNBOUNDED
        return cls(
            may_leave_recovery_label=TECH_LEAD_ACTION_CAPABILITIES.permits_recovery(
                flavor
            )
        )

    def recovery_label_outcome(
        self, *, add_label: AddLabelAction, note_when_added: str
    ) -> SubjectRecoveryOutcome:
        """Does *add_label* survive, and what does the operator get told?

        The whole decision for a path whose entire effect on its subject is ONE
        label, made once here rather than branched at each of them (#182): the
        rejection path, the rejected-record path, the blocked path, and the
        review-exchange halt. They differ only in which label they would have
        added and how they phrase its presence, so each hands those in and
        splices the result — none re-asks the question, and none can produce a
        comment that disagrees with its own action list.

        A path whose effect is a SET rather than one label — the publish-failure
        path adds a blocking label, clears ``needs-rework`` and rolls a failure
        counter, and past a threshold escalates to a DIFFERENT label — cannot be
        expressed as "keep or drop one action", because the threshold itself is
        part of what a bounded role may not reach. Those read
        :attr:`may_leave_recovery_label` and build their substitute, ending its
        comment with :meth:`suppression_note` so the voice stays one voice.
        """
        if self.may_leave_recovery_label:
            return SubjectRecoveryOutcome((add_label,), note_when_added)
        return SubjectRecoveryOutcome((), self.suppression_note(add_label.label))

    def completion_request_outcome(
        self, requested: tuple[RequestedAction, ...]
    ) -> SubjectRecoveryRequestOutcome:
        """Which of an agent's own requests survive, and which were refused.

        The completion-record twin of :meth:`recovery_label_outcome`, for the
        seam that holds untrusted REQUESTS rather than planned actions (#257).
        The two shapes cannot share one method — a planned path hands in the
        ``AddLabelAction`` it would have taken, while this one is handed the
        whole requested tuple and must return the whole survivor — but they must
        share the ANSWER, and now they read it from the same object rather than
        each consulting :attr:`may_leave_recovery_label` and deciding for itself
        what a suppression means.

        Which requests carry a recovery change is the domain's one vocabulary
        (:data:`~...domain.models.SUBJECT_RECOVERY_ACTIONS`), so a recovery
        action added to :class:`~...domain.models.RequestedAction` is refused
        here the moment it joins that set.
        """
        kept = _SURVIVING_RECOVERY_REQUESTS[self.may_leave_recovery_label](requested)
        return SubjectRecoveryRequestOutcome(
            kept,
            tuple(action for action in requested if action not in kept),
        )

    def suppression_note(self, *suppressed: str) -> str:
        """Why the subject carries no blocking label, in ONE voice for all paths.

        Each path suppresses a different label set — the crash and
        publish-failure paths two each, the rejection, rejected-record, blocked,
        and exchange-halt paths one each — so the names are passed in. An
        operator reading any of those issues gets the same explanation, because
        it is the same rule that produced all of them.
        """
        if self.may_leave_recovery_label:
            raise ValueError(
                "suppression_note is only meaningful for a role that may NOT"
                " leave a recovery label on its subject"
            )
        names = " or ".join(f"`{name}`" for name in suppressed)
        return (
            "This role holds no recovery authority over the issue it was sent to"
            f" work on, so **no {names} label was added** — the issue is left"
            " exactly as it was and remains available for normal work."
        )


# The default for every path: the generic recovery labels stand unchanged.
SUBJECT_RECOVERY_UNBOUNDED = SubjectRecoveryAuthority(may_leave_recovery_label=True)
