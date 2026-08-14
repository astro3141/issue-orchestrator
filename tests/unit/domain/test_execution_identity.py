"""The actor/reviewer execution identities §4's I2c is stated over.

These tests pin the record's meaning, not its plumbing: what makes two
executions "the same principal", what a stored payload must contain to parse at
all, and — the one that matters most — that the distinctness check is
falsifiable in *both* directions. Rev 4 of the contract separates the principal
(what I2c compares) from the provenance of the run that carried it (retained
for audit, never compared), and §11 states the three mutations that must hold
that separation in place.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.execution_identity import (
    EXECUTION_IDENTITY_SCHEMA_VERSION,
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
    ExecutionPrincipal,
    ExecutionProvenance,
    ExecutionRole,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _identity(
    role: ExecutionRole,
    *,
    agent_label: str,
    provider: str,
    model: str | None,
) -> AgentExecutionIdentity:
    return AgentExecutionIdentity(
        role=role,
        principal=ExecutionPrincipal(agent_label=agent_label),
        provenance=ExecutionProvenance(provider=provider, model=model),
    )


def _actor(
    *,
    agent_label: str = "agent:backend",
    provider: str = "claude-code",
    model: str | None = "opus",
) -> AgentExecutionIdentity:
    return _identity(
        ExecutionRole.ACTOR,
        agent_label=agent_label,
        provider=provider,
        model=model,
    )


def _reviewer(
    *,
    agent_label: str = "agent:reviewer",
    provider: str = "codex",
    model: str | None = "gpt-5",
) -> AgentExecutionIdentity:
    return _identity(
        ExecutionRole.REVIEWER,
        agent_label=agent_label,
        provider=provider,
        model=model,
    )


def _provenance_payload(
    payload: dict[str, object], role: str
) -> dict[str, object]:
    """The stored provenance sub-object, without asserting its way there."""
    identity = payload[role]
    assert isinstance(identity, dict)
    provenance = identity["provenance"]
    assert isinstance(provenance, dict)
    return provenance


def _identities(
    *,
    candidate_sha: str = SHA_A,
    actor: AgentExecutionIdentity | None = None,
    reviewer: AgentExecutionIdentity | None = None,
) -> CandidateExecutionIdentities:
    return CandidateExecutionIdentities(
        candidate_sha=candidate_sha,
        actor=actor or _actor(),
        reviewer=reviewer or _reviewer(),
        observed_at="2026-08-14T00:00:00+00:00",
    )


class TestDistinctness:
    """I2c: the reviewer *principal* must be distinct from the actor's.

    The three §11 rows below are the whole definition of "same identity". Each
    is a mutation that must fail, and between them they pin the boundary from
    both sides: fold provenance into the comparison and the second row dies;
    drop the principal from it and the first or third does.
    """

    def test_distinct_principals_satisfy_i2c_for_their_own_candidate(self) -> None:
        assert _identities().satisfies_reviewer_distinctness(SHA_A) is True

    def test_one_principal_in_both_roles_is_refused(self) -> None:
        """§11 row 1: actor principal == reviewer principal -> refused.

        A check that survives "set the actor's principal to the reviewer's" has
        pinned nothing. Role is deliberately excluded from the comparison, so
        the two records below differ *only* in the hat each wears — and that is
        exactly the arrangement I2c exists to refuse.
        """
        impostor = _identities(
            actor=_actor(
                agent_label="agent:reviewer", provider="codex", model="gpt-5"
            ),
        )

        assert impostor.principals_are_distinct() is False
        assert impostor.satisfies_reviewer_distinctness(SHA_A) is False

    @pytest.mark.parametrize(
        ("actor_provider", "actor_model"),
        [
            ("claude-code", "gpt-5"),
            ("codex", "opus"),
            ("claude-code", "opus"),
            ("codex", None),
        ],
        ids=["provider", "model", "both", "unpinned-model"],
    )
    def test_one_principal_stays_one_however_far_its_provenance_differs(
        self, actor_provider: str, actor_model: str | None
    ) -> None:
        """§11 row 2: same principal, differing provenance -> still refused.

        Provenance does not create a second principal. This is the row that
        pins *which* field is identity: fold ``provider`` or ``model`` back
        into the comparison and every case here starts passing I2c, admitting
        work whose actor reviewed itself under a changed configuration.
        """
        collapsed = _identities(
            actor=_actor(
                agent_label="agent:reviewer",
                provider=actor_provider,
                model=actor_model,
            ),
        )

        assert collapsed.principals_are_distinct() is False
        assert collapsed.satisfies_reviewer_distinctness(SHA_A) is False

    @pytest.mark.parametrize(
        "shared_model", ["opus", None], ids=["pinned", "unpinned"]
    )
    def test_two_principals_stay_distinct_on_identical_configuration(
        self, shared_model: str | None
    ) -> None:
        """§11 row 3: distinct principals, same provider/model -> admitted.

        This is how this fork is actually operated — both roles on the same
        provider and model — so a comparison that collapsed them would make
        independent review unrepresentable. Drop the principal from the
        comparison and this row dies.
        """
        matching_configuration = _identities(
            actor=_actor(provider="claude-code", model=shared_model),
            reviewer=_reviewer(provider="claude-code", model=shared_model),
        )

        assert matching_configuration.principals_are_distinct() is True
        assert matching_configuration.satisfies_reviewer_distinctness(SHA_A) is True

    def test_a_principal_stays_hashable_for_the_evidence_digest(self) -> None:
        """Rev 4 §2 puts both principals inside the set §5 digests.

        Nothing here computes a digest — #33 owns that. What this pins is that
        the principal is not structurally unhashable, so the digest obligation
        stays open rather than being foreclosed by this record's shape.
        """
        principal = ExecutionPrincipal(agent_label="agent:reviewer")

        assert hash(principal) == hash(ExecutionPrincipal(agent_label="agent:reviewer"))
        assert len({principal, _actor().principal}) == 2

    def test_evidence_about_another_commit_never_satisfies_i2c(self) -> None:
        """Distinct principals bound to a different candidate answer False.

        Both halves are required. Evidence about other work is not evidence
        about this candidate no matter how distinct its two roles were.
        """
        identities = _identities(candidate_sha=SHA_A)

        assert identities.principals_are_distinct() is True
        assert identities.covers(SHA_B) is False
        assert identities.satisfies_reviewer_distinctness(SHA_B) is False


class TestRecordInvariants:
    def test_candidate_sha_must_be_a_full_canonical_sha(self) -> None:
        with pytest.raises(ValueError, match="candidate_sha"):
            _identities(candidate_sha=SHA_A[:12])

    def test_uppercase_candidate_sha_is_normalised_not_rejected(self) -> None:
        assert _identities(candidate_sha=SHA_A.upper()).candidate_sha == SHA_A

    def test_roles_cannot_be_filed_under_the_wrong_slot(self) -> None:
        with pytest.raises(ValueError, match="actor identity must carry"):
            CandidateExecutionIdentities(
                candidate_sha=SHA_A,
                actor=_reviewer(),  # type: ignore[arg-type]
                reviewer=_reviewer(),
                observed_at="2026-08-14T00:00:00+00:00",
            )

    def test_a_principal_may_not_be_blank(self) -> None:
        with pytest.raises(ValueError, match="agent_label"):
            ExecutionPrincipal(agent_label="   ")

    def test_a_provenance_provider_may_not_be_blank(self) -> None:
        with pytest.raises(ValueError, match="provider"):
            ExecutionProvenance(provider="   ", model="opus")

    @pytest.mark.parametrize("unpinned", [None, "", "   "], ids=["none", "empty", "blank"])
    def test_an_agent_that_pinned_no_model_records_the_absence(
        self, unpinned: str | None
    ) -> None:
        """A supported configuration must be recordable, not fatal.

        An agent with an explicit non-Claude provider and no ``model:`` runs on
        whatever its CLI defaults to; the config loader spells that as a blank
        model and the launcher passes no model at all. The record states what
        the orchestrator did — it pinned none — instead of refusing to describe
        a run that really happens.
        """
        assert _actor(model=unpinned).provenance.model is None

    def test_observed_at_is_required(self) -> None:
        with pytest.raises(ValueError, match="observed_at"):
            CandidateExecutionIdentities(
                candidate_sha=SHA_A,
                actor=_actor(),
                reviewer=_reviewer(),
                observed_at="  ",
            )


class TestPayloadRoundTrip:
    def test_round_trip_preserves_every_field(self) -> None:
        original = _identities()

        reloaded = CandidateExecutionIdentities.from_payload(original.to_payload())

        assert reloaded == original
        assert reloaded.schema_version == EXECUTION_IDENTITY_SCHEMA_VERSION

    def test_a_mutated_stored_payload_is_observably_fatal(self) -> None:
        """The falsification, run through the durable form rather than memory.

        Editing the stored record so the actor names the reviewer's execution
        is the tamper an admission gate has to survive; it reloads cleanly and
        fails I2c, rather than reloading as something that still passes.
        """
        payload = _identities().to_payload()
        payload["actor"] = {
            **payload["reviewer"],  # type: ignore[dict-item]
            "role": ExecutionRole.ACTOR.value,
        }

        reloaded = CandidateExecutionIdentities.from_payload(payload)

        assert reloaded.satisfies_reviewer_distinctness(SHA_A) is False

    def test_an_unpinned_model_round_trips_as_an_explicit_absence(self) -> None:
        """Stored as ``null``, reloaded as ``None`` — a stated fact either way."""
        original = _identities(reviewer=_reviewer(model=None))

        payload = original.to_payload()
        assert _provenance_payload(payload, "reviewer")["model"] is None

        reloaded = CandidateExecutionIdentities.from_payload(payload)

        assert reloaded == original
        assert reloaded.reviewer.provenance.model is None

    def test_a_record_that_never_stated_a_model_does_not_parse(self) -> None:
        """Absent is not the same claim as "no model pinned".

        A payload whose ``model`` key is missing was written by something that
        never made the statement; reading it as an explicit absence would put a
        claim in the audit trail that nobody recorded.
        """
        payload = _identities().to_payload()
        del _provenance_payload(payload, "actor")["model"]

        with pytest.raises(ValueError, match="requires a model"):
            CandidateExecutionIdentities.from_payload(payload)

    @pytest.mark.parametrize("half", ["principal", "provenance"])
    def test_an_identity_missing_either_half_does_not_parse(self, half: str) -> None:
        """Principal and provenance are both required, and neither substitutes.

        A record with no principal cannot answer I2c, and one with no
        provenance is not the audit statement this record claims to be.
        """
        payload = _identities().to_payload()
        actor_payload = payload["actor"]
        assert isinstance(actor_payload, dict)
        del actor_payload[half]

        with pytest.raises(ValueError, match=half):
            CandidateExecutionIdentities.from_payload(payload)

    @pytest.mark.parametrize("missing", ["actor", "reviewer", "candidate_sha"])
    def test_a_payload_missing_any_half_does_not_parse(self, missing: str) -> None:
        payload = _identities().to_payload()
        del payload[missing]

        with pytest.raises(ValueError, match=missing):
            CandidateExecutionIdentities.from_payload(payload)

    def test_an_unknown_schema_version_fails_closed(self) -> None:
        payload = _identities().to_payload()
        payload["schema_version"] = EXECUTION_IDENTITY_SCHEMA_VERSION + 1

        with pytest.raises(ValueError, match="schema_version"):
            CandidateExecutionIdentities.from_payload(payload)

    def test_an_identity_stored_under_the_wrong_role_is_rejected(self) -> None:
        payload = _identities().to_payload()
        payload["actor"] = {  # type: ignore[assignment]
            **payload["actor"],  # type: ignore[dict-item]
            "role": ExecutionRole.REVIEWER.value,
        }

        with pytest.raises(ValueError, match="filed as actor"):
            CandidateExecutionIdentities.from_payload(payload)
