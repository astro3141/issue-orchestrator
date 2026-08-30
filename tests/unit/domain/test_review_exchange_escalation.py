"""What the review exchange reads out of a coder's completion (#386).

Pure-domain tests: every input is a payload literal, every assertion is on the
value object built from it. The question these pin is narrow and load-bearing
— *does this turn escalate, and does it ask to publish?* — because the two
answers together decide whether the exchange demands current-head validation
evidence or routes a question to a human.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.models import CompletionOutcome, RequestedAction
from issue_orchestrator.domain.review_exchange_escalation import (
    CoderCompletionIntent,
    CoderEscalation,
)


_HEAD = "a" * 40


class TestCoderCompletionIntent:
    def test_needs_human_escalates(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "question": "Whose call is this?"}
        )

        assert intent.outcome is CompletionOutcome.NEEDS_HUMAN
        assert intent.escalates_to_human is True
        assert intent.question == "Whose call is this?"

    @pytest.mark.parametrize(
        "outcome",
        ["completed", "blocked", "review_approved", "review_changes_requested"],
    )
    def test_no_other_outcome_escalates(self, outcome: str) -> None:
        """Only ``needs_human`` reaches the escalation terminal.

        ``blocked`` is a nearby neighbour and deliberately not included: it
        says the coder cannot proceed, not that a human owns the next
        decision, and #386 admits only the outcome it measured.
        """
        intent = CoderCompletionIntent.from_payload({"outcome": outcome})

        assert intent.escalates_to_human is False
        assert intent.requires_publication_evidence is True

    def test_an_unreadable_outcome_escalates_nothing(self) -> None:
        """Malformed is never the cheap way to the escalation terminal."""
        intent = CoderCompletionIntent.from_payload({"outcome": "something-else"})

        assert intent.outcome is None
        assert intent.escalates_to_human is False
        assert intent.requires_publication_evidence is True

    def test_a_missing_outcome_escalates_nothing(self) -> None:
        intent = CoderCompletionIntent.from_payload({})

        assert intent.outcome is None
        assert intent.requires_publication_evidence is True

    def test_escalation_without_publication_needs_no_publish_evidence(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": ["post_comment"]}
        )

        assert intent.requests_publication is False
        assert intent.requires_publication_evidence is False

    @pytest.mark.parametrize("action", ["create_pr", "push_branch"])
    def test_escalation_that_publishes_keeps_the_evidence_requirement(
        self, action: str
    ) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": [action]}
        )

        assert intent.requests_publication is True
        assert intent.requires_publication_evidence is True

    def test_unknown_actions_are_dropped_rather_than_read_as_publication(
        self,
    ) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": ["not_an_action"]}
        )

        assert intent.requested_actions == ()
        assert intent.requests_publication is False

    def test_actions_that_are_not_a_list_are_no_actions(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": "create_pr"}
        )

        assert intent.requested_actions == ()
        assert intent.requests_publication is False

    def test_known_actions_survive_beside_unknown_ones(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"requested_actions": ["nope", "create_pr"]}
        )

        assert intent.requested_actions == (RequestedAction.CREATE_PR,)

    @pytest.mark.parametrize("value", ["", "   ", 7, None])
    def test_blank_or_non_string_text_reads_as_absent(self, value: object) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "question": value, "context": value}
        )

        assert intent.question is None
        assert intent.context is None


class TestCoderEscalation:
    @staticmethod
    def _escalation(**overrides: object) -> CoderEscalation:
        fields: dict[str, object] = {
            "issue_number": 386,
            "session_name": "review-exchange-386",
            "round_index": 2,
            "head_sha": _HEAD,
            "raised_at": "2026-08-31T00:00:00+00:00",
            "question": "Who owns this policy?",
        }
        fields.update(overrides)
        return CoderEscalation(**fields)  # type: ignore[arg-type]

    def test_payload_round_trips(self) -> None:
        escalation = self._escalation(context="Reviewer asked for X.")

        assert CoderEscalation.from_payload(escalation.to_payload()) == escalation

    def test_absent_text_is_omitted_rather_than_written_as_null(self) -> None:
        payload = self._escalation(question=None, context=None).to_payload()

        assert "question" not in payload
        assert "context" not in payload

    def test_detail_names_the_commit_and_the_question(self) -> None:
        detail = self._escalation().detail

        assert _HEAD[:12] in detail
        assert "Who owns this policy?" in detail
        assert "round 2" in detail

    def test_detail_is_still_attributable_without_question_text(self) -> None:
        detail = self._escalation(question=None).detail

        assert _HEAD[:12] in detail
        assert "no question text supplied" in detail

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("session_name", ""),
            ("head_sha", ""),
            ("raised_at", ""),
        ],
    )
    def test_identity_fields_must_be_present(
        self, field_name: str, value: str
    ) -> None:
        with pytest.raises(ValueError, match=field_name):
            self._escalation(**{field_name: value})

    def test_round_index_must_name_a_real_round(self) -> None:
        with pytest.raises(ValueError, match="round_index"):
            self._escalation(round_index=0)

    def test_a_payload_missing_its_binding_is_rejected(self) -> None:
        payload = self._escalation().to_payload()
        del payload["head_sha"]

        with pytest.raises(ValueError, match="head_sha"):
            CoderEscalation.from_payload(payload)

    def test_a_payload_without_an_issue_is_rejected(self) -> None:
        payload = self._escalation().to_payload()
        payload["issue_number"] = "386"

        with pytest.raises(ValueError, match="issue_number"):
            CoderEscalation.from_payload(payload)

    def test_publication_flag_must_be_a_bool(self) -> None:
        payload = self._escalation().to_payload()
        payload["requested_publication"] = "yes"

        with pytest.raises(ValueError, match="requested_publication"):
            CoderEscalation.from_payload(payload)
