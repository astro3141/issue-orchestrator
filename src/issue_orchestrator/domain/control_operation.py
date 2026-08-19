"""Identity and reconciled projection for terminal-less control operations (#146).

A control operation is work the ORCHESTRATOR does about one exact candidate
while no agent terminal exists and no queue request was consumed. Every
existing exclusion in this codebase is derived from something that does not
exist for such an operation: ``PendingWorkClaim`` means a dequeued queue
request, ``InFlightWorkLedger`` is keyed on ``session.terminal_id``,
``LaunchWorkClaim`` is typed on ``SessionRunAssets``, the ``in-progress`` label
asserts an issue session exists, and the queue cache excludes from
``session_history`` ∪ ``active_sessions`` — both session-derived. So a
terminal-less operation is invisible to all of them, and needs its own
identity.

Two things this module is deliberately NOT:

* **Not operation truth.** :class:`ControlOperationExclusions` is a
  *projection*: it holds only what
  :class:`~..control.control_operation_ownership.ControlOperationOwnership`
  put there after reconciling durable leases against a caller-supplied set of
  live operations. A durable lease row can never appear here on its own
  authority, which is what stops a crash after settlement from turning a
  durable lease into a durable deadlock.
* **Not authority.** Presence here prevents conflicting execution and nothing
  else. It grants no publication or review authority, sets no label, and
  changes no evaluation history. A reader consulting it may only become more
  conservative.

Identity is exact-candidate: ``(issue, HEAD SHA, kind)``. The candidate half is
the same ``(issue, commit)`` pair :class:`~.attempt.AttemptKey` already names,
so ``A`` and ``A'`` are different operations and neither can inherit the
other's ownership — the property the SHA normalisation in
:mod:`.commit_sha` exists to guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .commit_sha import normalize_commit_sha
from .issue_key_codec import decode_issue_key, encode_issue_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .attempt import AttemptKey
    from .issue_key import IssueKey


class ControlOperationKind(str, Enum):
    """Which control-owned operation an ownership row is about.

    One member, on purpose. The kind exists so an ownership row states what it
    reserves rather than implying it, and so a second control operation over
    the same candidate cannot silently share the first one's reservation. New
    members are added by the leaf that owns the operation, never speculatively.
    """

    PUBLICATION_REVALIDATION_CONTINUATION = "publication_revalidation_continuation"


@dataclass(frozen=True, slots=True)
class ControlOperationKey:
    """The identity of one control operation over one exact candidate.

    ``issue_key`` is canonicalised through the durable issue-key codec on
    construction. ``IssueKey`` is a protocol whose implementations are ordinary
    frozen dataclasses, so two implementations naming the SAME work item
    (``GitHubIssueKey`` from a live issue, ``StoredIssueKey`` rebuilt from an
    attempt sidecar) compare unequal by dataclass identity. Ownership is looked
    up by equality, so a key that did not canonicalise would let the same
    operation be claimed twice — once per spelling.

    ``head_sha`` is normalised by the same rule every authority record binding
    evidence to a candidate uses, so an abbreviated or upper-case SHA is
    refused rather than turned into an identity that compares unequal to a real
    HEAD later.
    """

    issue_key: "IssueKey"
    head_sha: str
    kind: ControlOperationKind

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_key", decode_issue_key(encode_issue_key(self.issue_key))
        )
        object.__setattr__(
            self,
            "head_sha",
            normalize_commit_sha(
                self.head_sha, field_name="ControlOperationKey.head_sha"
            ),
        )

    @classmethod
    def for_candidate(
        cls, candidate: "AttemptKey", kind: ControlOperationKind
    ) -> "ControlOperationKey":
        """The operation identity for the candidate an ``AttemptKey`` names.

        The candidate half of this identity is exactly the ``(issue, commit)``
        pair the attempt record is already keyed by, so a caller holding a
        candidate cannot accidentally reserve a different one. It borrows the
        attempt's IDENTITY only: nothing here reads, writes, or depends on
        attempt evidence, which stays what it is — a record of what was
        evaluated, not a mutable scheduling source of truth.
        """
        return cls(candidate.issue_key, candidate.head_sha, kind)

    @property
    def issue_scope(self) -> str:
        return self.issue_key.scope()

    @property
    def issue_stable_id(self) -> str:
        return str(self.issue_key.stable_id())

    @property
    def durable_parts(self) -> tuple[str, str, str, str]:
        """The four values a durable row is addressed by, in one spelling.

        Used both as the storage address and as the ordering key, so a store
        and a caller cannot disagree about what makes two operations the same.
        """
        return (
            self.issue_scope,
            self.issue_stable_id,
            self.head_sha,
            self.kind.value,
        )

    def __str__(self) -> str:
        return f"{self.issue_key}@{self.head_sha[:7]}:{self.kind.value}"


class ControlOperationOwnershipStatus(str, Enum):
    """What this engine may do about one live control operation.

    ``CONTENDED`` and ``UNAVAILABLE`` are deliberately separate, and neither is
    folded into "free": "another holder has it" and "we could not tell" demand
    different words to an operator, and both must fail closed for scheduling.
    An unreadable ownership store never means the operation is not running.
    """

    OWNED = "owned"
    CONTENDED = "contended"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ControlOperationOwnershipEntry:
    """One live operation's reconciled ownership result."""

    key: ControlOperationKey
    status: ControlOperationOwnershipStatus
    holder: str = ""
    detail: str = ""

    @property
    def owned(self) -> bool:
        return self.status is ControlOperationOwnershipStatus.OWNED


@dataclass(frozen=True, slots=True)
class ControlOperationExclusions:
    """The reconciled ownership projection every scheduler reader consults.

    Only operations the owner has reconciled against caller-supplied live truth
    reach this value, so the scheduler can never be excluded by a raw durable
    row: a lease that survived a crash excludes nothing until reconciliation
    matches it to an operation the caller still declares live.

    Every entry excludes, whatever its status. An operation this engine owns is
    running; one another holder has is running too; one whose ownership could
    not be read might be. Only ``owned`` says this engine may act on it, which
    is a different question from whether ordinary work may start.

    Frozen and replaced wholesale rather than mutated, so a reader on the tick
    thread always sees one complete reconciliation rather than half of two.
    """

    entries: tuple[ControlOperationOwnershipEntry, ...] = ()

    def _keys(
        self, status: ControlOperationOwnershipStatus
    ) -> tuple[ControlOperationKey, ...]:
        return tuple(e.key for e in self.entries if e.status is status)

    @property
    def owned(self) -> tuple[ControlOperationKey, ...]:
        """Operations this engine holds and may act on."""
        return self._keys(ControlOperationOwnershipStatus.OWNED)

    @property
    def contended(self) -> tuple[ControlOperationKey, ...]:
        """Operations another holder reserved first."""
        return self._keys(ControlOperationOwnershipStatus.CONTENDED)

    @property
    def unavailable(self) -> tuple[ControlOperationKey, ...]:
        """Operations whose ownership could not be established or verified."""
        return self._keys(ControlOperationOwnershipStatus.UNAVAILABLE)

    def entry_for(
        self, key: ControlOperationKey
    ) -> ControlOperationOwnershipEntry | None:
        return next((e for e in self.entries if e.key == key), None)

    def owns(self, key: ControlOperationKey) -> bool:
        entry = self.entry_for(key)
        return entry is not None and entry.owned

    def excludes_issue(self, issue_key: "IssueKey") -> bool:
        """Whether a live control operation holds this issue's ordinary work.

        Asked per ISSUE rather than per candidate: ordinary Actor/rework work
        moves the branch, which is precisely what an operation bound to an
        exact candidate cannot survive. Which candidate the operation is about
        is the owner's business, not the scheduler's.
        """
        if not self.entries:
            # The ordinary case, asked once per issue per refresh: no operation
            # is live, so there is nothing to canonicalise the question against.
            return False
        canonical = decode_issue_key(encode_issue_key(issue_key))
        return any(entry.key.issue_key == canonical for entry in self.entries)


__all__ = [
    "ControlOperationExclusions",
    "ControlOperationKey",
    "ControlOperationKind",
    "ControlOperationOwnershipEntry",
    "ControlOperationOwnershipStatus",
]
