"""What a coder's completion asks the review exchange to do with it (#386).

The exchange used to read the coder's ``completion-coder.json`` for exactly
one thing: whether a passing validation record could be mirrored into pair
scope. The ``outcome`` field — the only field that says what the coder
actually concluded — was never consulted. A ``needs_human`` turn therefore had
two ways to end, and neither was the escalation:

* HEAD did not move, so the previous round's validation record still named the
  current commit. The envelope validated, the round advanced, and the question
  the coder asked disappeared.
* HEAD did move because the coder committed before escalating. The prior
  record named the old commit, and the escalation was rejected as a
  validation-record mismatch — a publication failure standing in for a
  question nobody was publishing.

:class:`CoderCompletionIntent` is the missing reader. It answers the two
questions the exchange has to ask of a completion — *does this escalate?* and
*does this ask to reach the remote?* — from the vocabulary the completion
record already speaks (:class:`~.models.CompletionOutcome`,
:data:`~.models.PUBLICATION_ACTIONS`), so escalation and publication authority
cannot drift apart from how the rest of the system spells them.

It reads leniently on purpose, and that is not the same as reading loosely.
:meth:`CompletionRecord.from_dict` is the strict reader for a record the
*orchestrator* is about to act on; this is the exchange asking a narrower
question of a mid-exchange artifact, and a record too malformed to answer it
is reported as an unknown outcome, which requires validation evidence exactly
as an ordinary turn does. Being unreadable can therefore never be the cheap
way to reach the escalation terminal.

:class:`CoderEscalation` is what the exchange records once the answer is
"escalate": the question, bound to the exact issue, session, round, and the
commit the coder's worktree holds *now*. Binding to current HEAD rather than
to a validation record is the whole point — an escalation requests no
publication, so there is no publication evidence for it to name, and demanding
some is what turned a question into a rejection.

An escalation grants no authority. It does not approve a commit, it does not
push, and it does not create anything on GitHub: it names a decision that
belongs to a human and stops.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import (
    PUBLICATION_ACTIONS,
    CompletionOutcome,
    RequestedAction,
)

ESCALATION_RECORD_FILENAME = "coder-escalation.json"
"""Filename of the exchange-scoped escalation record."""


@dataclass(frozen=True, slots=True)
class CoderCompletionIntent:
    """The two things a review exchange must know about a coder completion.

    ``outcome`` is ``None`` when the payload names no outcome the completion
    vocabulary recognizes. That is deliberately the *conservative* value: an
    unknown outcome escalates nothing and is held to the same publication
    evidence an ordinary completed turn is.
    """

    outcome: CompletionOutcome | None
    requested_actions: tuple[RequestedAction, ...]
    question: str | None = None
    context: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CoderCompletionIntent":
        """Read the intent out of a raw ``completion-coder.json`` payload."""
        return cls(
            outcome=_known_outcome(payload.get("outcome")),
            requested_actions=_known_actions(payload.get("requested_actions")),
            question=_text(payload.get("question")),
            context=_text(payload.get("context")),
        )

    @property
    def escalates_to_human(self) -> bool:
        """Whether the coder handed the next decision to a human."""
        return self.outcome is CompletionOutcome.NEEDS_HUMAN

    @property
    def requests_publication(self) -> bool:
        """Whether the same turn also asks to reach the remote."""
        return bool(PUBLICATION_ACTIONS & set(self.requested_actions))

    @property
    def requires_publication_evidence(self) -> bool:
        """Whether this turn must still present current-head validation.

        True for every ordinary turn, and true for an escalation that asks to
        publish in the same breath. Only the escalation that requests nothing
        from the remote is exempt, because there is nothing for the evidence
        to authorize: it asks a question, and a question publishes nothing.
        """
        return not self.escalates_to_human or self.requests_publication


@dataclass(frozen=True, slots=True)
class CoderEscalation:
    """A coder's ``needs_human`` turn, bound to what raised it.

    Every field is an identity the escalation is answerable for. ``head_sha``
    is the commit the coder's worktree holds at the moment the escalation was
    read, so a reader can tell whether the question still describes the
    working copy in front of them.
    """

    issue_number: int
    session_name: str
    round_index: int
    head_sha: str
    raised_at: str
    question: str | None = None
    context: str | None = None
    requested_publication: bool = False

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int:
            raise TypeError("issue_number must be an int")
        _require_non_empty_str(self.session_name, "session_name")
        if type(self.round_index) is not int:
            raise TypeError("round_index must be an int")
        if self.round_index < 1:
            raise ValueError("round_index must be >= 1")
        _require_non_empty_str(self.head_sha, "head_sha")
        _require_non_empty_str(self.raised_at, "raised_at")
        if type(self.requested_publication) is not bool:
            raise TypeError("requested_publication must be a bool")

    @property
    def detail(self) -> str:
        """One line naming the escalation, for events and summary detail."""
        asked = self.question or "no question text supplied"
        return (
            f"coder escalated to human at {self.head_sha[:12]} "
            f"(round {self.round_index}): {asked}"
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issue_number": self.issue_number,
            "session_name": self.session_name,
            "round_index": self.round_index,
            "head_sha": self.head_sha,
            "raised_at": self.raised_at,
            "requested_publication": self.requested_publication,
        }
        if self.question is not None:
            payload["question"] = self.question
        if self.context is not None:
            payload["context"] = self.context
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CoderEscalation":
        issue_number = payload.get("issue_number")
        round_index = payload.get("round_index")
        if not isinstance(issue_number, int) or isinstance(issue_number, bool):
            raise ValueError("coder escalation requires int issue_number")
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            raise ValueError("coder escalation requires int round_index")
        requested_publication = payload.get("requested_publication", False)
        if not isinstance(requested_publication, bool):
            raise ValueError("coder escalation requested_publication must be bool")
        return cls(
            issue_number=issue_number,
            session_name=_required_str(payload, "session_name"),
            round_index=round_index,
            head_sha=_required_str(payload, "head_sha"),
            raised_at=_required_str(payload, "raised_at"),
            question=_text(payload.get("question")),
            context=_text(payload.get("context")),
            requested_publication=requested_publication,
        )


def _known_outcome(raw: Any) -> CompletionOutcome | None:
    try:
        return CompletionOutcome(raw)
    except ValueError:
        return None


def _known_actions(raw: Any) -> tuple[RequestedAction, ...]:
    if not isinstance(raw, list):
        return ()
    actions: list[RequestedAction] = []
    for item in raw:
        try:
            actions.append(RequestedAction(item))
        except ValueError:
            continue
    return tuple(actions)


def _text(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"coder escalation requires non-empty {key}")
    return value


def _require_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "ESCALATION_RECORD_FILENAME",
    "CoderCompletionIntent",
    "CoderEscalation",
]
