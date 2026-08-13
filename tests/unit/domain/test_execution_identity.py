"""The actor/reviewer execution identities §4's I2c is stated over.

These tests pin the record's meaning, not its plumbing: what makes two
executions "the same one", what a stored payload must contain to parse at all,
and — the one that matters most — that the distinctness check is falsifiable.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.execution_identity import (
    EXECUTION_IDENTITY_SCHEMA_VERSION,
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
    ExecutionRole,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _actor(**overrides: str) -> AgentExecutionIdentity:
    fields = {
        "agent_label": "agent:backend",
        "provider": "claude-code",
        "model": "opus",
    }
    fields.update(overrides)
    return AgentExecutionIdentity(role=ExecutionRole.ACTOR, **fields)


def _reviewer(**overrides: str) -> AgentExecutionIdentity:
    fields = {
        "agent_label": "agent:reviewer",
        "provider": "codex",
        "model": "gpt-5",
    }
    fields.update(overrides)
    return AgentExecutionIdentity(role=ExecutionRole.REVIEWER, **fields)


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
    """I2c: the reviewer identity must be distinct from the actor's."""

    def test_distinct_configurations_satisfy_i2c_for_their_own_candidate(self) -> None:
        assert _identities().satisfies_reviewer_distinctness(SHA_A) is True

    def test_making_the_actor_equal_the_reviewer_is_fatal(self) -> None:
        """The falsification the issue requires: the mutation must break it.

        A check that survives "set the actor's identity to the reviewer's" has
        pinned nothing. Role is deliberately excluded from the comparison, so
        the two records below differ *only* in the hat each wears — and that is
        exactly the arrangement I2c exists to refuse.
        """
        impostor = _identities(
            actor=_actor(
                agent_label="agent:reviewer", provider="codex", model="gpt-5"
            ),
        )

        assert impostor.roles_are_distinct() is False
        assert impostor.satisfies_reviewer_distinctness(SHA_A) is False

    @pytest.mark.parametrize(
        "differing",
        [
            {"agent_label": "agent:reviewer"},
            {"provider": "codex"},
            {"model": "gpt-5"},
        ],
        ids=["label", "provider", "model"],
    )
    def test_any_single_matching_field_is_not_enough_to_collapse_them(
        self, differing: dict[str, str]
    ) -> None:
        """One shared field does not make two executions one execution."""
        assert _identities(actor=_actor(**differing)).roles_are_distinct() is True

    def test_evidence_about_another_commit_never_satisfies_i2c(self) -> None:
        """Distinct identities bound to a different candidate answer False.

        Both halves are required. Evidence about other work is not evidence
        about this candidate no matter how distinct its two roles were.
        """
        identities = _identities(candidate_sha=SHA_A)

        assert identities.roles_are_distinct() is True
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

    @pytest.mark.parametrize("field_name", ["agent_label", "provider", "model"])
    def test_an_identity_field_may_not_be_blank(self, field_name: str) -> None:
        with pytest.raises(ValueError, match=field_name):
            _actor(**{field_name: "   "})

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
