"""Copying recorded intent at the publication-gate verdict seam (#143, #149).

The one moment the descriptor can be written is the moment the gate reaches a
verdict, because the completion record it copies is destroyed with the worktree
shortly afterwards. These tests hold the writer to what it may copy, when it
may write at all, and what it must leave alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.continuation_descriptor_writer import (
    ContinuationDescriptorWriter,
)
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.ports.session_output import ValidationRecord

REPO = "owner/repo"
SHA_A = "a" * 40
SHA_A_PRIME = "b" * 40
PUBLISH_COMMAND = "make validate-pr-raw"
PROFILE = "default"


def _issue_key() -> GitHubIssueKey:
    return GitHubIssueKey(repo=REPO, external_id="149")


def _key(head_sha: str = SHA_A) -> AttemptKey:
    return AttemptKey(_issue_key(), head_sha)


def _completion(*actions: RequestedAction, **overrides: object) -> CompletionRecord:
    fields: dict[str, object] = {
        "session_id": "issue-149",
        "timestamp": "2026-08-19T00:00:00Z",
        "outcome": CompletionOutcome.COMPLETED,
        "summary": "done",
        "requested_actions": list(actions),
        "implementation": "what the agent claimed to build",
        "problems": "a real caveat",
    }
    fields.update(overrides)
    return CompletionRecord(**fields)  # type: ignore[arg-type]


def _gate_record(head_sha: str = SHA_A, *, passed: bool = False) -> ValidationRecord:
    return ValidationRecord(
        schema_version=1,
        suite=ValidationGateKind.PUBLISH.suite,
        head_sha=head_sha,
        passed=passed,
        exit_code=0 if passed else 1,
        command=PUBLISH_COMMAND,
        started_at="2026-08-19T00:00:00Z",
        ended_at="2026-08-19T00:01:00Z",
        profile=PROFILE,
    )


@pytest.fixture
def attempts(tmp_path: Path) -> SidecarAttemptStore:
    return SidecarAttemptStore(tmp_path)


@pytest.fixture
def writer(attempts: SidecarAttemptStore) -> ContinuationDescriptorWriter:
    return ContinuationDescriptorWriter(attempts)


class TestWhenIntentIsRecorded:
    def test_a_refused_candidate_records_its_intent(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        stored = attempts.for_key(_key())
        assert stored is not None
        assert stored.continuation_descriptor is not None

    def test_a_passing_candidate_records_nothing(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        """The ordinary path owns a candidate that cleared the gate. A
        descriptor here would invite a second, terminal-less driver to race it."""
        written = writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(passed=True),
        )

        assert written is None
        assert attempts.for_key(_key()) is None

    def test_a_gate_record_with_no_exact_commit_records_nothing(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        written = writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record("abc1234"),
        )

        assert written is None


class TestWhatIsCopied:
    def test_the_agents_own_fields_are_copied_verbatim(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(
                RequestedAction.CREATE_PR, RequestedAction.POST_COMMENT
            ),
            gate_record=_gate_record(),
        )

        stored = attempts.for_key(_key())
        assert stored is not None
        descriptor = stored.continuation_descriptor
        assert descriptor is not None
        assert descriptor.requested_actions == (
            RequestedAction.CREATE_PR,
            RequestedAction.POST_COMMENT,
        )
        assert descriptor.implementation == "what the agent claimed to build"
        assert descriptor.problems == "a real caveat"

    def test_the_contract_identity_comes_from_the_gates_own_record(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        stored = attempts.for_key(_key())
        assert stored is not None
        descriptor = stored.continuation_descriptor
        assert descriptor is not None
        assert descriptor.matches_contract(
            suite=ValidationGateKind.PUBLISH.suite,
            command=PUBLISH_COMMAND,
            profile=PROFILE,
        )

    def test_an_agent_that_asked_for_nothing_records_an_empty_intent(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        """Empty recorded intent and NO recorded intent are different states,
        and only the second forbids continuation outright."""
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(),
            gate_record=_gate_record(),
        )

        stored = attempts.for_key(_key())
        assert stored is not None
        descriptor = stored.continuation_descriptor
        assert descriptor is not None
        assert descriptor.requested_actions == ()
        assert descriptor.creates_pr is False

    def test_absent_summary_fields_are_recorded_as_written(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(
                RequestedAction.CREATE_PR, implementation=None, problems=None
            ),
            gate_record=_gate_record(),
        )

        stored = attempts.for_key(_key())
        assert stored is not None
        descriptor = stored.continuation_descriptor
        assert descriptor is not None
        assert descriptor.implementation == ""
        assert descriptor.problems == ""


class TestSupersession:
    def test_a_newer_candidates_intent_clears_the_older(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A),
        )

        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A_PRIME),
        )

        older = attempts.for_key(_key(SHA_A))
        newer = attempts.for_key(_key(SHA_A_PRIME))
        assert older is not None and older.continuation_descriptor is None
        assert newer is not None and newer.continuation_descriptor is not None

    def test_supersession_leaves_the_evaluation_history_alone(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        """Only the intent is superseded. The evidence #139 exists to preserve
        is never touched."""
        receipt = ValidationVerdictReceipt(
            suite=ValidationGateKind.PUBLISH.suite,
            head_sha=SHA_A,
            verdict=ValidationVerdict.FAILED,
            command=PUBLISH_COMMAND,
            profile=PROFILE,
        )
        attempts.update(
            _key(SHA_A),
            lambda attempt: attempt.with_completed_evaluation(receipt),
        )
        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A),
        )

        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A_PRIME),
        )

        older = attempts.for_key(_key(SHA_A))
        assert older is not None
        assert older.completed_evaluations == (receipt,)

    def test_re_recording_the_same_candidate_is_idempotent(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        first = writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        second = writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        assert first == second
        stored = attempts.for_key(_key())
        assert stored is not None
        assert stored.continuation_descriptor == first

    def test_another_issues_intent_is_never_superseded(
        self, writer: ContinuationDescriptorWriter, attempts: SidecarAttemptStore
    ) -> None:
        other = GitHubIssueKey(repo=REPO, external_id="150")
        writer.record_refused_candidate(
            issue_key=other,
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A),
        )

        writer.record_refused_candidate(
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(SHA_A_PRIME),
        )

        untouched = attempts.for_key(AttemptKey(other, SHA_A))
        assert untouched is not None
        assert untouched.continuation_descriptor is not None


class TestDamageIsNeverSilent:
    def test_an_unwritable_store_records_nothing_rather_than_half(self) -> None:
        class Refusing:
            def for_key(self, key: AttemptKey) -> Attempt | None:
                return None

            def update(self, key: AttemptKey, mutate: object) -> Attempt:
                raise OSError("read-only filesystem")

            def for_issue(self, issue_key: object) -> tuple[()]:
                return ()

            def supersede_issue(self, issue_key: object) -> int:
                return 0

        written = ContinuationDescriptorWriter(Refusing()).record_refused_candidate(  # type: ignore[arg-type]
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        assert written is None

    def test_an_unreadable_store_aborts_before_writing_a_second_intent(self) -> None:
        """A supersession that could not be established must abort the write it
        precedes, or the issue ends up offering two candidates at once."""

        class Blind:
            def __init__(self) -> None:
                self.writes: list[AttemptKey] = []

            def for_key(self, key: AttemptKey) -> Attempt | None:
                return None

            def update(self, key: AttemptKey, mutate: object) -> Attempt:
                self.writes.append(key)
                return Attempt(key=key)

            def for_issue(self, issue_key: object) -> tuple[()]:
                raise ValueError("Attempt sidecars are unreadable")

            def supersede_issue(self, issue_key: object) -> int:
                return 0

        store = Blind()
        written = ContinuationDescriptorWriter(store).record_refused_candidate(  # type: ignore[arg-type]
            issue_key=_issue_key(),
            completion=_completion(RequestedAction.CREATE_PR),
            gate_record=_gate_record(),
        )

        assert written is None
        assert store.writes == []
