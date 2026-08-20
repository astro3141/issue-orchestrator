"""The completion record a continuation replays (#149, #173).

The completion pipeline is entered with a completion record, and a
continuation has no agent to write one: the intent was recorded when the
candidate's own session ended, and the session is gone. So this composes the
record the pipeline reads from three sources, and from nothing else.

======================  ===============================================
the agent's fields      the durable :class:`~..domain.continuation_descriptor.ContinuationDescriptor`, copied verbatim
the run's identity      the orchestrator: session name, timestamp
the run's evidence      the record this run's quick gate just wrote (#173)
======================  ===============================================

Nothing is invented in between. ``summary`` names this replay for a human
reading the record and carries no claim about the work; ``validation_record_
path`` names a file that exists, never a path guessed from a naming
convention. It lives beside the runner rather than inside it because it is a
composition rule with three named sources — the one place a fabricated field
could enter a continuation — and a rule that can only be exercised through the
runner is a rule nothing states.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import CompletionOutcome, CompletionRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.continuation_descriptor import ContinuationDescriptor

REPLAY_SUMMARY = "Recorded continuation intent replayed by the orchestrator"
"""What this record says it is, to a human who opens it."""


def write_continuation_completion_record(
    path: Path,
    descriptor: "ContinuationDescriptor",
    *,
    session_name: str,
    validation_record_path: Path | None,
) -> None:
    """Put the recorded intent where the completion owner reads intent.

    Args:
        path: Where the completion pipeline will read this run's record.
        descriptor: The durable intent, whose fields are copied field for
            field. Nothing is added to what the agent recorded.
        session_name: This run's identity, which the orchestrator owns.
        validation_record_path: The quick-validation record this run just
            produced (#173). On the ordinary path the same field names what
            the coder turn's gate produced, and the review exchange mirrors
            exactly the file it names. ``None`` means the run's profile
            configures no quick contract, so there is no evidence to name —
            the honest absence, not a placeholder.
    """
    record = CompletionRecord(
        session_id=session_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary=REPLAY_SUMMARY,
        requested_actions=list(descriptor.requested_actions),
        implementation=descriptor.implementation or None,
        problems=descriptor.problems or None,
        validation_record_path=(
            None if validation_record_path is None else str(validation_record_path)
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")


__all__ = ["REPLAY_SUMMARY", "write_continuation_completion_record"]
