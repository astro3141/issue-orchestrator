"""Encoding for the durable pending-work claim artifact (#6999 F4).

The claim outlives the process that took it, so every field that exists nowhere
else has to survive the round trip: a failure investigation's typed
``DiscoveredFailure`` and its spent retry count, a validation retry's prompt,
error, attempt count and source task, a rework's PR number and cycle.

Encoding is explicit per kind rather than a generic ``asdict`` walk. A generic
walk silently degrades a type it does not understand — an enum to a bare value
it cannot rebuild, an ``IssueKey`` implementation to its private fields — and
the failure would only show up as work quietly reappearing wrong after a
restart. Every decoder here fails loudly on a payload it cannot rebuild.

``IssueKey`` round-trips through :mod:`..domain.issue_key_codec`, which owns the
one durable spelling of a key shared with the publish-retry locators. Encoding
it a second time here would let two artifacts naming the same work item
round-trip to keys that compare unequal.

Failing loudly is not the same as failing usefully, though (#209). This
orchestrator PINS trusted runtimes while ``main`` advances, so an older build
meeting durable state written by a newer one is a designed-for condition rather
than an accident. Every refusal here therefore carries a
:class:`~..ports.pending_work_claim_store.ClaimReadability`: an artifact this
build merely lacks the vocabulary for is intact and recoverable by the build
that wrote it, and saying otherwise sends an operator hunting for damage that
does not exist.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Callable, TypeVar

from ..domain.issue_key import IssueKey
from ..domain.issue_key_codec import (
    IssueKeyDecodeError,
    decode_issue_key,
    encode_issue_key,
)
from ..domain.models import (
    DiscoveredFailure,
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
)
from ..domain.pending_work import (
    PendingWorkClaim,
    PendingWorkKind,
    PendingWorkRequest,
)
from ..domain.session_key import TaskKind
from ..domain.tech_lead_session import TechLeadSessionFlavor
from ..ports.pending_work_claim_store import ClaimReadability, UnreadableClaimError

CLAIM_ARTIFACT_NAME = "pending-work-claim.json"
# Bumped only when an encoding change cannot be read by the previous decoder.
# A payload from a different version is refused rather than guessed at.
#
# It is deliberately NOT bumped when a persisted enum merely gains a member
# (#209). The version gate is an EQUALITY check, so a bump makes every already
# stored payload unreadable to the build that bumped it — trading a
# forward-compatibility problem for a strictly worse backward-compatibility one,
# on artifacts that are the only record of work in flight. Growth in an enum's
# value space is handled where it actually shows up instead: see
# :func:`_persisted_enum`.
CLAIM_SCHEMA_VERSION = 1

_EnumT = TypeVar("_EnumT", bound=Enum)


class PendingWorkClaimDecodeError(UnreadableClaimError):
    """A stored claim could not be rebuilt into its original typed request.

    Never raised directly — every refusal is one of the two subclasses below,
    which is what lets a caller branch on WHY without parsing a message. The
    base survives as the thing to catch when only "unreadable" matters, and as
    the name every existing caller already spells.
    """


class NewerPendingWorkClaimError(PendingWorkClaimDecodeError):
    """The payload is well-formed; this build's vocabulary is too small.

    A pinned runtime reading state written by ``main`` is the ordinary case, so
    this is a statement about the reader, not about the artifact: the bytes are
    intact, nothing has been discarded, and the build that wrote them still
    reads them.
    """

    readability = ClaimReadability.UNREADABLE_NEWER


class CorruptPendingWorkClaimError(PendingWorkClaimDecodeError):
    """The payload is a shape no build ever wrote, or contradicts itself."""

    readability = ClaimReadability.UNREADABLE_CORRUPT


def encode_claim(claim: PendingWorkClaim) -> dict[str, Any]:
    """Encode a claim for durable storage."""
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "kind": claim.kind.value,
        "request": _ENCODERS[claim.kind](claim.request),
    }


def encode_claim_text(claim: PendingWorkClaim) -> str:
    """The claim exactly as a store writes it: one canonical JSON document.

    Key order is fixed, so two encodings of the same claim compare equal as
    TEXT - which is what an idempotent re-hold rests on when it compares a
    stored payload against the one it is about to write.
    """
    return json.dumps(encode_claim(claim), sort_keys=True)


def decode_claim_text(text: object, *, source: str) -> PendingWorkClaim:
    """Rebuild a claim from stored text, with the refusal classified (#209).

    The JSON framing is part of the durable artifact, so it is decoded and
    classified HERE rather than by each store: a store that unwrapped the text
    itself had to invent its own verdict for "that is not JSON", and a verdict
    invented per call site is the untyped branch this module now exists to
    remove. Text that is not JSON at all is a shape no build ever wrote.

    ``source`` names the row for the operator - a run key, a file - and is
    never parsed.
    """
    if not isinstance(text, str):
        raise CorruptPendingWorkClaimError(
            f"stored claim for {source} is {type(text).__name__}, not text"
        )
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptPendingWorkClaimError(
            f"stored claim for {source} is unreadable: {exc}"
        ) from exc
    return decode_claim(loaded)


def decode_claim(payload: object) -> PendingWorkClaim:
    """Rebuild a claim, raising a CLASSIFIED refusal when it cannot (#209).

    Returning the claim is the ``READABLE`` verdict; the other two arrive as
    :class:`NewerPendingWorkClaimError` and :class:`CorruptPendingWorkClaimError`
    so that no caller has to read an error message to tell an intact artifact
    from a damaged one.
    """
    if not isinstance(payload, dict):
        raise CorruptPendingWorkClaimError(
            f"claim payload must be an object, got {type(payload).__name__}"
        )
    _require_supported_schema_version(payload.get("schema_version"))
    kind = _persisted_enum(PendingWorkKind, payload, "kind")
    request = payload.get("request")
    if not isinstance(request, dict):
        raise CorruptPendingWorkClaimError(
            f"{kind.value} claim payload has no request object"
        )
    try:
        return PendingWorkClaim(kind, _DECODERS[kind](request))
    except PendingWorkClaimDecodeError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptPendingWorkClaimError(
            f"{kind.value} claim payload could not be rebuilt: {exc}"
        ) from exc


def _require_supported_schema_version(version: object) -> None:
    """Refuse a version this build has no decoder for, saying which kind it is.

    A version number that is an integer is a well-formed field whatever its
    value; the artifact is simply spoken in a dialect this build does not
    implement, which is what ``UNREADABLE_NEWER`` means. Anything else — absent,
    a string, ``true`` — is a shape the encoder never produces.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        raise CorruptPendingWorkClaimError(
            f"claim payload carries no usable schema version: {version!r}"
        )
    if version != CLAIM_SCHEMA_VERSION:
        raise NewerPendingWorkClaimError(
            f"claim schema version {version} is not the {CLAIM_SCHEMA_VERSION} "
            "this build implements; the stored claim is intact and a build that "
            "implements that version reads it unchanged"
        )


def _persisted_enum(
    enum_cls: type[_EnumT], payload: dict[str, Any], field: str
) -> _EnumT:
    """Rebuild a persisted enum member, telling "unknown" apart from "wrong".

    The whole of #209 lives in this distinction. An enum's VALUE SPACE grows
    without the payload's SHAPE changing — ``TechLeadSessionFlavor`` gained
    ``planning_investigation`` in #136 — so the schema version cannot see it and
    the old runtime sails through the version gate before failing at coercion.
    Treating that as corruption told an operator an intact claim could not be
    recovered.

    A well-formed string this build's enum does not carry therefore means the
    artifact was written by a build that knows more members than this one. Only
    an ABSENT field or a value of the wrong TYPE is a shape no build ever wrote.
    """
    if field not in payload:
        raise CorruptPendingWorkClaimError(
            f"claim payload has no {field!r} field"
        )
    raw = payload[field]
    if not isinstance(raw, str):
        raise CorruptPendingWorkClaimError(
            f"claim payload field {field!r} must be a string, "
            f"got {type(raw).__name__}"
        )
    try:
        return enum_cls(raw)
    except ValueError as exc:
        raise NewerPendingWorkClaimError(
            f"claim payload field {field!r} is {raw!r}, which this build's "
            f"{enum_cls.__name__} does not carry; the claim was written by a "
            "build whose value space is larger, and is intact for that build"
        ) from exc


def _decode_issue_key(payload: object) -> IssueKey:
    # Re-raised as this artifact's own decode error: a caller catching a bad
    # claim should not have to know which shared codec spelled the failure.
    # An issue key has no value space that can grow, so a rejected one is
    # malformed rather than merely unfamiliar.
    try:
        return decode_issue_key(payload)
    except IssueKeyDecodeError as exc:
        raise CorruptPendingWorkClaimError(str(exc)) from exc


def _encode_review(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingReview)
    return {
        "issue_key": encode_issue_key(request.issue_key),
        "pr_number": request.pr_number,
        "pr_url": request.pr_url,
        "branch_name": request.branch_name,
        "issue_number": request.issue_number,
        "agent_label": request.agent_label,
        "issue_labels": list(request.issue_labels),
    }


def _decode_review(payload: dict[str, Any]) -> PendingReview:
    return PendingReview(
        issue_key=_decode_issue_key(payload["issue_key"]),
        pr_number=int(payload["pr_number"]),
        pr_url=str(payload["pr_url"]),
        branch_name=str(payload["branch_name"]),
        _issue_number=int(payload["issue_number"]),
        agent_label=payload["agent_label"],
        issue_labels=tuple(payload["issue_labels"]),
    )


def _encode_retrospective_review(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingRetrospectiveReview)
    return {
        "issue_key": encode_issue_key(request.issue_key),
        "issue_number": request.issue_number,
        "issue_title": request.issue_title,
        "agent_label": request.agent_label,
        "trigger_label": request.trigger_label,
        "prior_pr_number": request.prior_pr_number,
        "prior_pr_url": request.prior_pr_url,
        "issue_labels": list(request.issue_labels),
    }


def _decode_retrospective_review(
    payload: dict[str, Any],
) -> PendingRetrospectiveReview:
    return PendingRetrospectiveReview(
        issue_key=_decode_issue_key(payload["issue_key"]),
        issue_number=int(payload["issue_number"]),
        issue_title=str(payload["issue_title"]),
        agent_label=str(payload["agent_label"]),
        trigger_label=str(payload["trigger_label"]),
        prior_pr_number=payload["prior_pr_number"],
        prior_pr_url=payload["prior_pr_url"],
        issue_labels=tuple(payload["issue_labels"]),
    )


def _encode_rework(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingRework)
    return {
        "issue_key": encode_issue_key(request.issue_key),
        "agent_type": request.agent_type,
        "rework_cycle": request.rework_cycle,
        "issue_number": request.issue_number,
        "pr_number": request.pr_number,
        "source": request.source,
        "feedback": request.feedback,
    }


def _decode_rework(payload: dict[str, Any]) -> PendingRework:
    return PendingRework(
        issue_key=_decode_issue_key(payload["issue_key"]),
        agent_type=str(payload["agent_type"]),
        rework_cycle=int(payload["rework_cycle"]),
        issue_number=payload["issue_number"],
        pr_number=payload["pr_number"],
        source=str(payload["source"]),
        feedback=payload["feedback"],
    )


def _encode_validation_retry(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingValidationRetry)
    return {
        "issue_number": request.issue_number,
        "issue_title": request.issue_title,
        "agent_label": request.agent_label,
        "worktree_path": request.worktree_path,
        "branch_name": request.branch_name,
        "original_prompt": request.original_prompt,
        "validation_error": request.validation_error,
        "validation_error_file": request.validation_error_file,
        "retry_count": request.retry_count,
        "source_task": request.source_task.value,
        "validation_cmd": request.validation_cmd,
    }


def _decode_validation_retry(payload: dict[str, Any]) -> PendingValidationRetry:
    return PendingValidationRetry(
        issue_number=int(payload["issue_number"]),
        issue_title=str(payload["issue_title"]),
        agent_label=str(payload["agent_label"]),
        worktree_path=str(payload["worktree_path"]),
        branch_name=str(payload["branch_name"]),
        original_prompt=payload["original_prompt"],
        validation_error=str(payload["validation_error"]),
        validation_error_file=payload["validation_error_file"],
        retry_count=int(payload["retry_count"]),
        source_task=_persisted_enum(TaskKind, payload, "source_task"),
        validation_cmd=payload["validation_cmd"],
    )


def _encode_tech_lead(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingTechLeadReview)
    return {
        "issue_number": request.issue_number,
        "title": request.title,
        "flavor": request.flavor.value,
        "failure": request.failure.to_dict() if request.failure else None,
        "problem_cohort": [
            failure.to_dict() for failure in request.problem_cohort
        ],
        "retryable_launch_failures": request.retryable_launch_failures,
    }


def _decode_tech_lead(payload: dict[str, Any]) -> PendingTechLeadReview:
    failure_payload = payload["failure"]
    item = PendingTechLeadReview(
        issue_number=int(payload["issue_number"]),
        title=str(payload["title"]),
        flavor=_persisted_enum(TechLeadSessionFlavor, payload, "flavor"),
        failure=(
            DiscoveredFailure.from_dict(failure_payload)
            if failure_payload is not None
            else None
        ),
        problem_cohort=tuple(
            DiscoveredFailure.from_dict(entry)
            for entry in payload["problem_cohort"]
        ),
    )
    # Not a constructor argument: the retry budget is owner-tracked state, and
    # a restart must not silently refund what a previous process already spent.
    item.retryable_launch_failures = int(payload["retryable_launch_failures"])
    return item


_ENCODERS: dict[PendingWorkKind, Callable[[PendingWorkRequest], dict[str, Any]]] = {
    PendingWorkKind.REVIEW: _encode_review,
    PendingWorkKind.RETROSPECTIVE_REVIEW: _encode_retrospective_review,
    PendingWorkKind.REWORK: _encode_rework,
    PendingWorkKind.VALIDATION_RETRY: _encode_validation_retry,
    PendingWorkKind.TECH_LEAD: _encode_tech_lead,
}

_DECODERS: dict[PendingWorkKind, Callable[[dict[str, Any]], PendingWorkRequest]] = {
    PendingWorkKind.REVIEW: _decode_review,
    PendingWorkKind.RETROSPECTIVE_REVIEW: _decode_retrospective_review,
    PendingWorkKind.REWORK: _decode_rework,
    PendingWorkKind.VALIDATION_RETRY: _decode_validation_retry,
    PendingWorkKind.TECH_LEAD: _decode_tech_lead,
}

# Every kind must be encodable AND decodable. A kind added to the enum without
# both halves would otherwise only fail at the moment a real session held it.
assert set(_ENCODERS) == set(PendingWorkKind)
assert set(_DECODERS) == set(PendingWorkKind)


__all__ = [
    "CLAIM_ARTIFACT_NAME",
    "CLAIM_SCHEMA_VERSION",
    "ClaimReadability",
    "CorruptPendingWorkClaimError",
    "NewerPendingWorkClaimError",
    "PendingWorkClaimDecodeError",
    "decode_claim",
    "decode_claim_text",
    "encode_claim",
    "encode_claim_text",
]
