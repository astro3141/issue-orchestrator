"""The one durable spelling of an ``IssueKey``, shared by every artifact.

Two artifacts persist a work item's identity across a restart: the pending-work
claim and (since #85) the publish-retry locators, whose republish files a
publish verdict on ``Attempt(issue, A)``. If they spelled a key differently,
each would round-trip to something the other could not recognise, and the only
symptom would be evidence filed under an identity nothing else uses.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.issue_key import FakeIssueKey, GitHubIssueKey
from issue_orchestrator.domain.issue_key_codec import (
    IssueKeyDecodeError,
    decode_issue_key,
    encode_issue_key,
)


def _identity(key) -> tuple[str, str]:
    return (key.scope(), str(key.stable_id()))


def test_a_key_round_trips_to_the_same_work_item() -> None:
    key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")

    assert decode_issue_key(encode_issue_key(key)) == key


def test_a_foreign_implementation_round_trips_to_the_same_identity() -> None:
    """Identity is the protocol's scope + stable id, not the class that held it.

    The rebuilt key is a ``GitHubIssueKey`` whatever went in, which is the
    contract this codec advertises — and it is what ``AttemptStore`` keys on,
    so the rebuilt key reaches the same attempt as the original.
    """
    key = FakeIssueKey(name="M1-011", test_scope="owner/repo")

    rebuilt = decode_issue_key(encode_issue_key(key))

    assert _identity(rebuilt) == _identity(key)


def test_a_non_object_payload_is_corruption_not_an_absent_key() -> None:
    with pytest.raises(IssueKeyDecodeError):
        decode_issue_key("owner/repo:M1-011")


@pytest.mark.parametrize("missing", ["scope", "stable_id"])
def test_a_half_written_payload_names_the_missing_half(missing: str) -> None:
    """Both halves are load-bearing: a key missing either is not a key.

    Silently accepting one would produce a key scoped to nothing (or naming
    nothing), which compares unequal to every other record for the same work
    item — the drift #40 removed, arriving through the back door.
    """
    payload = {"scope": "owner/repo", "stable_id": "M1-011"}
    del payload[missing]

    with pytest.raises(IssueKeyDecodeError, match=missing):
        decode_issue_key(payload)


def test_both_durable_artifacts_write_the_same_bytes_for_one_work_item(
    make_session, tmp_path
) -> None:
    """The reason this codec exists, asserted across its two real clients."""
    from issue_orchestrator.domain.publish_retry import PublishRetryLocators
    from issue_orchestrator.domain.pending_work import (
        PendingWorkClaim,
        PendingWorkKind,
    )
    from issue_orchestrator.domain.models import PendingReview
    from issue_orchestrator.execution.pending_work_codec import encode_claim

    key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
    session = make_session(issue_number=4057, branch_name="4057-scratch-1")
    locators = PublishRetryLocators(
        issue_number=4057,
        issue_title=session.issue.title,
        session_key=session.key.stable_id(),
        worktree_path=str(session.worktree_path),
        branch_name=session.branch_name,
        completion_path=session.completion_path,
        run_assets=session.run_assets,
        issue_key=key,
    )
    claim = PendingWorkClaim(
        PendingWorkKind.REVIEW,
        PendingReview(
            issue_key=key,
            pr_number=5453,
            pr_url="https://github.com/owner/repo/pull/5453",
            branch_name="4057-scratch-1",
            _issue_number=4057,
            agent_label="agent:web",
            issue_labels=(),
        ),
    )

    assert (
        locators.to_dict()["issue_key"]
        == encode_claim(claim)["request"]["issue_key"]
        == encode_issue_key(key)
    )
