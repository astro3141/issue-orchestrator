"""What one coder turn's artifact means, and how an escalation ends (#386).

Two things live here, together and on purpose.

:func:`read_coder_turn` is the single read of ``completion-coder.json``. It
answers both questions the exchange has of a coder turn — *did it keep the
protocol?* and *what did it conclude?* — because they were never two reads of
two files. Before #386 only the first was asked: the envelope and the
validation binding were checked, the ``outcome`` field was not consulted by
anybody, and a ``needs_human`` turn whose HEAD had not moved passed every
check there was and vanished into an ordinary round.

:func:`build_outcome_for_coder_escalation` closes the exchange at the terminal
that answer deserves. It is beside the reader rather than beside the other
terminal builders because the escalation's binding is the reader's output: the
commit the question was raised against is carried from one to the other, and
nothing in between may re-observe it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.review_exchange import ReviewExchangeOutcome, ReviewExchangeResponse
from ..domain.review_exchange_escalation import (
    CoderCompletionIntent,
    CoderEscalation,
)
from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..domain.review_exchange_summary import ReviewExchangeReason, ReviewExchangeStatus
from ..events import EventName
from ..ports import (
    make_review_exchange_completed_event,
    make_review_exchange_round_completed_event,
)
from .review_exchange_records import record_coder_escalation, write_exchange_summary
from .review_exchange_terminals import emit_built_event
from .review_exchange_validation_mirror import PairValidationMirror

EmitEvent = Callable[[EventName, dict[str, Any]], None]

CODER_ESCALATED_RESPONSE_TYPE = "escalated_to_human"
"""How the round event names a coder turn that handed the decision over.

Named once, beside the terminal that emits it: the round event's
``coder_response_type`` is a vocabulary UI and tests read, and its siblings
(``protocol_error``, the raw agent response types) each already have exactly
one producer."""


@dataclass(frozen=True, slots=True)
class CoderTurnDisposition:
    """What the coder's completion artifact means for this round.

    At most one field is set. ``protocol_error`` is the retry-and-then-fail
    path the coder protocol has always had; ``escalation`` is the #386
    terminal. Both being ``None`` is the ordinary turn that continues.
    """

    protocol_error: str | None = None
    escalation: CoderEscalation | None = None


@dataclass(frozen=True)
class CoderTurnRead:
    """Everything needed to decide what one coder turn's artifact means."""

    completion_path: Path
    pair_validation: PairValidationMirror
    run_validation_record_path: Path
    require_validation: bool
    issue_number: int
    session_name: str
    round_index: int


def read_coder_turn(command: CoderTurnRead) -> CoderTurnDisposition:
    """Read the coder's turn artifact and say what it asks the exchange to do.

    The coder must produce a completion-coder.json artifact (the
    ``coding-done`` CLI's output). A coder that only writes the review-response
    file but skips coding-done would otherwise advance the exchange by
    accident.

    What that artifact then means depends on what the coder concluded, which
    is why the outcome is read before the validation gate is applied rather
    than after:

    * an ordinary turn, and an escalation that asks to publish in the same
      breath, must present a passing validation record naming current HEAD
      when ``require_validation`` is on. Escalating grants no publication
      authority, so asking for both keeps every publication prerequisite;
    * an escalation that asks for no publication is bound to the coder
      worktree's current HEAD and returned. It is not held to publish
      evidence it never claimed, and a stale record that happens to match
      cannot turn it back into an ordinary completed round.
    """
    pair_validation = command.pair_validation
    payload, envelope_error = _read_completion_payload(command.completion_path)
    if envelope_error is not None:
        return CoderTurnDisposition(protocol_error=envelope_error)
    intent = CoderCompletionIntent.from_payload(payload)
    # Mirrored unconditionally, exactly as before: the pair record's freshness
    # contract is about what evidence EXISTS, not about who is going to
    # demand it, so an escalation still invalidates a superseded record.
    validation_source_error = pair_validation.refresh_from_completion(
        payload,
        run_validation_record_path=command.run_validation_record_path,
    )
    if command.require_validation and intent.requires_publication_evidence:
        error = validation_source_error or pair_validation.current_validation_error()
        if error is not None:
            return CoderTurnDisposition(protocol_error=error)
    if not intent.escalates_to_human:
        return CoderTurnDisposition()
    head_sha = pair_validation.observe_candidate_head()
    if head_sha is None:
        return CoderTurnDisposition(
            protocol_error=(
                "cannot determine current HEAD to bind the coder's escalation"
            )
        )
    return CoderTurnDisposition(
        escalation=CoderEscalation(
            issue_number=command.issue_number,
            session_name=command.session_name,
            round_index=command.round_index,
            head_sha=head_sha,
            raised_at=datetime.now(timezone.utc).isoformat(),
            question=intent.question,
            context=intent.context,
            requested_publication=intent.requests_publication,
        )
    )


@dataclass(frozen=True)
class CoderEscalationTerminal:
    """The exchange state one coder escalation closes over."""

    run_assets: ReviewExchangeRunAssets
    exchange_dir: Path
    round_index: int
    escalation: CoderEscalation
    issue_number: int
    session_name: str
    emit: EmitEvent
    last_reviewer: ReviewExchangeResponse | None = None
    last_coder: ReviewExchangeResponse | None = None


def build_outcome_for_coder_escalation(
    terminal: CoderEscalationTerminal,
) -> ReviewExchangeOutcome:
    """Close the exchange at the coder's ``needs_human`` terminal (#386).

    ``stopped``, not ``error``: the coder kept the protocol and the exchange
    produced a real answer — just not one the exchange is allowed to settle.
    The reason is its own so a reader can tell this apart from a coder that
    stopped converging or one that broke the contract, without parsing prose.

    The escalation record is written before the summary. A summary naming a
    terminal whose evidence is not yet on disk is a window in which a crashed
    orchestrator resumes into "escalated, but there is no question here"; the
    reverse ordering leaves the harmless one, an unreferenced record that the
    next exchange overwrites.

    No validation record is read. An escalation that requested no publication
    produced no publish evidence, so there is nothing to summarize, and the
    commit the summary must name is the one the escalation was raised
    against.
    """
    escalation = terminal.escalation
    last_reviewer = terminal.last_reviewer
    last_coder = terminal.last_coder
    emit = terminal.emit
    record_coder_escalation(
        exchange_dir=terminal.exchange_dir,
        escalation=escalation,
    )
    summary = write_exchange_summary(
        terminal.exchange_dir, terminal.round_index,
        status=ReviewExchangeStatus.STOPPED,
        reason=ReviewExchangeReason.CODER_ESCALATED_TO_HUMAN,
        reviewer_response=last_reviewer,
        validation_record_path=None,
        bound_head_sha=escalation.head_sha,
        detail=escalation.detail,
    )
    emit_built_event(emit, make_review_exchange_round_completed_event({
        "issue_number": terminal.issue_number,
        "session_name": terminal.session_name,
        "round_index": terminal.round_index,
        "reviewer_response_type": (
            last_reviewer.response_type if last_reviewer else None
        ),
        "reviewer_response_text": (
            last_reviewer.response_text if last_reviewer else None
        ),
        "coder_response_type": CODER_ESCALATED_RESPONSE_TYPE,
        "coder_response_text": last_coder.response_text if last_coder else None,
        "detail": escalation.detail,
    }))
    emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": terminal.issue_number,
        "session_name": terminal.session_name,
        "rounds": terminal.round_index,
        "status": ReviewExchangeStatus.STOPPED.value,
        "reason": ReviewExchangeReason.CODER_ESCALATED_TO_HUMAN.value,
        "detail": escalation.detail,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.STOPPED,
        rounds=terminal.round_index,
        reason=ReviewExchangeReason.CODER_ESCALATED_TO_HUMAN,
        run_assets=terminal.run_assets,
        reviewer_response=last_reviewer,
        summary=summary,
    )



def _read_completion_payload(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read the completion artifact as a JSON object, or say why it is not one."""
    if not path.exists():
        return {}, f"missing completion artifact: {path}"
    if path.stat().st_size <= 0:
        return {}, f"completion artifact is empty: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, f"completion artifact is not valid JSON: {path}"
    if not isinstance(payload, dict):
        return {}, f"completion artifact must be a JSON object: {path}"
    return payload, None


__all__ = [
    "CODER_ESCALATED_RESPONSE_TYPE",
    "CoderEscalationTerminal",
    "CoderTurnDisposition",
    "CoderTurnRead",
    "build_outcome_for_coder_escalation",
    "read_coder_turn",
]
