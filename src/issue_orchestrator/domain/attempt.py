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

``completed_evaluations`` (#85, #139) is the third such fact, and the one that
closes a gap the other two left open: ``validation_record_path`` *points at*
the validation half rather than stating it, and it points into the session
directory inside the coder worktree — so once that worktree is reaped the
attempt still says a gate ran, without saying what it decided. A receipt
states the verdict itself, and is bound to the key by the same rule the
identities are.

It is a *history* rather than a slot (#139). One slot meant every later gate
run overwrote the earlier one, so the only exits from a candidate that failed
for a reason unrelated to the candidate were moving the SHA — which destroys
the evaluated artifact — or editing durable state by hand. An ordered,
append-only history lets the same candidate be evaluated again without any
prior evaluation being rewritten or dropped: order is list position, so no
field inside :class:`~.validation_verdict_receipt.ValidationVerdictReceipt`
had to change to carry it.

``revalidation_budget_used`` is the durable bound on that (#139), in the shape
``reroute_budget_used`` already had. It is a *start* budget: the revalidation
route consumes it before any external gate work begins, so an interrupted
revalidation fails closed rather than refunding itself on restart.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .commit_sha import normalize_commit_sha
from .execution_identity import CandidateExecutionIdentities
from .issue_key import GitHubIssueKey, IssueKey, StableIssueId
from .validation_verdict_receipt import ValidationVerdictReceipt

_SCHEMA_VERSION = 2
_V1_SCHEMA_VERSION = 1

REVALIDATION_ALLOWANCE = 1
"""How many same-SHA revalidations one candidate may ever start (#139).

Exactly one, and stated here rather than at the route that spends it: the
counter is durable, so the ceiling it is compared against has to be a property
of the record, not of whichever caller happens to read it.

The policy asks for the allowance per ``(candidate, validation contract)``,
and this counter is per *candidate*. The two are the same thing today because
publication is the only revalidatable contract — a quick-gate result is not
authority, and a candidate whose contract has drifted is refused by admission
rather than re-run. They stop being the same thing the moment a second
contract becomes revalidatable, and at that point this must become a
per-contract counter rather than one allowance both contracts draw from; see
:attr:`Attempt.revalidation_budget_used`.
"""


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
    # Every gate run that reached a verdict on this candidate, oldest first
    # (#85, #139). Ordered by list position and never rewritten: a later
    # evaluation is appended beside the earlier ones, so the FAIL that caused a
    # revalidation is still readable after the PASS that followed it.
    #
    # Each entry names the contract it executed, so a reader asks for the
    # contract it cares about rather than trusting whichever entry is last: the
    # quick gate runs again after every completion, and a shared *slot* is
    # exactly how a later quick verdict used to erase the publication one.
    #
    # Unbounded by design, and bounded in practice by what may append: only a
    # gate run that *reached* a verdict does, and reuse of an earlier
    # evaluation appends nothing. The ceiling is therefore the number of times
    # a contract actually executes against one commit — one publication
    # evaluation plus its one revalidation, and one quick evaluation per
    # session that re-runs the quick gate at that commit. If a future contract
    # can execute unboundedly at a fixed SHA, this is where a cap belongs; a
    # cap today would have to drop evidence, which is the one thing #139
    # exists to stop.
    completed_evaluations: tuple[ValidationVerdictReceipt, ...] = ()
    # How much of the one-revalidation allowance this candidate has spent
    # (#139). Durable in the primary-checkout sidecar, so it survives restart by
    # construction — the in-memory reroute counters cannot, which is why the
    # policy forbids reusing them here. One counter for the candidate, not one
    # per contract: see REVALIDATION_ALLOWANCE for why those coincide today and
    # what has to change when they stop.
    revalidation_budget_used: int = 0

    def __post_init__(self) -> None:
        if self.reroute_budget_used < 0:
            raise ValueError("Attempt.reroute_budget_used must be >= 0")
        if self.revalidation_budget_used < 0:
            raise ValueError("Attempt.revalidation_budget_used must be >= 0")
        object.__setattr__(
            self, "completed_evaluations", tuple(self.completed_evaluations)
        )
        if (
            self.execution_identities is not None
            and not self.execution_identities.covers(self.key.head_sha)
        ):
            raise ValueError(
                "Attempt.execution_identities must name the attempt's own "
                f"commit: key={self.key.head_sha} "
                f"identities={self.execution_identities.candidate_sha}"
            )
        for receipt in self.completed_evaluations:
            if receipt.covers(self.key.head_sha):
                continue
            # Same rule as the identities above, for the same reason: the key
            # *is* the binding to one candidate, so a receipt naming another
            # commit is not evidence filed under the wrong name — it is
            # evidence about other work, and must not be readable here at all.
            # Held for *every* entry, not only the newest, so appending cannot
            # smuggle another candidate's verdict in behind one that binds.
            raise ValueError(
                "Attempt.completed_evaluations must name the attempt's own "
                f"commit: key={self.key.head_sha} receipt={receipt.head_sha}"
            )

    @property
    def publication_evaluations(self) -> tuple[ValidationVerdictReceipt, ...]:
        """Every evaluation the *publication* contract produced, oldest first.

        The history holds whatever contract actually ran, so a reader that
        wants publication has to select rather than count or index: the quick
        gate runs again after every completion and appends beside the
        publication entries. Selecting here, once, is what lets callers ask
        "did publication decide anything new" without either re-deriving the
        predicate or reaching into
        :attr:`completed_evaluations` and mistaking someone else's receipt for
        their own.
        """
        return tuple(
            receipt
            for receipt in self.completed_evaluations
            if receipt.from_publication_contract
        )

    @property
    def latest_publication_evaluation(self) -> ValidationVerdictReceipt | None:
        """The most recent evaluation produced by the *publication* contract.

        The last *publication* entry rather than the last entry: an
        ``agent_gate`` or ``quick_gate`` receipt appended after a publication
        one says nothing about publication. ``None`` means no publication gate
        has reported on this candidate — never-run, which is not a failure and
        not a pass.
        """
        evaluations = self.publication_evaluations
        return evaluations[-1] if evaluations else None

    @property
    def publication_validation_passed(self) -> bool:
        """Whether this candidate passed the publication contract.

        The question the durable receipts exist to answer, asked of the
        attempt's own commit so no caller has to re-supply it, and of the
        *latest* publication evaluation so a revalidation's result supersedes
        the evaluation it re-ran. ``False`` covers every way the answer is not
        yes: no receipt (never gated), a failure, a timeout, and a receipt
        produced by some other contract.
        """
        latest = self.latest_publication_evaluation
        return latest is not None and latest.certifies_publication(self.key.head_sha)

    @property
    def revalidation_allowance_available(self) -> bool:
        """Whether a same-SHA revalidation may still be *started* (#139)."""
        return self.revalidation_budget_used < REVALIDATION_ALLOWANCE

    def with_completed_evaluation(
        self, receipt: ValidationVerdictReceipt
    ) -> "Attempt":
        """This attempt with ``receipt`` appended to its evaluation history.

        The only way an evaluation enters the record, so "append, never
        overwrite" is a property of the type rather than a convention each
        writer re-implements. The binding rule in :meth:`__post_init__` runs on
        the result, so a receipt naming another commit is refused here exactly
        as it is on construction.
        """
        return replace(
            self,
            completed_evaluations=(*self.completed_evaluations, receipt),
        )

    def with_revalidation_reserved(self) -> "Attempt":
        """This attempt with one revalidation allowance durably spent (#139).

        Spending is unconditional here and bounded by the caller's admission
        check, but the ceiling is re-asserted so a second reservation cannot be
        written even if a caller asked for one.
        """
        if not self.revalidation_allowance_available:
            raise ValueError(
                "Attempt revalidation allowance is already spent: "
                f"{self.revalidation_budget_used}/{REVALIDATION_ALLOWANCE}"
            )
        return replace(
            self, revalidation_budget_used=self.revalidation_budget_used + 1
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
            "completed_evaluations": [
                receipt.to_payload() for receipt in self.completed_evaluations
            ],
            "revalidation_budget_used": self.revalidation_budget_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Attempt":
        schema_version = _validate_schema_version(data.get("schema_version"))
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
            completed_evaluations=_completed_evaluations(data, schema_version),
            revalidation_budget_used=_int_field(
                data.get("revalidation_budget_used"), "revalidation_budget_used"
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


def _validate_schema_version(value: object) -> int:
    """Return the sidecar version this payload may be read as.

    Two versions are readable, not one: v1 filed a single ``publication_verdict``
    slot and v2 files the ordered history that replaced it (#139). A v1 sidecar
    is real durable evidence about a real candidate, so refusing it would erase
    a gate result the orchestrator itself wrote. Anything else still fails
    closed — a version this code does not know is a record written by a schema
    it cannot claim to understand.
    """
    if isinstance(value, bool) or value not in (_V1_SCHEMA_VERSION, _SCHEMA_VERSION):
        raise ValueError(
            "Attempt sidecar schema_version must be "
            f"{_V1_SCHEMA_VERSION} or {_SCHEMA_VERSION}"
        )
    return int(value)  # type: ignore[arg-type]


def _completed_evaluations(
    data: dict[str, object], schema_version: int
) -> tuple[ValidationVerdictReceipt, ...]:
    """The evaluation history this payload states, whichever schema wrote it.

    A v1 record's single verdict migrates to a one-element history: it *is* one
    completed evaluation, and it was the only one that could be recorded. The
    migration is a read-time projection rather than a rewrite, so a sidecar the
    orchestrator never writes to again keeps reading correctly.
    """
    if schema_version == _V1_SCHEMA_VERSION:
        migrated = _optional_publication_verdict(data.get("publication_verdict"))
        return () if migrated is None else (migrated,)
    raw = data.get("completed_evaluations")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("Attempt sidecar completed_evaluations must be a list")
    return tuple(
        _publication_verdict(entry, field_name="completed_evaluations entry")
        for entry in raw
    )


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
    """Parse a v1 sidecar's single publication verdict, or ``None`` if absent.

    A malformed receipt raises rather than reading as absent, for the reason
    :func:`_optional_execution_identities` gives: absent means "no publication
    gate has reported on this candidate", unparseable means the durable
    evidence is damaged, and a gate must not mistake the second for the first.
    Reading damage as "never gated" is the safe direction only by accident —
    it is a claim about the world made from a broken instrument.
    """
    if value is None:
        return None
    return _publication_verdict(value, field_name="publication_verdict")


def _publication_verdict(
    value: object, *, field_name: str
) -> ValidationVerdictReceipt:
    if not isinstance(value, dict):
        raise ValueError(f"Attempt sidecar {field_name} must be an object")
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
