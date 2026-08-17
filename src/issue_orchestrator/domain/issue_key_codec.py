"""The one durable spelling of an ``IssueKey``.

Several artifacts outlive the process that wrote them and have to name the work
item they belong to: the pending-work claim, and (since #85) the publish-retry
locators whose republish files a verdict on ``Attempt(issue, A)``. They must
agree on what a persisted key looks like, because they name the *same* work
item — a second spelling would round-trip to a key that compares unequal to the
first, and the only symptom would be evidence quietly filed under an identity
nothing else uses.

``IssueKey`` is a protocol, so its implementations cannot be reconstructed
generically: a walk over an implementation's private fields cannot know which
of them are identity. The protocol defines identity as structural over exactly
scope + stable id, so those two values are what is written, and what comes back
is a :class:`GitHubIssueKey`. That is not a downgrade — the rebuilt key *is* the
same work item by the protocol's own contract.

Decoding fails loudly. A payload that cannot produce a key is corruption, and a
silently-absent identity is precisely the "never gated" reading #85 exists to
remove.

The attempt sidecar (``domain.attempt``) is not a client of this codec and
should not become one: it persists a type tag alongside the two values and
rebuilds the original implementation, because it is the record every other
piece of a candidate's evidence is filed against and refuses to guess. What the
two agree on is what identity *is* — scope plus stable id — which is also what
``AttemptStore`` keys on, so a key rebuilt here reaches the same attempt.
"""

from __future__ import annotations

from typing import Any

from .issue_key import GitHubIssueKey, IssueKey


class IssueKeyDecodeError(ValueError):
    """A stored payload could not be rebuilt into a work item identity."""


def encode_issue_key(key: IssueKey) -> dict[str, str]:
    """Encode a key as the two values the protocol defines identity over."""
    return {"scope": key.scope(), "stable_id": str(key.stable_id())}


def decode_issue_key(payload: object) -> IssueKey:
    """Rebuild a key, raising when the payload cannot produce one."""
    if not isinstance(payload, dict):
        raise IssueKeyDecodeError("issue key payload must be an object")
    data: dict[str, Any] = payload
    try:
        return GitHubIssueKey(
            repo=str(data["scope"]), external_id=str(data["stable_id"])
        )
    except KeyError as exc:
        raise IssueKeyDecodeError(
            f"issue key payload is missing {exc.args[0]!r}"
        ) from exc


__all__ = [
    "IssueKeyDecodeError",
    "decode_issue_key",
    "encode_issue_key",
]
