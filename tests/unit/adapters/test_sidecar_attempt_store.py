from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _key(issue: str = "6130", sha: str = SHA_A) -> AttemptKey:
    return AttemptKey(GitHubIssueKey("BruceBGordon/issue-orchestrator", issue), sha)


def test_sidecar_attempt_store_round_trips(tmp_path: Path) -> None:
    store = SidecarAttemptStore(tmp_path)
    key = _key()

    stored = store.update(
        key,
        lambda attempt: replace(
            attempt,
            reroute_budget_used=3,
            validation_record_path=(
                ".issue-orchestrator/sessions/run/validation-record.json"
            ),
        ),
    )
    restored = store.for_key(key)

    assert restored is not None
    assert restored.key.issue_scope == key.issue_scope
    assert restored.key.issue_stable_id == key.issue_stable_id
    assert restored.key.head_sha == key.head_sha
    assert restored.reroute_budget_used == 3
    assert restored.validation_record_path == stored.validation_record_path


def test_update_creates_the_record_when_absent(tmp_path: Path) -> None:
    """A first write is not a special case for the caller."""
    store = SidecarAttemptStore(tmp_path)
    key = _key()

    stored = store.update(key, lambda attempt: replace(attempt, reroute_budget_used=1))

    assert stored.reroute_budget_used == 1
    assert store.for_key(key) == stored


def test_update_hands_the_writer_the_facts_already_recorded(tmp_path: Path) -> None:
    """The reason this is a mutation rather than a whole-record write.

    Each writer owns one field of an attempt — validation owns the record
    path, the review exchange owns the execution identities — and both are
    durable Foundation admission evidence about the same ``(issue, commit)``.
    A writer that never sees the other's field cannot erase it.
    """
    store = SidecarAttemptStore(tmp_path)
    key = _key()
    store.update(
        key, lambda attempt: replace(attempt, validation_record_path="/records/v.json")
    )

    seen: list[Attempt] = []

    def _set_job_id(attempt: Attempt) -> Attempt:
        seen.append(attempt)
        return replace(attempt, review_exchange_job_id="job-7")

    store.update(key, _set_job_id)

    assert seen[0].validation_record_path == "/records/v.json"
    restored = store.for_key(key)
    assert restored is not None
    assert restored.review_exchange_job_id == "job-7"
    assert restored.validation_record_path == "/records/v.json"


def test_update_refuses_to_file_the_result_under_another_key(tmp_path: Path) -> None:
    """A mutation that renames the record would file evidence about A under B."""
    store = SidecarAttemptStore(tmp_path)
    key = _key()

    with pytest.raises(ValueError, match="stay under the key"):
        store.update(key, lambda attempt: replace(attempt, key=_key(sha=SHA_B)))

    assert store.for_key(key) is None


def test_sidecar_attempt_store_supersedes_only_target_issue(tmp_path: Path) -> None:
    store = SidecarAttemptStore(tmp_path)
    first = _key("6130", SHA_A)
    second = _key("6130", SHA_B)
    other = _key("6131", SHA_C)
    for key in (first, second, other):
        store.update(key, lambda attempt: attempt)

    removed = store.supersede_issue(first.issue_key)

    assert removed == 2
    assert store.for_key(first) is None
    assert store.for_key(second) is None
    assert store.for_key(other) is not None


def test_sidecar_attempt_store_fails_fast_on_key_mismatch(tmp_path: Path) -> None:
    store = SidecarAttemptStore(tmp_path)
    key = _key()
    store.update(key, lambda attempt: attempt)
    path = next((tmp_path / ".issue-orchestrator" / "attempts").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["head_sha"] = "f" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="key mismatch"):
        store.for_key(key)
