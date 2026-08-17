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

Naming a work item *in a path* is the same obligation one layer down, so
:func:`issue_key_path_part` lives here too: the attempt sidecar and the publish
gate's durable failure diagnostic are filed per candidate under that one
spelling, which is what lets a reader holding one find the other.

The attempt sidecar (``domain.attempt``) is not a client of this codec and
should not become one: it persists a type tag alongside the two values and
rebuilds the original implementation, because it is the record every other
piece of a candidate's evidence is filed against and refuses to guess. What the
two agree on is what identity *is* — scope plus stable id — which is also what
``AttemptStore`` keys on, so a key rebuilt here reaches the same attempt.
"""

from __future__ import annotations

import re
from typing import Any

from .issue_key import GitHubIssueKey, IssueKey

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


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


def issue_key_path_part(key: IssueKey) -> str:
    """The one path-safe spelling of a work item identity.

    Artifacts filed *per candidate* name the candidate in a path, and they must
    agree on that name for the same reason :func:`encode_issue_key` exists: the
    attempt sidecar for ``(issue, A)`` and the publish gate's durable failure
    diagnostic for ``(issue, A)`` are two pieces of evidence about one
    candidate, and a reader that has one has to be able to find the other
    without a pointer. Two spellings would put them under two names, in the
    same directory tree, with nothing saying they were about the same work.

    Built from the same two values the codec persists — scope plus stable id —
    so the path and the payload cannot name different work items. Only the
    characters a path may not safely carry are folded away; the transform is
    deliberately lossy and is therefore never decoded back into a key.
    """
    return _path_safe(f"{key.scope()}--{key.stable_id()}")


def _path_safe(value: str) -> str:
    safe = _UNSAFE_PATH_CHARS.sub("-", value.strip())
    if not safe:
        raise ValueError("issue key path component must be non-empty")
    return safe


__all__ = [
    "IssueKeyDecodeError",
    "decode_issue_key",
    "encode_issue_key",
    "issue_key_path_part",
]
