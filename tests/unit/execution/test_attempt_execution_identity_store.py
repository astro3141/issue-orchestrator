"""Durability of candidate execution-identity evidence.

Foundation admission reads this evidence after the sessions that produced it —
and their worktrees — are gone, so "durable" is asserted by destroying things,
not by reasoning about where the bytes live.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.execution_identity import (
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
    ExecutionPrincipal,
    ExecutionProvenance,
    ExecutionRole,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.execution.attempt_execution_identity_store import (
    AttemptExecutionIdentityStore,
)

ISSUE = GitHubIssueKey(repo="acme/repo", external_id="34")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _identities(
    candidate_sha: str, *, reviewer_model: str | None = "gpt-5"
) -> CandidateExecutionIdentities:
    return CandidateExecutionIdentities(
        candidate_sha=candidate_sha,
        actor=AgentExecutionIdentity(
            role=ExecutionRole.ACTOR,
            principal=ExecutionPrincipal(agent_label="agent:backend"),
            provenance=ExecutionProvenance(provider="claude-code", model="opus"),
        ),
        reviewer=AgentExecutionIdentity(
            role=ExecutionRole.REVIEWER,
            principal=ExecutionPrincipal(agent_label="agent:reviewer"),
            provenance=ExecutionProvenance(
                provider="codex", model=reviewer_model
            ),
        ),
        observed_at="2026-08-14T00:00:00+00:00",
    )


def _store(repo_root: Path) -> AttemptExecutionIdentityStore:
    return AttemptExecutionIdentityStore(SidecarAttemptStore(repo_root))


class TestRecordAndRead:
    def test_recorded_identities_read_back_for_their_candidate(
        self, tmp_path: Path
    ) -> None:
        key = AttemptKey(ISSUE, "a" * 40)
        _store(tmp_path).record(key, _identities("a" * 40))

        assert _store(tmp_path).read(key) == _identities("a" * 40)

    def test_an_unpinned_model_survives_the_durable_round_trip(
        self, tmp_path: Path
    ) -> None:
        """A reviewer whose CLI chose its own model is still admissible evidence.

        The record states the absence rather than omitting it, so what comes
        back is "no model was pinned" — not "this record forgot to say".
        """
        key = AttemptKey(ISSUE, "a" * 40)
        _store(tmp_path).record(key, _identities("a" * 40, reviewer_model=None))

        restored = _store(tmp_path).read(key)

        assert restored is not None
        assert restored.reviewer.provenance.model is None
        assert restored.principals_are_distinct() is True

    def test_an_unrecorded_candidate_reads_as_absent(self, tmp_path: Path) -> None:
        assert _store(tmp_path).read(AttemptKey(ISSUE, "b" * 40)) is None

    def test_evidence_cannot_be_filed_under_a_commit_it_does_not_describe(
        self, tmp_path: Path
    ) -> None:
        """The exact-``A`` binding is the storage key, so this cannot be faked."""
        with pytest.raises(ValueError, match="must name the attempt's own commit"):
            _store(tmp_path).record(
                AttemptKey(ISSUE, "a" * 40), _identities("b" * 40)
            )

    def test_recording_preserves_the_attempt_s_other_facts(
        self, tmp_path: Path
    ) -> None:
        """Validation's half of §4 lives on the same record and must survive."""
        key = AttemptKey(ISSUE, "a" * 40)
        attempts = SidecarAttemptStore(tmp_path)
        attempts.update(
            key,
            lambda attempt: replace(
                attempt, validation_record_path="/runs/1/validation-record.json"
            ),
        )

        AttemptExecutionIdentityStore(attempts).record(key, _identities("a" * 40))

        stored = attempts.for_key(key)
        assert stored is not None
        assert stored.validation_record_path == "/runs/1/validation-record.json"
        assert stored.execution_identities == _identities("a" * 40)

    def test_a_damaged_record_raises_rather_than_reading_as_absent(
        self, tmp_path: Path
    ) -> None:
        """Corruption is not absence: a gate must not read one as the other."""
        key = AttemptKey(ISSUE, "a" * 40)
        _store(tmp_path).record(key, _identities("a" * 40))
        sidecar = next((tmp_path / ".issue-orchestrator" / "attempts").glob("*.json"))
        payload = json.loads(sidecar.read_text())
        del payload["execution_identities"]["reviewer"]
        sidecar.write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="reviewer"):
            _store(tmp_path).read(key)


class TestSurvivesTeardown:
    def test_evidence_outlives_the_worktree_that_produced_it(
        self, tmp_path: Path
    ) -> None:
        """Removed for real — ``git worktree remove``, not a reasoned claim.

        The store is rooted at the primary checkout, which is why admission can
        still read it once the issue worktree the exchange ran in is gone.
        """
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _git(repo_root, "init", "-q", "-b", "main")
        _git(repo_root, "config", "user.email", "t@example.com")
        _git(repo_root, "config", "user.name", "T")
        (repo_root / "seed.txt").write_text("seed\n")
        _git(repo_root, "add", "seed.txt")
        _git(repo_root, "commit", "-q", "-m", "seed")

        worktree = tmp_path / "issue-34"
        _git(repo_root, "worktree", "add", "-q", "-b", "issue-34", str(worktree))
        (worktree / "work.py").write_text("# candidate\n")
        _git(worktree, "add", "work.py")
        _git(worktree, "commit", "-q", "-m", "candidate")
        candidate_sha = _git(worktree, "rev-parse", "HEAD")

        key = AttemptKey(ISSUE, candidate_sha)
        _store(repo_root).record(key, _identities(candidate_sha))

        _git(repo_root, "worktree", "remove", "--force", str(worktree))
        assert not worktree.exists()

        # A fresh store instance: nothing is served from the writer's memory,
        # so this is also the process-restart case.
        reloaded = _store(repo_root).read(key)
        assert reloaded is not None
        assert reloaded.satisfies_reviewer_distinctness(candidate_sha) is True

    def test_a_fresh_store_instance_reads_what_a_previous_one_wrote(
        self, tmp_path: Path
    ) -> None:
        """Process restart: reconstructed from storage, nothing re-inferred."""
        key = AttemptKey(ISSUE, "c" * 40)
        _store(tmp_path).record(key, _identities("c" * 40))

        assert _store(tmp_path).read(key) == _identities("c" * 40)
