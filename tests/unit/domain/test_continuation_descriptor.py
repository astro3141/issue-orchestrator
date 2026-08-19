"""The recorded continuation intent (#143, #149).

The descriptor's whole job is to be a COPY. These tests hold it to that: every
field it can hold has an authoritative producer, and every way of producing one
without a producer — a default, a lenient parse, a silently dropped field —
must be impossible rather than merely unused.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.continuation_descriptor import (
    CONTINUATION_DESCRIPTOR_SCHEMA_VERSION,
    ContinuationDescriptor,
)
from issue_orchestrator.domain.models import RequestedAction

PUBLISH_COMMAND = "make validate-pr-raw"


def _descriptor(*actions: RequestedAction) -> ContinuationDescriptor:
    return ContinuationDescriptor(
        requested_actions=tuple(actions),
        implementation="what the agent claimed to build",
        problems="None",
        suite="publish_gate",
        command=PUBLISH_COMMAND,
        profile="default",
    )


class TestRecordedIntent:
    def test_create_pr_is_read_from_the_recorded_actions(self) -> None:
        assert _descriptor(RequestedAction.CREATE_PR).creates_pr is True

    def test_an_intent_without_create_pr_never_asks_for_one(self) -> None:
        assert _descriptor(RequestedAction.PUSH_BRANCH).creates_pr is False

    def test_an_empty_intent_asks_for_nothing(self) -> None:
        """Empty intent is a real recorded state, distinct from no descriptor."""
        assert _descriptor().creates_pr is False

    def test_the_contract_identity_is_compared_whole(self) -> None:
        descriptor = _descriptor(RequestedAction.CREATE_PR)

        assert descriptor.matches_contract(
            suite="publish_gate", command=PUBLISH_COMMAND, profile="default"
        )
        assert not descriptor.matches_contract(
            suite="publish_gate", command=PUBLISH_COMMAND, profile="strict"
        )


class TestConstructionRefusals:
    @pytest.mark.parametrize("field_name", ["suite", "command", "profile"])
    def test_a_blank_contract_field_is_refused(self, field_name: str) -> None:
        kwargs = {
            "requested_actions": (),
            "implementation": "",
            "problems": "",
            "suite": "publish_gate",
            "command": PUBLISH_COMMAND,
            "profile": "default",
        }
        kwargs[field_name] = "   "

        with pytest.raises(ValueError, match=field_name):
            ContinuationDescriptor(**kwargs)  # type: ignore[arg-type]

    def test_an_action_outside_the_closed_set_is_refused(self) -> None:
        """An unknown action must never be carried as an opaque string: the PR
        gate compares against the enum, and a string it cannot match would read
        as "the agent asked for nothing"."""
        with pytest.raises(ValueError):
            ContinuationDescriptor(
                requested_actions=("merge_to_main",),  # type: ignore[arg-type]
                implementation="",
                problems="",
                suite="publish_gate",
                command=PUBLISH_COMMAND,
                profile="default",
            )

    def test_an_unknown_schema_version_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            ContinuationDescriptor(
                requested_actions=(),
                implementation="",
                problems="",
                suite="publish_gate",
                command=PUBLISH_COMMAND,
                profile="default",
                schema_version=CONTINUATION_DESCRIPTOR_SCHEMA_VERSION + 1,
            )


class TestRoundTrip:
    def test_a_descriptor_survives_serialisation_unchanged(self) -> None:
        descriptor = _descriptor(
            RequestedAction.CREATE_PR, RequestedAction.POST_COMMENT
        )

        assert ContinuationDescriptor.from_payload(descriptor.to_payload()) == descriptor

    def test_action_order_is_preserved(self) -> None:
        descriptor = _descriptor(
            RequestedAction.POST_COMMENT, RequestedAction.CREATE_PR
        )

        reloaded = ContinuationDescriptor.from_payload(descriptor.to_payload())

        assert reloaded.requested_actions == (
            RequestedAction.POST_COMMENT,
            RequestedAction.CREATE_PR,
        )


class TestDamageIsNotAbsence:
    """A corrupt record raises; it never reads as "the agent asked for nothing"."""

    def test_an_unknown_action_raises(self) -> None:
        payload = _descriptor(RequestedAction.CREATE_PR).to_payload()
        payload["requested_actions"] = ["merge_to_main"]

        with pytest.raises(ValueError, match="unknown continuation descriptor"):
            ContinuationDescriptor.from_payload(payload)

    def test_a_missing_action_list_raises(self) -> None:
        payload = _descriptor(RequestedAction.CREATE_PR).to_payload()
        del payload["requested_actions"]

        with pytest.raises(ValueError, match="requested_actions"):
            ContinuationDescriptor.from_payload(payload)

    def test_a_missing_schema_version_raises(self) -> None:
        payload = _descriptor(RequestedAction.CREATE_PR).to_payload()
        del payload["schema_version"]

        with pytest.raises(ValueError, match="schema_version"):
            ContinuationDescriptor.from_payload(payload)

    def test_a_non_string_implementation_raises(self) -> None:
        payload = _descriptor(RequestedAction.CREATE_PR).to_payload()
        payload["implementation"] = {"claimed": "everything"}

        with pytest.raises(ValueError, match="implementation"):
            ContinuationDescriptor.from_payload(payload)
