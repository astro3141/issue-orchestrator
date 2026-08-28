"""The durable operator surface a FAILED completion gate earns, routed by kind.

A gate that does not commit terminalizes its whole completion FAILED
(:func:`~.completion_effect_gate.effective_terminal_status`), but that terminal
record lives in :class:`OrchestratorState` — in memory, in ONE process. Every
other ``failed`` completion is expected to "plant a BLOCKING label, so the
scheduler refuses the issue whether or not any in-memory gate is retired"
(``domain.models.ABANDONED_AFTER_COMPLETION_HISTORY_STATUSES``), and a gate
failure is no exception: without a label on the issue, a restart loses the stop
entirely.

Both gate members earn that surface, and they earn the SAME one — the shared
needs-human blocking label plus an explanation, written through the ordinary
label/comment action owners so there is no parallel mechanism. Only the words
differ, so only the words belong to the gate owners
(:class:`~.completion_gate_narrative.GateFailureNarrative`); the label, the
cause token, the failure formatting and the action shapes are one policy and
live here.

This module is the dispatch, and it is the only place that maps a
:class:`~.completion_effect_gate.CompletionGateKind` to what an operator is
told about it (#337 r4, N3). The mapping is TOTAL over the enum, which is why
"``apply_all`` raised" is not a member of it: no gate reached a verdict on that
path, so there is no owner whose account would be true, and the completion path
deliberately writes nothing (a second GitHub write immediately after a
reconciliation/claim raise would re-fail and mask the re-raise).
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .actions import Action, AddCommentAction, AddLabelAction
from .completion_effect_gate import (
    CompletionGateFailure,
    CompletionGateKind,
    CompletionGateOutcome,
)
from .completion_gate_narrative import GateFailureNarrative
from .needs_human_block import NeedsHumanCause
from .result_only_completion import FAILED_RESULT_ONLY_CLOSE_NARRATIVE
from .tech_lead_reset_retry import FAILED_MANDATED_RESET_NARRATIVE

GATE_FAILURE_NARRATIVES: Mapping[CompletionGateKind, GateFailureNarrative] = {
    CompletionGateKind.MANDATED_RESET: FAILED_MANDATED_RESET_NARRATIVE,
    CompletionGateKind.RESULT_ONLY_CLOSE: FAILED_RESULT_ONLY_CLOSE_NARRATIVE,
}
"""Every gate kind, and what its failure tells the operator who must clear it.

Total over :class:`~.completion_effect_gate.CompletionGateKind` by construction
and by test: a gate that can withhold a completion's success-only effects but
leaves nothing durable behind is the F1 defect, not a design choice.
"""


def build_completion_gate_failure_actions(
    outcome: CompletionGateOutcome,
    *,
    issue_number: int,
    needs_human_label: str,
    session_id: str,
    runtime_minutes: float,
) -> list[Action]:
    """Durable, crash-safe operator surface for every gate that did not commit.

    Returns an EMPTY list when no gate failed — the success path, the ordinary
    genuine-failure path (whose surface the completion handler already planned),
    and the unjudged-apply path all apply nothing.

    Each failing gate contributes its own label + comment pair, built from ITS
    OWN failures: routing happens here, once, so no owner can word a report
    about a gate it does not own (#337 r3 F1) and no call site has to know which
    kinds have surfaces (#337 r4, N3).
    """
    actions: list[Action] = []
    for kind, narrative in GATE_FAILURE_NARRATIVES.items():
        failures = outcome.failures_of(kind)
        if failures:
            actions.extend(
                _surface_actions(
                    narrative,
                    failures,
                    issue_number=issue_number,
                    needs_human_label=needs_human_label,
                    session_id=session_id,
                    runtime_minutes=runtime_minutes,
                )
            )
    return actions


def _surface_actions(
    narrative: GateFailureNarrative,
    failures: Sequence[CompletionGateFailure],
    *,
    issue_number: int,
    needs_human_label: str,
    session_id: str,
    runtime_minutes: float,
) -> list[Action]:
    """One gate's blocking label and its explanation, in that order.

    The label first, deliberately: it is the half that makes the stop durable,
    and a comment posted without it would read as an escalation the scheduler
    never got.
    """
    return [
        AddLabelAction(
            issue_number=issue_number,
            label=needs_human_label,
            reason=(
                f"{narrative.subject} did not commit; routing to needs-human"
            ),
            needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
        ),
        AddCommentAction(
            number=issue_number,
            comment=_comment(
                narrative,
                failures,
                needs_human_label=needs_human_label,
                session_id=session_id,
                runtime_minutes=runtime_minutes,
            ),
            reason=(
                f"notify operator that the {narrative.subject} failed at apply"
                " time"
            ),
        ),
    ]


def _comment(
    narrative: GateFailureNarrative,
    failures: Sequence[CompletionGateFailure],
    *,
    needs_human_label: str,
    session_id: str,
    runtime_minutes: float,
) -> str:
    summary = "; ".join(failure.detail for failure in failures)
    return (
        f"**{narrative.heading}**\n\n"
        f"{narrative.explanation}\n\n"
        f"- Failure: {summary}\n"
        f"- Session: `{session_id}`\n"
        f"- Runtime: {runtime_minutes:.1f} minutes\n\n"
        f"This issue has been marked as `{needs_human_label}` because"
        f" {narrative.label_because}.\n"
        f"{narrative.remedy}"
    )


__all__ = [
    "GATE_FAILURE_NARRATIVES",
    "build_completion_gate_failure_actions",
]
