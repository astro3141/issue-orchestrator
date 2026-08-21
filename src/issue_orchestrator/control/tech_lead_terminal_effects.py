"""What a dead-or-rejected Tech Lead run does to its anchor and its subject.

The other half of ``tech_lead_completion``: that module plans what a session
that LANDED produces, this one plans what a session that did not produces —
the FAILED/TIMED_OUT crash path and the COMPLETED-but-rejected decision path.

They are one module rather than two because they answer one question the same
way, and answering it twice is how the boundary broke (#136 review A1/A2):

    may THIS run leave a recovery label on its own SUBJECT?

A tech_lead run has two issues, and the distinction is the whole subject of
this module. Its ANCHOR is the issue the session runs in — bookkeeping for a
batch/health review, closed when the run dies. Its SUBJECT is the work item a
FOCUSED run was aimed at: a live board card the orchestrator still owes work
on. The generic session-failure path in ``completion_action_planner`` stamps
``blocked-failed``/``needs-human`` on ``issue-{N}`` for every ``issue-``
session without asking whose session it is, and the rejection path below used
to add ``blocked-failed`` unconditionally. For a role whose capability row
omits every recovery kind (#136's ``planning_investigation``), both are a
recovery-state change the role may not propose — reached by crashing, or by
proposing the very action the capability gate refused. ``permits_recovery`` on
the capability policy is the single owner of the answer;
:func:`_may_leave_recovery_label_on_subject` is the single place it is asked.

Everything else a failure produces is untouched: the rejection surface, the
operator comment, the manifest labels, the anchor close, and the released
in-progress claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.models import Session, SessionStatus
from ..domain.tech_lead_capabilities import TECH_LEAD_ACTION_CAPABILITIES
from ..domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    RemoveLabelAction,
)
from .label_manager import LabelManager
from .tech_lead_completion import (
    manifest_label_actions,
    resolve_launch_authority_for_session,
    split_tech_lead_decision_error,
)
from .tech_lead_decision_actions import plan_tech_lead_rejection_action
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)


def generate_tech_lead_decision_failure_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    processing_errors: list[str],
    labels: LabelManager,
    tech_lead_authority: "TechLeadAuthorityStore",
) -> list[Action]:
    """Completion effects when a COMPLETED tech_lead session was rejected.

    The completion processing path recorded a tech_lead authority/decision
    error (findings 1/3); history is FAILED via the critical-error seam.
    This plans the label/comment effects for every flavor:

    * batch review — the AUTHORITY manifest PRs get the tech-lead-failed label;
    * every flavor — the rejection is surfaced as an event AND durably on the
      session's own issue (explanatory comment, the operator-facing escalation
      surface — #6761 finding 6), and the in-progress claim is released. The
      batch tracking issue stays open for re-audit.
    * the blocked-failed label on that issue is the SUBJECT's recovery state,
      so it is subject to the same rule as the crash path (#136 review A2): a
      run whose role may not propose a recovery action does not get to cause
      one by proposing it. Rejecting a bounded role's forbidden proposal is the
      capability row working; blocking the healthy issue it was preparing is
      not part of that. The rejection surface itself is untouched — only the
      label, and the sentence of the comment that describes it.
    """
    failure, detail = split_tech_lead_decision_error(processing_errors)
    actions: list[Action] = []
    authority, _tamper = resolve_launch_authority_for_session(
        tech_lead_authority, session
    )
    # The SAME question the crash path asks (#136 review A2), asked of the same
    # owner: may this run leave a recovery label on its own subject? Without an
    # authority record the role is unproven, so the generic label stands — the
    # conservative direction, matching plan_tech_lead_terminal_effects.
    bounded_role = authority is not None and not _may_leave_recovery_label_on_subject(
        authority.flavor
    )
    if (
        authority is not None
        and authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW
    ):
        actions.extend(
            manifest_label_actions(config, authority, expected, success=False)
        )
    actions.append(
        plan_tech_lead_rejection_action(
            anchor_issue_number=session.issue.number,
            failure=failure,
            detail=detail,
        )
    )
    detail_text = detail or "no detail recorded"
    outcome_note = (
        _no_recovery_authority_note(suppressed=(labels.blocked_failed,))
        if bounded_role
        else (
            f"The session is recorded as failed and `{labels.blocked_failed}`"
            " was added. Remove the label to allow reprocessing."
        )
    )
    if not bounded_role:
        actions.append(
            AddLabelAction(
                issue_number=session.issue.number,
                label=labels.blocked_failed,
                reason=f"Tech Lead completion rejected ({failure})",
                expected=expected,
            )
        )
    actions.extend(
        (
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    "## ❌ Tech Lead completion rejected\n\n"
                    "The tech_lead session completed, but its output was"
                    f" rejected (`{failure}`):\n\n"
                    f"> {detail_text}\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"{outcome_note}"
                ),
                reason="Durable operator record of the rejected tech_lead completion",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=labels.in_progress,
                reason="Tech Lead completion rejected - releasing claim",
                expected=expected,
            ),
        )
    )
    return actions


@dataclass(frozen=True, slots=True)
class TechLeadTerminalEffects:
    """Both ends of what a FAILED/TIMED_OUT tech_lead session does (#136 A1).

    A dead tech_lead session has effects on two different things, and until
    #136 only one of them had an owner:

    * its ANCHOR — close the tracking/health issue, label the manifest PRs.
      ``added`` carries these, appended after the generic session-failure
      effects the way they always were.
    * its SUBJECT — the recovery labels (``blocked-failed`` / ``needs-human``)
      the GENERIC session-failure path stamps on ``issue-{N}`` for every
      ``issue-`` session. That path never asks whose session it is. For a batch
      or health anchor the label is bookkeeping on an issue that is closing
      anyway, and a failure investigation's subject is blocked by definition —
      but for a role with no recovery authority over a live, unblocked subject
      it is a recovery-state change the role is forbidden to propose, achieved
      by dying. ``subject_actions`` is that role's own subject effects,
      substituted for the generic ones; ``None`` means the generic effects
      stand unchanged.
    """

    subject_actions: tuple[Action, ...] | None
    added: tuple[Action, ...]

    def resolve(self, generic_subject_actions: list[Action]) -> list[Action]:
        """The terminal action list, generic subject effects kept or replaced."""
        actions = (
            list(generic_subject_actions)
            if self.subject_actions is None
            else list(self.subject_actions)
        )
        actions.extend(self.added)
        return actions


_NO_TECH_LEAD_TERMINAL_EFFECTS = TechLeadTerminalEffects(subject_actions=None, added=())


def plan_tech_lead_terminal_effects(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    status: SessionStatus,
    labels: LabelManager,
    tech_lead_authority: "TechLeadAuthorityStore",
) -> TechLeadTerminalEffects:
    """FAILED/TIMED_OUT tech_lead terminal effects (#6768 round 5, ADR-0031 §4).

    Resolves the orchestrator-owned launch authority ONCE and answers both
    halves of :class:`TechLeadTerminalEffects` from it — the anchor's effects
    and whether this run's role may leave a recovery label on its subject.

    A non-tech_lead session, or a session without a launch authority record,
    changes nothing: the generic effects stand and no anchor effects are added
    (the session already failed; closing or labeling from untrusted worktree
    copies would hand the agent authority).
    """
    if not is_tech_lead_session(config.tech_lead_review_agent, session.issue.agent_type):
        return _NO_TECH_LEAD_TERMINAL_EFFECTS
    authority, _tamper = resolve_launch_authority_for_session(
        tech_lead_authority, session
    )
    if authority is None:
        logger.warning(
            "[tech_lead] No launch authority for session %s; "
            "skipping terminal tech_lead effects",
            session.terminal_id,
        )
        return _NO_TECH_LEAD_TERMINAL_EFFECTS
    return TechLeadTerminalEffects(
        subject_actions=_bounded_subject_terminal_actions(
            session, expected, authority=authority, status=status, labels=labels
        ),
        added=tuple(
            _anchor_terminal_actions(config, session, expected, authority=authority)
        ),
    )


def _may_leave_recovery_label_on_subject(flavor: TechLeadSessionFlavor) -> bool:
    """May a run of *flavor* change its own SUBJECT's recovery state? (#136 A1/A2)

    The SINGLE owner of that question, asked by both paths that would otherwise
    answer it separately: the crash path (session died) and the rejection path
    (session completed with a decision the contract refused). Two enforcement
    points for one rule is how the boundary ended up half-enforced in the first
    place — advertised by the capability row, the target scope, and the prompt,
    and then bypassed by whichever effect path nobody re-read.

    True for a non-focused run: its "subject" is a bookkeeping anchor, not a
    work item, and the label is part of how that anchor is retired. True for any
    focused role that may propose a recovery action — its subject's recovery
    state is already its business. False only for a bounded focused role, whose
    subject admission accepted precisely because it was OPEN and unblocked.
    """
    if not flavor.is_issue_focused:
        return True
    return TECH_LEAD_ACTION_CAPABILITIES.permits_recovery(flavor)


def _no_recovery_authority_note(*, suppressed: tuple[str, ...]) -> str:
    """Why the subject carries no blocking label, in ONE voice for both paths.

    The crash path suppresses two labels and the rejection path one, so the
    names are passed in — but an operator reading either issue gets the same
    explanation, because it is the same rule that produced both.
    """
    names = " or ".join(f"`{name}`" for name in suppressed)
    return (
        "This role holds no recovery authority over the issue it was sent to"
        f" work on, so **no {names} label was added** — the issue is left"
        " exactly as it was and remains available for normal work."
    )


# How each TERMINAL status reads in the subject's obituary. A map rather than a
# branch: the two statuses this owner is called for are exactly the two the
# generic path stamps a recovery label for, and an unexpected third raises here
# instead of being described as the wrong death.
_TERMINAL_STATUS_PHRASES: dict[SessionStatus, str] = {
    SessionStatus.TIMED_OUT: "exceeded its timeout",
    SessionStatus.FAILED: "ended without calling its completion command",
}


def _bounded_subject_terminal_actions(
    session: Session,
    expected: "ExpectedState",
    *,
    authority: TechLeadLaunchAuthority,
    status: SessionStatus,
    labels: LabelManager,
) -> tuple[Action, ...] | None:
    """Subject effects for a dead run whose ROLE holds no recovery authority.

    ``None`` — the generic session-failure effects stand — for every role that
    may propose a recovery action (its subject's recovery state is already its
    business) and for every non-focused run (its subject is an anchor, not a
    work item). What is left is the bounded focused role (#136): admission
    accepts only an OPEN, non-blocked subject for it, its capability row omits
    every recovery kind, and ``allowed_act_level_targets`` gives it no recovery
    target — so a crashed session must not be the one path that blocks the
    issue anyway. The claim is still released, and the operator still gets the
    session's obituary; what they do not get is work newly marked as failed.

    Recovery authority is read from the capability table rather than matched by
    flavor so a future bounded role inherits this the moment it declares its
    row, and a role that later GAINS a recovery kind loses the substitution in
    the same edit.
    """
    flavor = authority.flavor
    if _may_leave_recovery_label_on_subject(flavor):
        return None
    # Faithful stand-in for the generic issue-session terminal effects minus the
    # label: comment, then claim release (``_generate_timeout_actions`` /
    # ``_generate_failure_actions``). The comment is OWNED here rather than
    # reused because it has to explain the absent label — but if that generic
    # trio ever grows a fourth action, this substitution must grow with it.
    return (
        AddCommentAction(
            number=session.issue.number,
            comment=(
                f"## 🛑 Tech Lead {flavor.value} session ended without a result"
                "\n\n"
                f"The `{flavor.value}` session on this issue"
                f" {_TERMINAL_STATUS_PHRASES[status]}.\n\n"
                f"- Session: `{session.terminal_id}`\n"
                f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                + _no_recovery_authority_note(
                    suppressed=(labels.blocked_failed, labels.needs_human)
                )
            ),
            reason=(
                f"Report the dead {flavor.value} run without blocking its subject"
            ),
            expected=expected,
        ),
        RemoveLabelAction(
            issue_number=session.issue.number,
            label=labels.in_progress,
            reason=f"Tech Lead {flavor.value} session ended - releasing claim",
            expected=expected,
        ),
    )


def _anchor_terminal_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    authority: TechLeadLaunchAuthority,
) -> list[Action]:
    """Anchor-side terminal effects for a dead tech_lead run.

    Batch: the AUTHORITY manifest PRs get the operator-visible tech-lead-failed
    label and the tracking issue closes after the generic failure diagnosis
    and the PR labels: an open failed tracker would be requeued at restart
    with an empty manifest (its PRs are now candidate-filtered as
    tech-lead-failed), looping forever. Health reviews close their anchor the
    same way — an open dead anchor would both be requeued at restart and
    dedupe the next interval's trigger — but have no manifest to label.
    FOCUSED runs produce nothing here — their "anchor" is a live work issue
    (the failed one an investigation was sent to diagnose, or the open one a
    planning run was sent to prepare), and closing it because the tech-lead
    session died would close work the orchestrator still owes (#136).
    """
    if authority.flavor.is_issue_focused:
        return []
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        return [
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Health review session failed - closing anchor issue "
                "(the next interval re-fires a fresh review)",
                expected=expected,
            )
        ]
    actions = manifest_label_actions(config, authority, expected, success=False)
    actions.append(
        CloseIssueAction(
            issue_number=session.issue.number,
            reason="Batch tech_lead review failed - closing tracking issue "
            "(manifest PRs carry tech-lead-failed)",
            expected=expected,
        )
    )
    return actions
