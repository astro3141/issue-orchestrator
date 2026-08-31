"""The admitted executable leaf contract one review exchange consumes.

A review exchange used to hand the Reviewer an issue *title* and a diff.
The Coder held the executable leaf contract — the issue body that says
which mutation was admitted — and the Reviewer did not, so a Reviewer
could correctly find a defect whose repair lay outside the admitted
contract and demand it as ordinary rework. The Coder complied and the
candidate widened past what Control had admitted (#399, measured on
#398).

This module owns the two values that close that gap:

``AdmittedLeafContract``
    The contract as the orchestrator read it from the canonical source,
    before the exchange starts. Its ``digest`` is over the contract bytes
    alone, so two exchanges that consumed the same issue body agree on
    one string regardless of who staged it.

``StagedLeafContract``
    The handle to the exact staged bytes on disk: path, issue identity,
    digest. This is what a turn packet carries, what a prompt points at,
    and what proves *which bytes* an exchange consumed. Both roles in one
    exchange carry the same handle; a reader comparing their persisted
    turn packets can prove it.

Everything here is pure: no filesystem, no network. Staging and loading
the artifact — the I/O half — belongs to
``execution/review_exchange_leaf_contract.py``, which raises
:class:`LeafContractUnavailable` on every way the evidence can be
missing, unreadable, malformed, or mismatched. There is no fallback to
issue title, PR prose, or labels: an exchange without exact contract
bytes must fail closed rather than review against an approximation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEAF_CONTRACT_FILENAME = "issue-contract.md"
"""Exact contract bytes, staged under the exchange evidence directory."""

LEAF_CONTRACT_MANIFEST_FILENAME = "issue-contract.json"
"""Attribution sidecar naming the issue and digest of the bytes beside it."""

LEAF_CONTRACT_MANIFEST_SCHEMA = "review_exchange_leaf_contract.v1"

_DIGEST_PREFIX = "sha256:"


class LeafContractUnavailable(Exception):
    """The exchange cannot prove which contract bytes it would consume.

    Raised by every producer and reader of the staged artifact. It is
    deliberately one exception for "the issue could not be read", "the
    staged file is gone", "the sidecar is malformed", and "the digest
    does not match the bytes": each of them leaves the exchange unable to
    say what scope it was reviewing against, and the required response to
    all four is the same — refuse the exchange, authorize nothing.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def leaf_contract_digest(body: str) -> str:
    """Digest the contract bytes an exchange consumes.

    Over the UTF-8 encoding of the body alone — not the issue number, not
    the title, not the staged path — so the same admitted contract
    produces the same digest whichever run staged it.
    """
    encoded = body.encode("utf-8")
    return f"{_DIGEST_PREFIX}{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class AdmittedLeafContract:
    """The canonical issue contract, as the orchestrator read it.

    ``body`` is the executable leaf contract verbatim. It is required and
    must be non-empty: an issue whose body the orchestrator could not read
    has no admitted mutation contract to review against, and substituting
    the title would be exactly the approximation #399 refuses.
    """

    issue_number: int
    issue_title: str
    body: str

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("admitted leaf contract requires a positive issue_number")
        if not self.issue_title.strip():
            raise ValueError("admitted leaf contract requires a non-empty issue_title")
        if not self.body.strip():
            raise ValueError("admitted leaf contract requires a non-empty body")

    @property
    def digest(self) -> str:
        return leaf_contract_digest(self.body)

    def staged_at(self, contract_path: Path) -> "StagedLeafContract":
        """The handle describing this contract staged at ``contract_path``."""
        return StagedLeafContract(
            path=contract_path,
            issue_number=self.issue_number,
            issue_title=self.issue_title,
            digest=self.digest,
        )

    def manifest_payload(self, *, contract_path: Path) -> dict[str, Any]:
        """The attribution sidecar written beside the staged bytes."""
        return {
            "schema": LEAF_CONTRACT_MANIFEST_SCHEMA,
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "digest": self.digest,
            "contract_file": contract_path.name,
        }


@dataclass(frozen=True, slots=True)
class StagedLeafContract:
    """Where one exchange's contract bytes live, and which bytes they are.

    Carried on the turn packet for both roles. The digest is the whole
    point: a prompt naming a path proves only that a file was mentioned,
    while a persisted packet naming path *and* digest proves which bytes
    that turn was built against.
    """

    path: Path
    issue_number: int
    issue_title: str
    digest: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("staged leaf contract requires an absolute path")
        if self.issue_number <= 0:
            raise ValueError("staged leaf contract requires a positive issue_number")
        if not self.issue_title.strip():
            raise ValueError("staged leaf contract requires a non-empty issue_title")
        if not self.digest.startswith(_DIGEST_PREFIX):
            raise ValueError(
                f"staged leaf contract digest must start with {_DIGEST_PREFIX!r}"
            )

    def matches(self, body: str) -> bool:
        """Whether ``body`` is the exact text this handle attests to."""
        return leaf_contract_digest(body) == self.digest

    def to_manifest_fields(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "digest": self.digest,
        }

    @classmethod
    def from_manifest(cls, manifest: Any) -> "StagedLeafContract | None":
        """Recover a handle from its persisted fields.

        ``None`` means "this artifact is unusable", not "unset" — the
        caller that reads a turn packet rejects the whole packet on it,
        so a replayed prompt dependency can never silently degrade to a
        contract nobody can name.
        """
        if not isinstance(manifest, Mapping):
            return None
        path_raw = manifest.get("path")
        issue_number = manifest.get("issue_number")
        issue_title = manifest.get("issue_title")
        digest = manifest.get("digest")
        if not isinstance(path_raw, str) or not path_raw:
            return None
        if not isinstance(issue_number, int) or isinstance(issue_number, bool):
            return None
        if not isinstance(issue_title, str):
            return None
        if not isinstance(digest, str):
            return None
        try:
            return cls(
                path=Path(path_raw),
                issue_number=issue_number,
                issue_title=issue_title,
                digest=digest,
            )
        except ValueError:
            return None


def staged_leaf_contract_from_manifest_payload(
    payload: Any,
    *,
    contract_path: Path,
) -> StagedLeafContract:
    """Read the attribution sidecar staged beside ``contract_path``.

    Raises :class:`LeafContractUnavailable` for every malformed shape
    rather than returning a partial handle: a sidecar the orchestrator
    cannot read is a contract the exchange cannot prove.
    """
    if not isinstance(payload, Mapping):
        raise LeafContractUnavailable(
            "staged leaf contract manifest is not a JSON object"
        )
    schema = payload.get("schema")
    if schema != LEAF_CONTRACT_MANIFEST_SCHEMA:
        raise LeafContractUnavailable(
            f"staged leaf contract manifest schema {schema!r} is not "
            f"{LEAF_CONTRACT_MANIFEST_SCHEMA!r}"
        )
    issue_number = payload.get("issue_number")
    issue_title = payload.get("issue_title")
    digest = payload.get("digest")
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise LeafContractUnavailable(
            "staged leaf contract manifest requires an int issue_number"
        )
    if not isinstance(issue_title, str):
        raise LeafContractUnavailable(
            "staged leaf contract manifest requires a str issue_title"
        )
    if not isinstance(digest, str):
        raise LeafContractUnavailable(
            "staged leaf contract manifest requires a str digest"
        )
    try:
        return StagedLeafContract(
            path=contract_path,
            issue_number=issue_number,
            issue_title=issue_title,
            digest=digest,
        )
    except ValueError as exc:
        raise LeafContractUnavailable(
            f"staged leaf contract manifest is invalid: {exc}"
        ) from exc
