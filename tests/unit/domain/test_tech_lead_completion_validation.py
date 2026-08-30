"""The evidence a Tech Lead completion is gated on says what it is about (#385).

Everything downstream — the fail-closed policy, the durable store, the refusal
an operator reads — rests on this value being unable to describe itself
vaguely. So the proofs here are on the invariants rather than on the getters:
evidence with no candidate cannot exist, evidence for another candidate does
not bind, and a payload this build cannot interpret raises instead of
half-parsing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from issue_orchestrator.domain.tech_lead_completion_validation import (
    TECH_LEAD_COMPLETION_VALIDATION_SCHEMA,
    TechLeadCompletionValidation,
    TechLeadCompletionValidationStatus,
)

RUN_ID = "20260830T101112000000Z"
SESSION = "issue-385"
HEAD = "a" * 40


def _validation(**overrides) -> TechLeadCompletionValidation:
    kwargs = {
        "run_id": RUN_ID,
        "session_name": SESSION,
        "candidate_head_sha": HEAD,
        "status": TechLeadCompletionValidationStatus.PASSED,
        "detail": "the checkout is clean (dirty_check='tracked')",
    }
    kwargs.update(overrides)
    return TechLeadCompletionValidation.concluded(**kwargs)


class TestOnlyAPassPermitsCompletion:
    @pytest.mark.parametrize(
        "status",
        [
            TechLeadCompletionValidationStatus.FAILED,
            TechLeadCompletionValidationStatus.TIMED_OUT,
            TechLeadCompletionValidationStatus.UNAVAILABLE,
        ],
    )
    def test_every_other_status_refuses(
        self, status: TechLeadCompletionValidationStatus
    ) -> None:
        assert not status.permits_completion
        assert not _validation(status=status, detail="nope").permits_completion

    def test_passed_permits(self) -> None:
        assert TechLeadCompletionValidationStatus.PASSED.permits_completion
        assert _validation().permits_completion

    def test_every_member_answers_the_question(self) -> None:
        """A new status cannot be added without deciding what it permits."""
        permitting = [
            status
            for status in TechLeadCompletionValidationStatus
            if status.permits_completion
        ]
        assert permitting == [TechLeadCompletionValidationStatus.PASSED]


class TestEvidenceMustNameWhatItIsAbout:
    @pytest.mark.parametrize(
        "field", ["run_id", "session_name", "candidate_head_sha", "detail"]
    )
    def test_an_empty_identity_field_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            _validation(**{field: "   "})

    def test_a_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            TechLeadCompletionValidation(
                run_id=RUN_ID,
                session_name=SESSION,
                candidate_head_sha=HEAD,
                status=TechLeadCompletionValidationStatus.PASSED,
                detail="clean",
                recorded_at=datetime(2026, 8, 30, 10, 11, 12),
            )

    def test_a_failing_verdict_still_names_its_candidate(self) -> None:
        """The failing directions are the ones a stale reuse would abuse."""
        failed = _validation(
            status=TechLeadCompletionValidationStatus.FAILED, detail="dirty"
        )
        assert failed.candidate_head_sha == HEAD


class TestBindingIsExact:
    def test_the_same_run_session_and_commit_binds(self) -> None:
        assert _validation().binds_to(
            run_id=RUN_ID, session_name=SESSION, candidate_head_sha=HEAD
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"run_id": "other-run"},
            {"session_name": "issue-999"},
            {"candidate_head_sha": "b" * 40},
        ],
    )
    def test_any_difference_does_not_bind(self, kwargs: dict[str, str]) -> None:
        query = {
            "run_id": RUN_ID,
            "session_name": SESSION,
            "candidate_head_sha": HEAD,
            **kwargs,
        }
        assert not _validation().binds_to(**query)


class TestThePersistedFormRoundTrips:
    def test_payload_round_trip_preserves_every_field(self) -> None:
        original = _validation(
            status=TechLeadCompletionValidationStatus.TIMED_OUT, detail="slow"
        )

        restored = TechLeadCompletionValidation.from_payload(original.to_payload())

        assert restored == original

    def test_the_payload_is_schema_stamped(self) -> None:
        assert (
            _validation().to_payload()["schema"]
            == TECH_LEAD_COMPLETION_VALIDATION_SCHEMA
        )

    @pytest.mark.parametrize(
        "payload, match",
        [
            ("not-an-object", "JSON object"),
            ({"schema": 999}, "unknown"),
            (
                {
                    "schema": TECH_LEAD_COMPLETION_VALIDATION_SCHEMA,
                    "run_id": RUN_ID,
                    "session_name": SESSION,
                },
                "incomplete",
            ),
        ],
    )
    def test_unusable_payloads_raise(self, payload: object, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            TechLeadCompletionValidation.from_payload(payload)

    def test_an_unknown_status_raises_rather_than_defaulting(self) -> None:
        payload = _validation().to_payload()
        payload["status"] = "probably_fine"

        with pytest.raises(ValueError):
            TechLeadCompletionValidation.from_payload(payload)

    def test_recorded_at_survives_as_an_aware_datetime(self) -> None:
        original = _validation(recorded_at=datetime(2026, 8, 30, tzinfo=timezone.utc))

        restored = TechLeadCompletionValidation.from_payload(original.to_payload())

        assert restored.recorded_at == datetime(2026, 8, 30, tzinfo=timezone.utc)
