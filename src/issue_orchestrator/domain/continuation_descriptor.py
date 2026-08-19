"""The recorded intent one exact candidate's continuation may act on (#143, #149).

A publication that fails takes its worktree with it. What dies with the
worktree is not the artifact — the commit survives on its branch, and #85/#139
kept the verdict — but the *completion record*: the only place the agent ever
said what it wanted done with the work, and what it claimed to have done. Once
that file is gone there is no durable answer to "may this candidate become a
pull request", and #143 measured the consequence precisely: every remaining
source of that answer (issue text, labels, logs, the failure diagnostic, the
branch name) is either the orchestrator's own guess or a diagnostic that
declares itself non-authoritative.

So this descriptor exists to copy — never to derive. Each field has exactly one
authoritative producer, and is taken from it verbatim:

===========================  ===================================================
``requested_actions``        the agent's completion record
``implementation``           the agent's completion record
``problems``                 the agent's completion record
``suite`` / ``command`` /    the publication verdict receipt the gate just filed
``profile``
===========================  ===================================================

The candidate binding — ``issue_key`` and ``head_sha`` — is deliberately NOT a
field. A descriptor is stored on :class:`~.attempt.Attempt`, whose key already
*is* that pair, so there is no second spelling of the binding that could
disagree with the record it is filed under.

**Absence means no recorded intent, never empty intent.** A missing descriptor
makes the continuation impossible rather than permissive: no PR, no review, no
ownership. That asymmetry is the whole point, and it is why nothing in this
module has a default that could be mistaken for a recorded value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import RequestedAction

CONTINUATION_DESCRIPTOR_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ContinuationDescriptor:
    """One candidate's copied completion intent and publication contract identity."""

    requested_actions: tuple[RequestedAction, ...]
    implementation: str
    problems: str
    suite: str
    command: str
    profile: str
    schema_version: int = CONTINUATION_DESCRIPTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Coerced through the enum, as every other value object here coerces
        # its own vocabulary: an action outside the closed set raises rather
        # than being carried as an opaque string the PR gate would silently
        # not match.
        object.__setattr__(
            self,
            "requested_actions",
            tuple(RequestedAction(action) for action in self.requested_actions),
        )
        for field_name in ("suite", "command", "profile"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"ContinuationDescriptor.{field_name} must be a non-empty str"
                )
        for field_name in ("implementation", "problems"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(
                    f"ContinuationDescriptor.{field_name} must be a str"
                )
        if (
            type(self.schema_version) is not int
            or self.schema_version != CONTINUATION_DESCRIPTOR_SCHEMA_VERSION
        ):
            # Fails closed for the reason every other authority record here
            # does: a version this code does not know is a record written by a
            # schema it cannot claim to understand, and continuation acts on
            # what it reads.
            raise ValueError(
                "continuation descriptor schema_version must be "
                f"{CONTINUATION_DESCRIPTOR_SCHEMA_VERSION}, got "
                f"{self.schema_version!r}"
            )

    @property
    def creates_pr(self) -> bool:
        """Whether the agent asked for a pull request.

        The one question the PR half of the continuation may ask, and it is
        answered by the recorded intent alone. A candidate that passed the gate
        has proved something about the artifact; it has said nothing about what
        the agent wanted done with it.
        """
        return RequestedAction.CREATE_PR in self.requested_actions

    def matches_contract(self, *, suite: str, command: str, profile: str) -> bool:
        """Whether this descriptor was recorded under the same contract identity."""
        return (
            self.suite == suite
            and self.command == command
            and self.profile == profile
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requested_actions": [action.value for action in self.requested_actions],
            "implementation": self.implementation,
            "problems": self.problems,
            "suite": self.suite,
            "command": self.command,
            "profile": self.profile,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ContinuationDescriptor":
        """Parse a stored descriptor, refusing anything it cannot read exactly.

        Damage raises rather than reading as absence. The two states a reader
        must never confuse are "the agent recorded no intent" and "the record of
        what it wanted is corrupt": the first forbids continuation, the second
        is a broken instrument, and a parser that silently returned ``None``
        would report the second as the first.
        """
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("continuation descriptor requires int schema_version")
        return cls(
            requested_actions=_requested_actions(payload.get("requested_actions")),
            implementation=_text(payload.get("implementation"), "implementation"),
            problems=_text(payload.get("problems"), "problems"),
            suite=_text(payload.get("suite"), "suite"),
            command=_text(payload.get("command"), "command"),
            profile=_text(payload.get("profile"), "profile"),
            schema_version=schema_version,
        )


def _requested_actions(value: object) -> tuple[RequestedAction, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(
            "continuation descriptor requested_actions must be a list"
        )
    actions: list[RequestedAction] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(
                "continuation descriptor requested_actions must hold strings"
            )
        try:
            actions.append(RequestedAction(entry))
        except ValueError as exc:
            raise ValueError(
                f"unknown continuation descriptor requested action: {entry!r}"
            ) from exc
    return tuple(actions)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"continuation descriptor {field_name} must be a str"
        )
    return value


__all__ = [
    "CONTINUATION_DESCRIPTOR_SCHEMA_VERSION",
    "ContinuationDescriptor",
]
