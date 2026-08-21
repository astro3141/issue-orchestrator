"""May THIS run change its own SUBJECT's recovery state? (#136 A1/A2, #182)

A tech_lead run has two issues. Its ANCHOR is the issue the session runs in —
bookkeeping for a batch or health review. Its SUBJECT is the work item a
FOCUSED run was aimed at: a live board card the orchestrator still owes work
on. #136 gave ``planning_investigation`` a capability row that omits every
recovery kind, so the role may not propose a recovery action on its subject —
and therefore must not achieve one by malfunctioning either.

Four completion paths would each answer that question for themselves, which is
how the boundary ended up half-enforced the first time (#136 review A1/A2):

1. the crash path — the session died;
2. the rejection path — the session completed with a refused decision;
3. ``invalid_record_actions`` — the record could not be accepted;
4. the BLOCKED completion path — the agent reported it cannot proceed.

Paths 1 and 2 are planned by :mod:`.tech_lead_terminal_effects`, which knows
the run's flavor. Paths 3 and 4 are GENERIC session machinery that never learns
whose session it is, so #182 threads the answer to them as a value instead —
:class:`SubjectRecoveryAuthority`, resolved once by the tech_lead owner and
passed in. What travels is the ANSWER, never the flavor: a generic path that
received a flavor would be one flavor comparison away from owning this rule
too.

The answer is read from the capability table rather than matched by flavor, so
a future bounded role inherits every one of these four suppressions the moment
it declares its row, and a role that later GAINS a recovery kind loses them in
the same edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.tech_lead_capabilities import TECH_LEAD_ACTION_CAPABILITIES
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .actions import Action, AddLabelAction


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

        The whole decision the two generic paths need, made once here rather
        than branched at each of them (#182). They differ only in which label
        they would have added and how they phrase its presence, so both hand
        those in and splice the result — neither re-asks the question, and
        neither can produce a comment that disagrees with its own action list.
        """
        if self.may_leave_recovery_label:
            return SubjectRecoveryOutcome((add_label,), note_when_added)
        return SubjectRecoveryOutcome((), self.suppression_note(add_label.label))

    def suppression_note(self, *suppressed: str) -> str:
        """Why the subject carries no blocking label, in ONE voice for all paths.

        Each path suppresses a different label set — the crash path two, the
        rejection, rejected-record, and blocked paths one each — so the names are
        passed in. An operator reading any of those issues gets the same
        explanation, because it is the same rule that produced all of them.
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
