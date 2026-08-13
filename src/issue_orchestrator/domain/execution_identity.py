"""Who executed a candidate, as the orchestrator observed it.

Foundation admission (``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4)
admits work only when I2c holds: the review outcome is an approval **and the
reviewer identity is distinct from the actor's**. :mod:`.review_verdict_binding`
supplies the approval half. This module supplies the other one — the two
execution identities, paired with the exact candidate they executed against.

Three properties make these records evidence rather than description.

* **Orchestrator-observed.** Every field is something the orchestrator did or
  configured: the role's agent label and its resolved provider/model come from
  the launcher's own configuration, and ``candidate_sha`` is the commit the
  orchestrator checked out for the review. No agent-authored text reaches any
  field. This is the rule #15 applied to ``reviewed_sha``, applied again: the
  orchestrator records what it ran, not what an agent said it was.
* **Bound to the exact candidate, not to the issue.** An issue-keyed role name
  ("this issue is reviewed by ``agent:reviewer``") survives the candidate
  moving, so it cannot answer I2c about a specific ``A``. ``candidate_sha`` is
  a full SHA and :meth:`CandidateExecutionIdentities.covers` re-derives the
  match whenever it matters, so a moved candidate is detectably stale rather
  than quietly reusable.
* **Distinctness is falsifiable.** :meth:`AgentExecutionIdentity.fingerprint`
  deliberately excludes ``role``. Including it would make every pair distinct
  by construction — a check that no mutation can break, which is a check that
  has pinned nothing (#21 §9). What remains is the executing configuration:
  make the actor's equal the reviewer's and
  :meth:`CandidateExecutionIdentities.roles_are_distinct` goes false.

The record is evidence only. Nothing here admits, holds, approves or publishes
anything; #33 owns the gate that reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .commit_sha import normalize_commit_sha

EXECUTION_IDENTITY_SCHEMA_VERSION = 1


class ExecutionRole(StrEnum):
    """The two roles §4 requires to be distinct.

    Named for the contract's vocabulary rather than IO's: the contract says
    "actor" and "reviewer", and IO's ``coder``/``reviewer`` exchange roles are
    what fills them. Keeping the contract's spelling here is what lets a later
    reader check the record against §4 without a translation step.
    """

    ACTOR = "actor"
    REVIEWER = "reviewer"


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty str")
    return stripped


def _unpinned_or_text(value: object, *, field_name: str) -> str | None:
    """A field the orchestrator may legitimately not have pinned.

    ``model`` is the one such field: an agent configured with an explicit
    non-Claude provider and no ``model:`` runs on whatever its CLI defaults to,
    and the orchestrator observes exactly that — it passed no model. Recording
    the absence is the truthful record; refusing to record it would make a
    supported configuration fatal at the seam that only *describes* the run.

    Blank and ``None`` are one fact and canonicalise to ``None``, so two
    spellings of "no model pinned" cannot fingerprint as different executions.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str or None")
    return value.strip() or None


@dataclass(frozen=True, slots=True)
class AgentExecutionIdentity:
    """One role's executing configuration, as the orchestrator launched it.

    ``model`` is ``None`` when the orchestrator pinned no model and left the
    choice to the provider's CLI — see :func:`_unpinned_or_text`. It is the
    launcher's own :meth:`~..domain.models.AgentConfig.resolved_model`, so the
    record and the spawn cannot name different models.
    """

    role: ExecutionRole
    agent_label: str
    provider: str
    model: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", ExecutionRole(self.role))
        for field_name in ("agent_label", "provider"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self, "model", _unpinned_or_text(self.model, field_name="model")
        )

    def fingerprint(self) -> tuple[str, str, str | None]:
        """What makes two executions the same one, ignoring the role played.

        Role is excluded on purpose — see the module docstring. Two roles run
        by the same agent label on the same provider and model are one
        execution identity wearing two hats, which is exactly the arrangement
        I2c exists to refuse.

        An unpinned model keeps the check's teeth: two roles that both let
        their CLI choose still differ by ``agent_label`` and ``provider``.
        """
        return (self.agent_label, self.provider, self.model)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object], *, expected_role: ExecutionRole
    ) -> "AgentExecutionIdentity":
        """Parse one stored identity, rejecting a role it was not filed under."""
        raw_role = payload.get("role")
        if not isinstance(raw_role, str) or not raw_role:
            raise ValueError("execution identity requires a role")
        try:
            role = ExecutionRole(raw_role)
        except ValueError as exc:
            raise ValueError(f"unknown execution identity role: {raw_role!r}") from exc
        if role is not expected_role:
            raise ValueError(
                f"execution identity filed as {expected_role.value} carries "
                f"role {role.value}"
            )
        if "model" not in payload:
            # Present-and-null ("no model pinned") is a statement; absent is a
            # record that never made one. A gate comparing executions must not
            # read the second as the first.
            raise ValueError("execution identity requires a model")
        return cls(
            role=role,
            agent_label=_required_text(payload.get("agent_label"), field_name="agent_label"),
            provider=_required_text(payload.get("provider"), field_name="provider"),
            model=_unpinned_or_text(payload["model"], field_name="model"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "agent_label": self.agent_label,
            "provider": self.provider,
            "model": self.model,
        }


@dataclass(frozen=True, slots=True)
class CandidateExecutionIdentities:
    """Both execution identities for one candidate commit, as one record.

    The pairing is structural: a payload naming one role without the other, or
    either without ``candidate_sha``, does not parse. There is no state in
    which half the evidence exists, so a gate can never read "the actor ran X"
    and silently assume anything about who reviewed it.
    """

    candidate_sha: str
    actor: AgentExecutionIdentity
    reviewer: AgentExecutionIdentity
    observed_at: str
    schema_version: int = EXECUTION_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_sha",
            normalize_commit_sha(self.candidate_sha, field_name="candidate_sha"),
        )
        if self.actor.role is not ExecutionRole.ACTOR:
            raise ValueError("actor identity must carry the actor role")
        if self.reviewer.role is not ExecutionRole.REVIEWER:
            raise ValueError("reviewer identity must carry the reviewer role")
        object.__setattr__(
            self,
            "observed_at",
            _required_text(self.observed_at, field_name="observed_at"),
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != EXECUTION_IDENTITY_SCHEMA_VERSION
        ):
            # Fails closed, as the verdict binding does. A version this code
            # does not know is a record written by a schema it cannot claim to
            # understand; reading it as v1 would let a gate act on fields it
            # may be misreading.
            raise ValueError(
                "execution identity schema_version must be "
                f"{EXECUTION_IDENTITY_SCHEMA_VERSION}, got {self.schema_version!r}"
            )

    def covers(self, head_sha: str) -> bool:
        """Whether this evidence is about ``head_sha`` itself."""
        return (
            normalize_commit_sha(head_sha, field_name="head_sha")
            == self.candidate_sha
        )

    def roles_are_distinct(self) -> bool:
        """Whether the reviewer's execution differs from the actor's."""
        return self.actor.fingerprint() != self.reviewer.fingerprint()

    def satisfies_reviewer_distinctness(self, head_sha: str) -> bool:
        """The only question I2c asks of this record, for ``head_sha`` itself.

        Both halves, never one: evidence about another commit answers ``False``
        even when its two identities differ, because it is evidence about other
        work. Mirrors :meth:`~.review_verdict_binding.BoundReviewVerdict.approves`.
        """
        return self.covers(head_sha) and self.roles_are_distinct()

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "CandidateExecutionIdentities":
        """Parse a stored record, rejecting any payload missing a half."""
        actor_raw = payload.get("actor")
        reviewer_raw = payload.get("reviewer")
        if not isinstance(actor_raw, Mapping):
            raise ValueError("candidate execution identities require an actor")
        if not isinstance(reviewer_raw, Mapping):
            raise ValueError("candidate execution identities require a reviewer")
        if "candidate_sha" not in payload:
            raise ValueError("candidate execution identities require candidate_sha")
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError(
                "candidate execution identities require int schema_version"
            )
        return cls(
            candidate_sha=normalize_commit_sha(
                payload["candidate_sha"], field_name="candidate_sha"
            ),
            actor=AgentExecutionIdentity.from_payload(
                actor_raw, expected_role=ExecutionRole.ACTOR
            ),
            reviewer=AgentExecutionIdentity.from_payload(
                reviewer_raw, expected_role=ExecutionRole.REVIEWER
            ),
            observed_at=_required_text(
                payload.get("observed_at"), field_name="observed_at"
            ),
            schema_version=schema_version,
        )

    def to_payload(self) -> dict[str, Any]:
        """Render the on-disk form. Both halves are always present."""
        return {
            "schema_version": self.schema_version,
            "candidate_sha": self.candidate_sha,
            "actor": self.actor.to_payload(),
            "reviewer": self.reviewer.to_payload(),
            "observed_at": self.observed_at,
        }
