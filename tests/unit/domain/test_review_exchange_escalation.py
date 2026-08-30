"""What the review exchange reads out of a coder's completion (#386).

Pure-domain tests: every input is a payload literal, every assertion is on the
value object built from it. The question these pin is narrow and load-bearing
— *does this turn escalate, and does it offer a change for review?* — because
the two answers together decide whether the exchange demands current-head
validation evidence or routes a question to a human.

One test here reaches for the producer's own action table
(``STATUS_TO_ACTIONS``) rather than a literal. The predicate is only correct
relative to what ``coding-done`` actually writes, and reading a literal instead
is how the exemption shipped unreachable: every payload under test carried
actions no producer emits.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
    offers_a_change_for_review,
)
from issue_orchestrator.domain.review_exchange_escalation import (
    CoderCompletionIntent,
    CoderEscalation,
)
from issue_orchestrator.entrypoints.cli_tools.agent_done import (
    STATUS_TO_ACTIONS,
    AgentStatus,
)


def _needs_human_actions() -> list[str]:
    """What ``coding-done needs_human`` writes into ``requested_actions``."""
    return [action.value for action in STATUS_TO_ACTIONS[AgentStatus.NEEDS_HUMAN]]


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

    def test_escalation_offering_no_change_needs_no_publish_evidence(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": ["post_comment"]}
        )

        assert intent.offers_a_change_for_review is False
        assert intent.requires_publication_evidence is False

    def test_the_real_needs_human_action_set_needs_no_publish_evidence(self) -> None:
        """The payload ``coding-done needs_human`` actually writes is exempt.

        ``push_branch`` is in that set — the orchestrator's standing intent to
        preserve the coder's work — so a predicate keyed on reaching the remote
        would make the exemption unreachable in production while every literal
        payload in this file still passed. If ``STATUS_TO_ACTIONS`` ever grows
        ``create_pr`` for this status, this fails here rather than silently
        re-disabling the exemption.
        """
        actions = _needs_human_actions()

        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": actions}
        )

        assert RequestedAction.PUSH_BRANCH in intent.requested_actions
        assert intent.offers_a_change_for_review is False
        assert intent.requires_publication_evidence is False

    def test_push_branch_alone_is_not_an_offer_of_a_change(self) -> None:
        """Preserving work is not putting a change up to be judged."""
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": ["push_branch"]}
        )

        assert intent.offers_a_change_for_review is False
        assert intent.requires_publication_evidence is False

    def test_escalation_that_opens_a_pr_keeps_the_evidence_requirement(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": ["create_pr"]}
        )

        assert intent.offers_a_change_for_review is True
        assert intent.requires_publication_evidence is True

    def test_a_pr_beside_the_real_needs_human_set_still_fails_closed(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {
                "outcome": "needs_human",
                "requested_actions": [*_needs_human_actions(), "create_pr"],
            }
        )

        assert intent.offers_a_change_for_review is True
        assert intent.requires_publication_evidence is True

    def test_unknown_actions_are_dropped_rather_than_read_as_an_offer(
        self,
    ) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": ["not_an_action"]}
        )

        assert intent.requested_actions == ()
        assert intent.offers_a_change_for_review is False

    def test_actions_that_are_not_a_list_are_no_actions(self) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": "create_pr"}
        )

        assert intent.requested_actions == ()
        assert intent.offers_a_change_for_review is False

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


class TestOneOwnerForTheOfferQuestion:
    """The record reader and the payload reader must answer alike (#386, A1).

    They read different shapes — ``CompletionRecord`` is the strict reader the
    orchestrator acts on, ``CoderCompletionIntent`` is the exchange reading a
    raw artifact — but "does this offer a change for review?" is one question
    with one answer, and answering it twice is what let the exchange's answer
    diverge from the publish contract's.
    """

    @staticmethod
    def _record(actions: list[str]) -> CompletionRecord:
        return CompletionRecord(
            session_id="issue-386",
            timestamp="2026-08-31T00:00:00Z",
            outcome=CompletionOutcome.NEEDS_HUMAN,
            summary="Needs human: whose call is this?",
            requested_actions=[RequestedAction(action) for action in actions],
        )

    @pytest.mark.parametrize(
        "actions",
        [
            pytest.param(_needs_human_actions(), id="real-needs-human-set"),
            pytest.param(["push_branch"], id="push-only"),
            pytest.param(["create_pr"], id="pr-only"),
            pytest.param(["push_branch", "create_pr"], id="both"),
            pytest.param(["post_comment"], id="neither"),
        ],
    )
    def test_both_readers_agree_over_the_same_actions(
        self, actions: list[str]
    ) -> None:
        intent = CoderCompletionIntent.from_payload(
            {"outcome": "needs_human", "requested_actions": actions}
        )

        assert (
            intent.offers_a_change_for_review
            is self._record(actions).offers_a_change_for_review
        )

    def test_the_shared_predicate_reads_the_action_vocabulary(self) -> None:
        """Asked of every action there is, so the answer is not a literal."""
        assert offers_a_change_for_review(tuple(RequestedAction)) is True
        assert (
            offers_a_change_for_review(
                action
                for action in RequestedAction
                if action is not RequestedAction.CREATE_PR
            )
            is False
        )


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

    def test_offer_flag_must_be_a_bool(self) -> None:
        payload = self._escalation().to_payload()
        payload["offered_a_change_for_review"] = "yes"

        with pytest.raises(ValueError, match="offered_a_change_for_review"):
            CoderEscalation.from_payload(payload)
