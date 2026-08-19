from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.continuation_settlement import (
    ContinuationSettlement,
    ContinuationSettlementKind,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey, GitHubIssueKey
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)

SHA = "a" * 40


def test_attempt_key_rejects_empty_head_sha() -> None:
    with pytest.raises(ValueError, match="head_sha"):
        AttemptKey(GitHubIssueKey("owner/repo", "6130"), "")


def test_attempt_key_rejects_short_head_sha() -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        AttemptKey(GitHubIssueKey("owner/repo", "6130"), "abc123")


def test_attempt_round_trips_to_dict() -> None:
    key = AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA)
    attempt = Attempt(
        key=key,
        reroute_budget_used=2,
        validation_record_path=".issue-orchestrator/sessions/run/validation-record.json",
        review_exchange_summary_path=".issue-orchestrator/sessions/run/review-exchange/summary.json",
        review_exchange_job_id="review-exchange:6130:abc123",
    )

    restored = Attempt.from_dict(attempt.to_dict())

    assert restored.key.issue_stable_id == "6130"
    assert restored.key.issue_scope == "owner/repo"
    assert restored.key.head_sha == SHA
    assert isinstance(restored.key.issue_key, GitHubIssueKey)
    assert restored.reroute_budget_used == 2
    assert restored.validation_record_path == attempt.validation_record_path
    assert restored.review_exchange_summary_path == attempt.review_exchange_summary_path
    assert restored.review_exchange_job_id == attempt.review_exchange_job_id


def test_attempt_rejects_negative_reroute_budget() -> None:
    with pytest.raises(ValueError, match="reroute_budget_used"):
        Attempt(
            key=AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA),
            reroute_budget_used=-1,
        )


def test_attempt_is_immutable_after_validation() -> None:
    attempt = Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA))

    with pytest.raises(FrozenInstanceError):
        setattr(attempt, "reroute_budget_used", -1)


def test_attempt_from_dict_rejects_unknown_schema_version() -> None:
    payload = Attempt(
        key=AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA)
    ).to_dict()
    # One past the newest version this code writes: an unknown schema is a
    # record written by rules it cannot claim to understand, and reading it
    # anyway would let a gate act on fields it may be misreading.
    payload["schema_version"] = 4

    with pytest.raises(ValueError, match="schema_version"):
        Attempt.from_dict(payload)


def test_attempt_to_dict_rejects_unsupported_issue_key_type() -> None:
    attempt = Attempt(key=AttemptKey(FakeIssueKey("6130"), SHA))

    with pytest.raises(ValueError, match="unsupported IssueKey type"):
        attempt.to_dict()


def _publish_receipt(
    *,
    verdict: ValidationVerdict = ValidationVerdict.PASSED,
    suite: str = "publish_gate",
) -> ValidationVerdictReceipt:
    return ValidationVerdictReceipt(
        suite=suite,
        head_sha=SHA,
        verdict=verdict,
        command="make validate-pr-raw",
        profile="default",
    )


def test_attempt_round_trips_its_evaluation_history() -> None:
    key = AttemptKey(GitHubIssueKey("owner/repo", "85"), SHA)
    failed = _publish_receipt(verdict=ValidationVerdict.FAILED)
    passed = _publish_receipt()

    restored = Attempt.from_dict(
        Attempt(key=key)
        .with_completed_evaluation(failed)
        .with_completed_evaluation(passed)
        .to_dict()
    )

    assert restored.completed_evaluations == (failed, passed)
    assert restored.latest_publication_evaluation == passed
    assert restored.publication_validation_passed is True


def test_a_sidecar_written_before_verdicts_existed_still_parses() -> None:
    """Absence is "no publication gate has reported", not a parse failure."""
    payload = Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "85"), SHA)).to_dict()
    del payload["completed_evaluations"]

    restored = Attempt.from_dict(payload)

    assert restored.completed_evaluations == ()
    assert restored.publication_validation_passed is False


def test_a_v1_sidecars_single_verdict_migrates_to_a_one_entry_history() -> None:
    """A v1 record is real evidence; refusing it would erase a gate result."""
    receipt = _publish_receipt()
    v1_payload = {
        "schema_version": 1,
        "issue_key_type": "github",
        "issue_key": "85",
        "issue_scope": "owner/repo",
        "head_sha": SHA,
        "reroute_budget_used": 0,
        "validation_record_path": None,
        "review_exchange_summary_path": None,
        "review_exchange_job_id": None,
        "execution_identities": None,
        "publication_verdict": receipt.to_payload(),
    }

    restored = Attempt.from_dict(v1_payload)

    assert restored.completed_evaluations == (receipt,)
    assert restored.publication_validation_passed is True
    assert restored.revalidation_budget_used == 0


def test_a_v1_sidecar_with_no_verdict_migrates_to_an_empty_history() -> None:
    v1_payload = {
        "schema_version": 1,
        "issue_key_type": "github",
        "issue_key": "85",
        "issue_scope": "owner/repo",
        "head_sha": SHA,
        "publication_verdict": None,
    }

    assert Attempt.from_dict(v1_payload).completed_evaluations == ()


def test_appending_never_rewrites_or_drops_an_earlier_evaluation() -> None:
    failed = _publish_receipt(verdict=ValidationVerdict.FAILED)
    passed = _publish_receipt()

    attempt = (
        Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "139"), SHA))
        .with_completed_evaluation(failed)
        .with_completed_evaluation(passed)
    )

    assert attempt.completed_evaluations == (failed, passed)
    assert attempt.completed_evaluations[0] is failed


def test_an_attempt_refuses_a_history_entry_naming_another_commit() -> None:
    other = ValidationVerdictReceipt(
        suite="publish_gate",
        head_sha="b" * 40,
        verdict=ValidationVerdict.PASSED,
        command="make validate-pr-raw",
        profile="default",
    )

    with pytest.raises(ValueError, match="must name the attempt's own commit"):
        Attempt(
            key=AttemptKey(GitHubIssueKey("owner/repo", "139"), SHA),
            completed_evaluations=(_publish_receipt(), other),
        )


def test_a_later_quick_evaluation_does_not_answer_the_publication_question() -> None:
    """A shared slot is how a quick verdict used to erase the publication one."""
    published = _publish_receipt()

    attempt = (
        Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "139"), SHA))
        .with_completed_evaluation(published)
        .with_completed_evaluation(_publish_receipt(suite="agent_gate"))
    )

    assert attempt.latest_publication_evaluation == published
    assert attempt.publication_validation_passed is True


def test_the_revalidation_allowance_is_exactly_one_and_durable() -> None:
    attempt = Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "139"), SHA))
    assert attempt.revalidation_allowance_available is True

    reserved = attempt.with_revalidation_reserved()

    assert reserved.revalidation_budget_used == 1
    assert reserved.revalidation_allowance_available is False
    assert Attempt.from_dict(reserved.to_dict()).revalidation_budget_used == 1
    with pytest.raises(ValueError, match="allowance is already spent"):
        reserved.with_revalidation_reserved()


def test_attempt_rejects_negative_revalidation_budget() -> None:
    with pytest.raises(ValueError, match="revalidation_budget_used"):
        Attempt(
            key=AttemptKey(GitHubIssueKey("owner/repo", "139"), SHA),
            revalidation_budget_used=-1,
        )


def test_the_continuation_settlement_survives_storage() -> None:
    settlement = ContinuationSettlement(
        kind=ContinuationSettlementKind.PULL_REQUEST_OPENED,
        settled_at="2026-08-19T02:00:00Z",
        pr_url="https://example.test/owner/repo/pull/7",
    )

    restored = Attempt.from_dict(
        Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "149"), SHA))
        .with_continuation_settlement(settlement)
        .to_dict()
    )

    assert restored.continuation_settlement == settlement


def test_a_sidecar_written_before_settlements_existed_reads_as_unsettled() -> None:
    """Absence is "this continuation still owes work", which permits a run."""
    payload = Attempt(
        key=AttemptKey(GitHubIssueKey("owner/repo", "149"), SHA)
    ).to_dict()
    del payload["continuation_settlement"]

    assert Attempt.from_dict(payload).continuation_settlement is None


def test_a_damaged_settlement_refuses_rather_than_reading_as_unsettled() -> None:
    """Reading damage as absence would put a finished run back on the runner."""
    payload = Attempt(
        key=AttemptKey(GitHubIssueKey("owner/repo", "149"), SHA)
    ).to_dict()
    payload["continuation_settlement"] = {"kind": "pull_request_opened"}

    with pytest.raises(ValueError):
        Attempt.from_dict(payload)


def test_retiring_the_recorded_intent_keeps_the_settlement() -> None:
    """Supersession clears intent only; what a run produced is still evidence."""
    settlement = ContinuationSettlement(
        kind=ContinuationSettlementKind.NOTHING_FURTHER_REQUESTED,
        settled_at="2026-08-19T02:00:00Z",
    )
    attempt = Attempt(
        key=AttemptKey(GitHubIssueKey("owner/repo", "149"), SHA)
    ).with_continuation_settlement(settlement)

    assert attempt.without_continuation_descriptor().continuation_settlement == (
        settlement
    )
