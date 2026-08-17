"""Attempt-scoped state for one issue at one commit.

An ``Attempt`` is the cache boundary named in issue #6130: all facts here are
about a specific issue at a specific HEAD SHA. Per-run artifacts remain on the
session run manifest; cross-run cache facts belong here.

Because its key is exactly ``(issue, commit)``, it is also where Foundation
admission evidence about one candidate lives (#34). ``validation_record_path``
already points at §4's validation half; ``execution_identities`` carries the
actor/reviewer half. Both are about the same ``(issue, A)``, and an attempt
refuses to hold identities naming a different commit than the key it is filed
under — so the exact-``A`` binding is the storage key itself, not a field that
could disagree with it.

``publication_verdict`` (#85) is the third such fact, and the one that closes a
gap the other two left open: ``validation_record_path`` *points at* the
validation half rather than stating it, and it points into the session
directory inside the coder worktree — so once that worktree is reaped the
attempt still says a gate ran, without saying what it decided. The receipt
states the verdict itself, and is bound to the key by the same rule the
identities are.
"""

from __future__ import annotations

from dataclasses import dataclass

from .commit_sha import normalize_commit_sha
from .execution_identity import CandidateExecutionIdentities
from .issue_key import GitHubIssueKey, IssueKey, StableIssueId
from .validation_verdict_receipt import ValidationVerdictReceipt

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredIssueKey:
    """IssueKey implementation reconstructed from an attempt sidecar."""

    stable: str
    key_scope: str

    def stable_id(self) -> StableIssueId:
        return StableIssueId(self.stable)

    def scope(self) -> str:
        return self.key_scope

    def __str__(self) -> str:
        return f"{self.key_scope}:{self.stable}"


@dataclass(frozen=True)
class AttemptKey:
    """Stable identity for an issue attempt at a specific commit."""

    issue_key: IssueKey
    head_sha: str

    def __post_init__(self) -> None:
        # The same rule the authority records use: this key *is* the binding
        # between one candidate and the evidence filed under it, so it must
        # decide "same commit" the way the records it holds do.
        object.__setattr__(
            self,
            "head_sha",
            normalize_commit_sha(self.head_sha, field_name="AttemptKey.head_sha"),
        )

    @property
    def issue_stable_id(self) -> str:
        return str(self.issue_key.stable_id())

    @property
    def issue_scope(self) -> str:
        return self.issue_key.scope()


@dataclass(frozen=True)
class Attempt:
    """Authoritative per-attempt state for an issue at a specific commit."""

    key: AttemptKey
    reroute_budget_used: int = 0
    validation_record_path: str | None = None
    review_exchange_summary_path: str | None = None
    review_exchange_job_id: str | None = None
    execution_identities: CandidateExecutionIdentities | None = None
    # The publication gate's own verdict for this candidate (#85). One slot,
    # holding the *publish* contract's receipt: the quick gate runs again after
    # every completion, so a shared slot would let a later quick verdict erase
    # the publication one. ``None`` means no publication gate has reported on
    # this candidate — never-run, which is not a failure and not a pass.
    publication_verdict: ValidationVerdictReceipt | None = None

    def __post_init__(self) -> None:
        if self.reroute_budget_used < 0:
            raise ValueError("Attempt.reroute_budget_used must be >= 0")
        if (
            self.execution_identities is not None
            and not self.execution_identities.covers(self.key.head_sha)
        ):
            raise ValueError(
                "Attempt.execution_identities must name the attempt's own "
                f"commit: key={self.key.head_sha} "
                f"identities={self.execution_identities.candidate_sha}"
            )
        if (
            self.publication_verdict is not None
            and not self.publication_verdict.covers(self.key.head_sha)
        ):
            # Same rule as the identities above, for the same reason: the key
            # *is* the binding to one candidate, so a receipt naming another
            # commit is not evidence filed under the wrong name — it is
            # evidence about other work, and must not be readable here at all.
            raise ValueError(
                "Attempt.publication_verdict must name the attempt's own "
                f"commit: key={self.key.head_sha} "
                f"receipt={self.publication_verdict.head_sha}"
            )

    @property
    def publication_validation_passed(self) -> bool:
        """Whether this candidate passed the publication contract.

        The question the durable receipt exists to answer, asked of the
        attempt's own commit so no caller has to re-supply it. ``False`` covers
        every way the answer is not yes: no receipt (never gated), a failure, a
        timeout, and a receipt produced by some other contract.
        """
        return self.publication_verdict is not None and (
            self.publication_verdict.certifies_publication(self.key.head_sha)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "issue_key_type": _issue_key_type(self.key.issue_key),
            "issue_key": self.key.issue_stable_id,
            "issue_scope": self.key.issue_scope,
            "head_sha": self.key.head_sha,
            "reroute_budget_used": self.reroute_budget_used,
            "validation_record_path": self.validation_record_path,
            "review_exchange_summary_path": self.review_exchange_summary_path,
            "review_exchange_job_id": self.review_exchange_job_id,
            "execution_identities": (
                self.execution_identities.to_payload()
                if self.execution_identities is not None
                else None
            ),
            "publication_verdict": (
                self.publication_verdict.to_payload()
                if self.publication_verdict is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Attempt":
        _validate_schema_version(data.get("schema_version"))
        issue_key_type_raw = data.get("issue_key_type")
        issue_key_raw = data.get("issue_key")
        issue_scope_raw = data.get("issue_scope")
        head_sha = data.get("head_sha")
        if not isinstance(issue_key_type_raw, str) or not issue_key_type_raw.strip():
            raise ValueError("Attempt sidecar missing issue_key_type")
        if not isinstance(issue_key_raw, str) or not issue_key_raw.strip():
            raise ValueError("Attempt sidecar missing issue_key")
        if not isinstance(issue_scope_raw, str) or not issue_scope_raw.strip():
            raise ValueError("Attempt sidecar missing issue_scope")
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise ValueError("Attempt sidecar missing head_sha")
        return cls(
            key=AttemptKey(
                _issue_key_from_dict(
                    issue_key_type=issue_key_type_raw,
                    stable_id=issue_key_raw,
                    scope=issue_scope_raw,
                ),
                head_sha,
            ),
            reroute_budget_used=_int_field(
                data.get("reroute_budget_used"), "reroute_budget_used"
            ),
            validation_record_path=_optional_str(data.get("validation_record_path")),
            review_exchange_summary_path=_optional_str(
                data.get("review_exchange_summary_path")
            ),
            review_exchange_job_id=_optional_str(data.get("review_exchange_job_id")),
            execution_identities=_optional_execution_identities(
                data.get("execution_identities")
            ),
            publication_verdict=_optional_publication_verdict(
                data.get("publication_verdict")
            ),
        )


def _issue_key_type(issue_key: IssueKey) -> str:
    if isinstance(issue_key, GitHubIssueKey):
        return "github"
    if isinstance(issue_key, StoredIssueKey):
        return "stored"
    raise ValueError(
        f"Attempt cannot persist unsupported IssueKey type: {type(issue_key).__name__}"
    )


def _issue_key_from_dict(
    *,
    issue_key_type: str,
    stable_id: str,
    scope: str,
) -> IssueKey:
    match issue_key_type.strip().lower():
        case "github":
            return GitHubIssueKey(repo=scope, external_id=stable_id)
        case "stored":
            return StoredIssueKey(stable_id, scope)
        case other:
            raise ValueError(f"unknown Attempt issue_key_type: {other}")


def _validate_schema_version(value: object) -> None:
    if isinstance(value, bool) or value != _SCHEMA_VERSION:
        raise ValueError(f"Attempt sidecar schema_version must be {_SCHEMA_VERSION}")


def _optional_execution_identities(
    value: object,
) -> CandidateExecutionIdentities | None:
    """Parse the admission-evidence half, or ``None`` when none was recorded.

    A malformed record raises rather than reading as absent. Absent means "no
    exchange has bound identities to this commit yet"; unparseable means the
    durable evidence is damaged, and a gate must not mistake the second for the
    first — that is the difference between "not yet reviewed" and "the record
    of who reviewed it is corrupt".
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Attempt sidecar execution_identities must be an object")
    return CandidateExecutionIdentities.from_payload(value)


def _optional_publication_verdict(value: object) -> ValidationVerdictReceipt | None:
    """Parse the publication verdict, or ``None`` when none was recorded.

    A malformed receipt raises rather than reading as absent, for the reason
    :func:`_optional_execution_identities` gives: absent means "no publication
    gate has reported on this candidate", unparseable means the durable
    evidence is damaged, and a gate must not mistake the second for the first.
    Reading damage as "never gated" is the safe direction only by accident —
    it is a claim about the world made from a broken instrument.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Attempt sidecar publication_verdict must be an object")
    return ValidationVerdictReceipt.from_payload(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected string or null, got {type(value).__name__}")
    stripped = value.strip()
    return stripped or None


def _int_field(value: object, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value
