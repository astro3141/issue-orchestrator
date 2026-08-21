"""When a QUEUED tech-lead run may actually launch (#6994).

Admission (:mod:`.tech_lead_run_admission`) answers "should this run exist?".
This module answers the different question a TICK asks: of the runs that already
exist, which may start right now, and which of them should no longer exist at
all. The two are separated because a run can be admitted once and then wait many
ticks — behind the global barrier, behind capacity, behind an open provider
circuit — and the board moves underneath it in that window. Admitting a run is
never a standing licence to launch it.

Two rules live here, both consulted by
:func:`..control.reactive_tech_lead_planning.plan_tech_lead_launch_queue`:

* :func:`plan_tech_lead_launch_gate` — scope exclusivity. A global run is
  exclusive of every other tech-lead run, and a QUEUED one is a barrier.
* :func:`plan_tech_lead_launch_revalidation` — subject eligibility, re-asked
  against this tick's live evidence, so a run whose subject was closed or
  unblocked while it waited is withdrawn rather than launched.

They are free functions, not coordinator methods, so the planner can consult the
rules without constructing an admission coordinator — while there is still only
one implementation of each.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..domain.models import PendingTechLeadReview
from ..domain.tech_lead_run import (
    BARRIER_GLOBAL_AWAITING_DRAIN,
    BARRIER_GLOBAL_RUN_ACTIVE,
    BARRIER_GLOBAL_RUN_QUEUED,
    REASON_ISSUE_BLOCKED,
    REASON_ISSUE_CLOSED,
    REASON_NO_LONGER_BLOCKED,
    global_run_precedence,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .tech_lead_run_scopes import (
    active_tech_lead_sessions,
    has_active_global_run,
    is_global_pending,
    run_key_of_pending,
)

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..infra.config import Config
    from ..ports import Issue


@dataclass(frozen=True, slots=True)
class TechLeadLaunchGate:
    """Which queued runs the scope matrix allows to launch this tick.

    ``held`` is never silently empty-handed: whenever anything is withheld,
    ``barrier_reason`` says which rule withheld it, so the launch log and the
    dashboard can explain a queued-but-idle run instead of showing a stall.
    """

    launchable: tuple[PendingTechLeadReview, ...]
    held: tuple[PendingTechLeadReview, ...]
    barrier_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if bool(self.held) != bool(self.barrier_reason):
            raise ValueError(
                "TechLeadLaunchGate: barrier_reason must be set iff runs are held"
                f" (held={len(self.held)}, reason={self.barrier_reason!r})"
            )


def plan_tech_lead_launch_gate(
    config: "Config",
    pending: "Sequence[PendingTechLeadReview]",
    active_sessions: "Sequence[Session]",
) -> TechLeadLaunchGate:
    """The scope-exclusivity gate over a tick's queued tech-lead runs.

    The three rules, in the order they apply:

    1. A queued global run is a BARRIER. Nothing else launches while it is
       queued, and the global run itself waits until every active tech-lead
       session has drained — that is what makes it exclusive rather than merely
       first in line. Which queued global gets the turn is decided by
       :func:`...domain.tech_lead_run.global_run_precedence`, the same authority
       the shared ledger promotes by, so the two can never nominate different
       winners and stall each other (#6994 round 5 F16).
    2. An ACTIVE global run holds everything back until it completes.
    3. Otherwise every queued targeted run is launchable; the numeric budget
       (``worker_budget.tech_lead_slot_availability``) slices it downstream,
       which is exactly why no capacity arithmetic happens here.

    A free function so the planner can consult the rule without constructing an
    admission coordinator — there is still only ONE implementation of it, which
    :meth:`TechLeadRunCoordinator.launch_gate` also delegates to.
    """
    items = tuple(pending)
    if not items:
        return TechLeadLaunchGate((), ())

    global_queued = tuple(item for item in items if is_global_pending(item))
    if global_queued:
        if active_tech_lead_sessions(config, active_sessions):
            return TechLeadLaunchGate((), items, BARRIER_GLOBAL_AWAITING_DRAIN)
        # Whose turn it is comes from the SHARED authority, never from where a
        # run happens to sit in this engine's list. Startup recovery preserves
        # whatever order the repository scan returned, so electing
        # ``global_queued[0]`` here meant this gate and the durable ledger could
        # nominate different winners — and then renew that disagreement every
        # tick, launching neither and barring every targeted run behind them
        # (#6994 round 5 F16/A9).
        first = min(
            global_queued,
            key=lambda item: global_run_precedence(run_key_of_pending(item)),
        )
        held = tuple(item for item in items if item is not first)
        return TechLeadLaunchGate(
            (first,), held, BARRIER_GLOBAL_RUN_QUEUED if held else None
        )
    if has_active_global_run(config, active_sessions):
        return TechLeadLaunchGate((), items, BARRIER_GLOBAL_RUN_ACTIVE)
    return TechLeadLaunchGate(items, ())


# ----------------------------------------------------------------------
# Subject eligibility — one rule, applied at request time AND before launch
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SubjectRule:
    """What one tech-lead role requires of its subject issue.

    A frozen row rather than a per-role function, because the roles differ in
    exactly one bit — must the subject be blocked, or must it not be — and one
    implementation of "open, and blocked-ness as required" cannot then drift
    between them. The refusal vocabulary travels in the row so each role reports
    its own machine-readable reason.
    """

    requires_blocking: bool
    refusal_reason: str
    refusal_detail: str  # formatted with ``number`` and ``label``


# The subject rule per FOCUSED role. Global runs are absent on purpose: a
# whole-repository anchor is not a work item, and blocked-ness says nothing
# about whether the board is still worth auditing.
_SUBJECT_RULES: dict["TechLeadSessionFlavor", _SubjectRule] = {
    TechLeadSessionFlavor.FAILURE_INVESTIGATION: _SubjectRule(
        requires_blocking=True,
        refusal_reason=REASON_NO_LONGER_BLOCKED,
        refusal_detail=(
            "Issue #{number} is no longer blocked; nothing to investigate."
        ),
    ),
    TechLeadSessionFlavor.PLANNING_INVESTIGATION: _SubjectRule(
        requires_blocking=False,
        refusal_reason=REASON_ISSUE_BLOCKED,
        refusal_detail=(
            "Issue #{number} is blocked by {label!r}; a blocked subject is a"
            " failure investigation's, not a planning run's."
        ),
    ),
}


def subject_run_eligibility(
    flavor: "TechLeadSessionFlavor", issue: "Issue", blocking_label: str
) -> Optional[tuple[str, str]]:
    """Is this issue still worth a run of *flavor*? None when yes.

    The single subject rule, parameterised by the role's row above (#136). Both
    focused roles ask about the same two facts — is the subject open, and is it
    blocked — and reach OPPOSITE verdicts on the second: an investigation exists
    because the subject is blocked, a planning run because it is not. Two
    hand-written rules would be two places for "what counts as open" to drift,
    and a caller choosing between them by hand would be a third.

    Returned as a ``(reason_code, detail)`` pair so every caller reports the same
    machine-readable refusal.

    It is deliberately module-level, because it is asked TWICE about the same
    logical run: once by :meth:`TechLeadRunCoordinator.admit` when the request
    arrives, and again immediately before the queued run would launch. A run can
    sit queued for many ticks behind the global barrier, and in that window its
    subject can be closed, unblocked, or start failing — so admitting a run is
    never a standing licence to launch it. ``blocking_label`` is the label the
    caller already resolved: classification happens ONCE, so the verdict and the
    evidence-map context can never disagree about which label blocked it.
    """
    rule = _SUBJECT_RULES[flavor]
    lifecycle = (getattr(issue, "state", "") or "").casefold()
    if lifecycle and lifecycle != "open":
        return (
            REASON_ISSUE_CLOSED,
            f"Issue #{issue.number} is closed; nothing for a tech lead to do.",
        )
    if bool(blocking_label) is not rule.requires_blocking:
        return (
            rule.refusal_reason,
            rule.refusal_detail.format(number=issue.number, label=blocking_label),
        )
    return None


@dataclass(frozen=True, slots=True)
class TechLeadRunWithdrawal:
    """A queued run whose subject stopped being worth investigating."""

    item: PendingTechLeadReview
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class TechLeadRevalidation:
    """Which queued runs survived launch-time revalidation, and which did not."""

    still_eligible: tuple[PendingTechLeadReview, ...]
    withdrawn: tuple[TechLeadRunWithdrawal, ...]


def plan_tech_lead_launch_revalidation(
    pending: "Sequence[PendingTechLeadReview]",
    board: "Sequence[Issue]",
    is_blocking_any: "Callable[[Sequence[str]], bool]",
    subjects: "Sequence[Issue]" = (),
) -> TechLeadRevalidation:
    """Re-check every queued FOCUSED run against this tick's live evidence.

    Each item is re-asked its OWN role's subject rule
    (:func:`subject_run_eligibility`), so a planning run is not withdrawn for
    being unblocked — the state it requires — and a failure investigation is
    not kept alive for being unblocked, the state that ends it (#136).

    Two evidence sources, in order:

    * ``board`` — the issues the tick already fetched, so a subject still on the
      board costs no extra GitHub call no matter how long its run waits behind
      the global barrier;
    * ``subjects`` — the AUTHORITATIVE lifecycle reads the fact gatherer makes
      for queued subjects the board did not carry
      (:meth:`FactGatherer.gather_tech_lead_subject_facts`). Without them the
      closed-while-queued rule was unreachable in production: the board fetch
      asks GitHub only for OPEN issues, so a subject closed while queued came
      back ABSENT rather than ``state="closed"`` (#6994 round 1 F4).

    Only POSITIVE evidence withdraws a run. The board is filtered — by agent
    label, milestone, and ``filtering.exclude_labels``, which ``tech_lead
    .inherit_labels`` deliberately re-admits for tech-lead work — so a subject
    that is absent from BOTH sources proves nothing and its run is kept.
    Withdrawing on absence would silently cancel legitimate investigations of
    every issue the board filter happens not to carry, and would turn a
    transient GitHub read failure into a cancelled run.

    Global runs are never subject to this: a health-review anchor is not a
    blocked work item, and blocked-label eligibility says nothing about whether
    the board is still worth auditing.
    """
    by_number: dict[int, "Issue"] = {issue.number: issue for issue in subjects}
    by_number.update({issue.number: issue for issue in board})
    eligible: list[PendingTechLeadReview] = []
    withdrawn: list[TechLeadRunWithdrawal] = []
    for item in pending:
        issue = (
            None if is_global_pending(item) else by_number.get(item.issue_number)
        )
        if issue is None:
            eligible.append(item)
            continue
        blocking = next(
            (name for name in issue.labels if is_blocking_any([name])), ""
        )
        verdict = subject_run_eligibility(item.flavor, issue, blocking)
        if verdict is None:
            eligible.append(item)
        else:
            withdrawn.append(TechLeadRunWithdrawal(item, verdict[0], verdict[1]))
    return TechLeadRevalidation(tuple(eligible), tuple(withdrawn))
