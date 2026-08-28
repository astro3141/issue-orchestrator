"""Which completion effects may commit, and what a failed one terminalizes to.

A completion's planned actions are not a flat list of equals. Some are effects
that only make sense once the completion has actually SUCCEEDED — releasing the
``in-progress`` claim, stamping labels, posting the completion comment — and
some are the mutations the completion's own decision REQUIRES. If a required
one fails and its success-only siblings commit anyway, the issue is left in a
state no decision ever chose.

Ordering the required one first does not prevent that. :class:`ActionApplier`
catches an ordinary adapter error, reports a FAILED :class:`ActionResult`, and
goes on to the next action, so within one ``apply_all`` a failure is not a stop.
The boundary has to be built, and this module is it: the gate members are
applied FIRST, as their own batch, and the remainder applies only if that batch
committed.

Two gate members are wired today, from two issues that arrived at the same
shape from opposite directions:

``ResetRetryIssueAction`` (ADR-0031 §2, #6779 R13)
    A tech_lead decision MANDATED a scratch reset. A completion whose reset did
    not run is not the completion the decision authorised, so its labels and
    comments must not commit as if it were.

:class:`~.result_only_completion.ResultOnlyCloseIssueAction` (#337 round 3, F1)
    A run proven to offer no code candidate is closed instead of being released
    to ``pr-pending``, because no pull request will ever arrive to close it. A
    close that FAILED followed by a release that succeeded leaves an OPEN issue
    with its execution claim given up — indistinguishable to ``Scheduler`` from
    work never started, so the finished measurement relaunches every tick. Held
    behind the gate, the failed close leaves the issue open and still CLAIMED:
    not runnable, and recoverable by the ordinary stale-claim path.

The verdict this produces is also the single terminal-status policy for the
whole post-apply completion phase (#6764 re-review F2, #6777):
:func:`effective_terminal_status` turns it into the ONE status the observer,
failure discovery, retry gating, cleanup reason, operator surface and history
all read, so none of them can split between the agent's reported status and
what actually committed. An apply that RAISED past the runtime-kill boundary is
folded through the same machinery, so it cannot be a false success either.

The failure carries its KIND, because the durable operator surface a failure
deserves is the failing owner's business and not this module's: a failed
mandated reset routes to needs-human through
``tech_lead_reset_retry.build_required_act_level_failure_actions``, whereas a
failed result-only close is already durably surfaced by the state it declines
to leave — an open, still-claimed issue carrying the run's RESULT comment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from ..domain.models import SessionStatus
from ..infra.logging_config import issue_log
from .actions import Action, ActionResult, ActionResultType, ResetRetryIssueAction
from .result_only_completion import ResultOnlyCloseIssueAction

if TYPE_CHECKING:
    from ..domain.models import SessionHistoryEntry
    from .action_applier import ActionApplier

logger = logging.getLogger(__name__)


class CompletionGateKind(Enum):
    """Which gate an action is, so its failure can be surfaced by its owner."""

    MANDATED_RESET = "mandated_reset"
    """A tech_lead decision's required act-level reset (ADR-0031 §2)."""

    RESULT_ONLY_CLOSE = "result_only_close"
    """The terminal close of a run whose comment was its whole delivery (#337)."""

    APPLY_RAISED = "apply_raised"
    """No gate result at all: ``apply_all`` raised before anything committed."""


@dataclass(frozen=True, slots=True)
class CompletionGateFailure:
    """One gate that did not commit, named by kind so its owner can act on it."""

    kind: CompletionGateKind
    detail: str


@dataclass(frozen=True)
class CompletionGateOutcome:
    """Did every gating action of this completion commit?

    The single authoritative boundary the completion path consumes to decide
    terminalization. ``committed`` is true when no gate FAILED. A stale
    downgrade (``ActionResultType.SKIPPED``) counts as committed: the board
    moved and the owner correctly surfaced instead of mutating — a non-failure
    outcome. Only a hard FAILURE blocks success terminalization.
    """

    committed: bool
    failures: tuple[CompletionGateFailure, ...] = ()

    @property
    def failed(self) -> bool:
        return not self.committed

    def failed_kinds(self) -> frozenset[CompletionGateKind]:
        """Which gates failed — what a kind-specific surface keys off."""
        return frozenset(failure.kind for failure in self.failures)

    def failures_of(
        self, kind: CompletionGateKind
    ) -> tuple[CompletionGateFailure, ...]:
        """The failures of ONE gate — what its owner's surface is built from.

        Routing by kind happens here rather than inside each surface builder, so
        a builder is handed only its own failures and can never word a report
        about a gate it does not own (#337 r3 F1).
        """
        return tuple(failure for failure in self.failures if failure.kind is kind)

    def failure_summary(self) -> str:
        return "; ".join(failure.detail for failure in self.failures) or (
            "a required completion action did not commit"
        )


def completion_gate_kind(action: Action) -> CompletionGateKind | None:
    """The gate this action is, or ``None`` for a success-only effect.

    THE single source of "which actions gate the rest", shared by the apply-time
    boundary (:func:`partition_completion_gate_actions`, which withholds the
    success-only effects) and the terminal VERDICT
    (:func:`evaluate_completion_gate_outcome`), so the gate and the effects it
    holds back classify the same actions and cannot drift (#6779 R13).

    ``ResultOnlyCloseIssueAction`` is matched by its own type and not by
    ``CloseIssueAction``: a generic close is planned by the tech_lead terminal
    effects and the close-on-merge owner, whose batches are NOT gated by it.
    """
    if isinstance(action, ResetRetryIssueAction):
        return CompletionGateKind.MANDATED_RESET
    if isinstance(action, ResultOnlyCloseIssueAction):
        return CompletionGateKind.RESULT_ONLY_CLOSE
    return None


def is_completion_gate_action(action: Action) -> bool:
    """True for an action whose failure withholds the success-only remainder."""
    return completion_gate_kind(action) is not None


def partition_completion_gate_actions(
    actions: Sequence[Action],
) -> tuple[list[Action], list[Action]]:
    """Split completion actions into (gates, success-only remainder).

    Relative order within each partition is preserved. The gate partition is
    applied first; the remainder holds the success-only effects
    (labels/comments) that must NOT commit unless every gate commits (#6779,
    #337 r3 F1).
    """
    gates = [action for action in actions if is_completion_gate_action(action)]
    remainder = [
        action for action in actions if not is_completion_gate_action(action)
    ]
    return gates, remainder


def evaluate_completion_gate_outcome(
    applied: Sequence[ActionResult],
) -> CompletionGateOutcome:
    """Fold applied results into the gate commit verdict.

    Pure over the apply results — the single seam that classifies a gate
    failure, shared by the completion terminalization path so a failed gate can
    never be recorded as a clean success (#6764 re-review F2).
    """
    failures = tuple(
        CompletionGateFailure(kind=kind, detail=_failure_detail(result, kind))
        for result in applied
        for kind in (completion_gate_kind(result.action),)
        if kind is not None and result.result_type is ActionResultType.FAILURE
    )
    return CompletionGateOutcome(committed=not failures, failures=failures)


def _failure_detail(result: ActionResult, kind: CompletionGateKind) -> str:
    return result.error or f"{kind.value} did not commit"


def apply_completion_actions_gated(
    action_applier: "ActionApplier",
    actions: Sequence[Action],
    *,
    issue_number: int,
) -> tuple[list[ActionResult], BaseException | None]:
    """Apply completion actions so the gates decide whether the rest commits.

    THE authority-with-effects owner (ADR-0031 §2, #6779 R13 root cause, #337 r3
    F1): every gating action is applied FIRST as its own batch, and the
    success-only siblings (completion labels/comments/release) apply ONLY when
    all of them commit — so a failing gate can never leave a success-only effect
    committed, no matter where it sat in the planned list. A raised apply past
    the runtime-kill boundary (Reconciliation/Claim/adapter, #6777) withholds
    the remainder too and is returned so the caller can finalize the ONE
    terminal outcome, then re-raise. With no gate the whole list applies in one
    pass — behavior for ordinary completions is unchanged.
    """
    gates, remainder = partition_completion_gate_actions(actions)
    applied, error = _apply_completion_action_batch(
        action_applier, gates or list(actions), issue_number
    )
    if not gates or error is not None or evaluate_completion_gate_outcome(applied).failed:
        if gates and remainder:
            logger.warning(
                issue_log(issue_number, "Gating completion action did not commit; "
                          "withholding %d success-only completion effect(s)"),
                len(remainder),
            )
        return applied, error
    remainder_applied, error = _apply_completion_action_batch(
        action_applier, remainder, issue_number
    )
    return applied + remainder_applied, error


def _apply_completion_action_batch(
    action_applier: "ActionApplier",
    actions: Sequence[Action],
    issue_number: int,
) -> tuple[list[ActionResult], BaseException | None]:
    """Apply one batch of actions, capturing a raise past the runtime-kill boundary.

    Terminal finalization must run on EVERY apply outcome (#6777): a propagated
    ``ReconciliationRequired`` / ``ClaimLostError`` / adapter fault is CAPTURED and
    returned rather than aborting before finalization.
    """
    if not actions:
        return [], None
    logger.info(
        issue_log(issue_number, "Applying %d completion action(s): %s"),
        len(actions),
        [type(action).__name__ for action in actions],
    )
    try:
        # `or []` tolerates test doubles whose apply_all returns None.
        return list(action_applier.apply_all(list(actions)) or []), None
    except Exception as exc:
        logger.warning(
            issue_log(issue_number, "Completion-action apply raised; finalizing "
                      "terminal FAILED before re-raising: %s"),
            exc,
        )
        return [], exc


def completion_gate_outcome_after_apply(
    applied: Sequence[ActionResult],
    apply_error: BaseException | None,
) -> CompletionGateOutcome:
    """The gate verdict once completion actions have been applied.

    On a normal return this folds the real applied results
    (:func:`evaluate_completion_gate_outcome`). When ``apply_all`` RAISED past
    the runtime-kill boundary — ``ReconciliationRequired`` / ``ClaimLostError`` /
    any adapter fault — no gate can be confirmed committed, so the verdict is a
    hard failure through the SAME machinery (#6777).
    :func:`effective_terminal_status` therefore terminalizes the whole
    completion FAILED (never a false COMPLETED) with no parallel status path, and
    the caller re-raises ``apply_error`` only AFTER finalization has committed.
    """
    if apply_error is not None:
        return CompletionGateOutcome(
            committed=False,
            failures=(
                CompletionGateFailure(
                    kind=CompletionGateKind.APPLY_RAISED,
                    detail=(
                        "completion action apply raised before commit:"
                        f" {apply_error}"
                    ),
                ),
            ),
        )
    return evaluate_completion_gate_outcome(applied)


def finalize_completion_gate_history(
    history_entry: "SessionHistoryEntry",
    outcome: CompletionGateOutcome,
) -> "SessionHistoryEntry":
    """Terminal history status for a completion carrying gating work.

    The authoritative outcome boundary (ADR-0031 §2, #6764 re-review F2): a
    gating action that FAILED at apply time makes the WHOLE completion a
    failure — never a partial success. The gate either committed or the
    session's terminal record is FAILED, so the agent's "completed" intent can
    never mask an un-run reset or an unclosed result-only issue
    (orchestrator-authoritative, fail-loud). A committed or stale-downgraded
    outcome returns the caller's entry unchanged — success terminalization
    proceeds as before.
    """
    if outcome.committed:
        return history_entry
    return replace(
        history_entry,
        status="failed",
        status_reason=(
            "required completion action did not commit: "
            + outcome.failure_summary()
        ),
    )


def effective_terminal_status(
    status: SessionStatus, outcome: CompletionGateOutcome
) -> SessionStatus:
    """The single terminal status the WHOLE post-apply completion phase consumes.

    Terminal-status policy lives HERE, co-located with the gate outcome, so the
    completion path cannot split it between the agent's reported ``status`` and
    this outcome object (#6764 re-review F2, the final abstraction point). A
    gating action that FAILED at apply time makes the effective terminal status
    :attr:`SessionStatus.FAILED` regardless of the agent's "completed" intent —
    every downstream consumer (observer, failure discovery, retry gating,
    cleanup reason, operator surface, and history) then routes the completion as
    the failure it is. A committed or stale-downgraded outcome preserves the
    agent-reported status unchanged, so ordinary completions and genuine
    failures behave exactly as before.
    """
    if outcome.failed:
        return SessionStatus.FAILED
    return status


__all__ = [
    "CompletionGateFailure",
    "CompletionGateKind",
    "CompletionGateOutcome",
    "apply_completion_actions_gated",
    "completion_gate_kind",
    "completion_gate_outcome_after_apply",
    "effective_terminal_status",
    "evaluate_completion_gate_outcome",
    "finalize_completion_gate_history",
    "is_completion_gate_action",
    "partition_completion_gate_actions",
]
