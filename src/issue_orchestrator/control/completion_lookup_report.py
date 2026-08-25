"""Reporting support for the completion-record lookup a decision makes.

The counterpart to ``invalid_completion_record``: that module gives a
rejected record its voice, this one gives the *selection* its voice.

The split it exists to hold is between polling and deciding.
``select_completion_record`` is asked by the observer on every
``observe_session`` for every live session, and the conditions it meets
persist across ticks — a malformed record is still malformed next tick,
and a producer-error placeholder no retry resolves stays unresolved until
the session ends. So the owner explains itself at DEBUG. A decision
happens once, which is what makes this the place where an operator gets
told what was chosen, what it cost, and what still needs a human.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .completion_record_validation import (
    CompletionPathChoice,
    CompletionRecordSelection,
)

logger = logging.getLogger(__name__)


def report_completion_lookup(
    *,
    worktree_path: Path,
    issue_number: int,
    session_name: str,
    completion_path: str | None,
    default_completion_path: str,
    selection: CompletionRecordSelection,
    events: EventSink,
) -> None:
    """Log and emit which completion record the decision is about to read.

    Reports the path the selection owner made authoritative rather than
    re-deriving the canonical one: a lookup line naming a file the
    decision did not read is exactly what made #264 invisible.
    """
    full_path = selection.path.resolve()
    logger.info(
        issue_log(
            issue_number, "Session not running: session=%s checking_completion=%s"
        ),
        session_name,
        completion_path or default_completion_path,
    )
    exists = full_path.exists()
    payload: dict[str, Any] = {
        "issue_number": issue_number,
        "session_name": session_name,
        "worktree_path": str(worktree_path.resolve()),
        "completion_path": completion_path,
        "full_path": str(full_path),
        "file_exists": exists,
    }
    payload.update(selection.lookup_fields())
    events.publish(make_trace_event(EventName.COMPLETION_LOOKUP, payload))
    logger.info(
        issue_log(
            issue_number,
            "Completion lookup: exists=%s size=%s path=%s choice=%s",
        ),
        exists,
        full_path.stat().st_size if exists else None,
        full_path,
        selection.choice.value,
    )
    _report_selection_verdict(
        issue_number=issue_number,
        session_name=session_name,
        selection=selection,
    )


def _report_selection_verdict(
    *,
    issue_number: int,
    session_name: str,
    selection: CompletionRecordSelection,
) -> None:
    """Say what the selection had to work around, or refused to resolve."""
    producer_error = selection.producer_error
    if producer_error:
        # A repaired retry must not erase the fact that the first
        # completion command failed (#264).
        logger.info(
            issue_log(
                issue_number,
                "Completion producer error preserved for session=%s at %s: %s",
            ),
            session_name,
            selection.canonical_path,
            producer_error,
        )
    if selection.choice is CompletionPathChoice.AMBIGUOUS_PRODUCER_ERROR_RETRY:
        # The one condition here a human has to resolve by hand: two valid
        # retries for the same run, so the owner failed closed onto the
        # placeholder and this session is about to fail on a rejected
        # record. Name the candidates so the operator does not have to go
        # find them.
        logger.warning(
            issue_log(
                issue_number,
                "Ambiguous completion retry for session=%s beside %s: %s; "
                "refusing to choose",
            ),
            session_name,
            selection.canonical_path,
            ", ".join(str(path) for path in selection.unresolved_candidates),
        )
