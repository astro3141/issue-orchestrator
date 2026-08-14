"""One canonical issue key, on every path that files or restores evidence (#40).

``github_issue_key(repo, number, title)`` is the one derivation of a work
item's stable identity. It is title-aware: an issue titled ``[M1-011] ...``
keys as ``repo:M1-011``, not ``repo:38``. Two paths used to spell a number-only
key by hand instead:

- the completion path's validation attempt identity, which is the key
  validation evidence for a candidate is filed under, and
- session restoration, which rebuilds a live session's identity after a
  restart.

For an unprefixed title - every issue in this repository today - both
spellings agree, which is exactly why the split was latent. These tests use a
prefixed title, where they do not agree, and keep the unprefixed lane pinned
beside it so the ordinary case is proven rather than assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.session_completion import process_active_sessions
from issue_orchestrator.control.session_controller import SessionDecision
from issue_orchestrator.control.session_restorer import SessionRestorer
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.execution_identity import (
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
    ExecutionPrincipal,
    ExecutionProvenance,
    ExecutionRole,
)
from issue_orchestrator.domain.issue_key import (
    FakeIssueKey,
    GitHubIssueKey,
    IssueKey,
    github_issue_key,
)
from issue_orchestrator.domain.models import (
    OrchestratorState,
    Session,
    SessionStatus,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.attempt_execution_identity_store import (
    AttemptExecutionIdentityStore,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.observation.observation import SessionObservationResult
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.test_session_restorer import (
    MockWorkingCopy,
    make_agent_config,
    make_config,
    make_discovered_session,
)

REPO = "astro3141/issue-orchestrator"
ISSUE_NUMBER = 38
PREFIXED_TITLE = "[M1-011] Example"
PLAIN_TITLE = "Example"
CANDIDATE_SHA = "a" * 40


def _issue(title: str) -> GitHubIssue:
    """The issue snapshot production runs on, repo stamped by the adapter."""
    return GitHubIssue(
        number=ISSUE_NUMBER,
        repo=REPO,
        title=title,
        labels=("agent:backend",),
    )


class _FakeRepositoryHost:
    """Only what ``SessionRestorer`` asks of the repository host."""

    def __init__(self, issue: GitHubIssue | None) -> None:
        self._issue = issue
        self.get_issue_calls: list[int] = []

    def get_issue(self, issue_number: int) -> GitHubIssue | None:
        self.get_issue_calls.append(issue_number)
        return self._issue


def _identities() -> CandidateExecutionIdentities:
    return CandidateExecutionIdentities(
        candidate_sha=CANDIDATE_SHA,
        actor=AgentExecutionIdentity(
            role=ExecutionRole.ACTOR,
            principal=ExecutionPrincipal(agent_label="agent:backend"),
            provenance=ExecutionProvenance(provider="claude-code", model="opus"),
        ),
        reviewer=AgentExecutionIdentity(
            role=ExecutionRole.REVIEWER,
            principal=ExecutionPrincipal(agent_label="agent:reviewer"),
            provenance=ExecutionProvenance(provider="codex", model="gpt-5"),
        ),
        observed_at="2026-08-14T00:00:00+00:00",
    )


def _review_evidence_key(title: str) -> IssueKey:
    """The key ``completion_review_exchange`` files its #34 records under.

    That module derives it from ``github_issue_key(config.repo, number, title)``
    and hands the same value to the exchange runner, which is what the
    execution-identity store keys its attempt record by - pinned by
    ``tests/unit/test_completion_review_exchange_async.py::TestExchangeRecordScope``.
    """
    return github_issue_key(repo=REPO, number=ISSUE_NUMBER, title=title)


def _validation_evidence_key(title: str, tmp_path: Path) -> IssueKey:
    """The key the completion path actually reaches ``decide_outcome`` with.

    Driven through ``process_active_sessions`` rather than the private helper,
    so what is captured is the identity a real completion would file validation
    evidence under.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)

    config = Config()
    config.repo = REPO
    config.repo_root = tmp_path

    session = Session(
        # Deliberately NOT the issue's key: the validation identity must be
        # derived from the work item, so a session key that disagreed with it
        # cannot be what this captures.
        key=SessionKey(issue=FakeIssueKey("unused-session-scope"), task=TaskKind.CODE),
        issue=_issue(title),
        agent_config=make_agent_config(tmp_path),
        terminal_id=f"issue-{ISSUE_NUMBER}",
        worktree_path=worktree,
        branch_name=f"{ISSUE_NUMBER}-branch",
        run_assets=make_session_run_assets(
            worktree, session_name=f"issue-{ISSUE_NUMBER}"
        ),
    )

    state = OrchestratorState()
    state.active_sessions = [session]

    observer = MagicMock()
    observer.observe_session.return_value = SessionObservationResult.terminated()

    captured: list[Any] = []

    def _decide(*_args: Any, **kwargs: Any) -> SessionDecision:
        captured.append(kwargs["issue_key"])
        # A deferral: the identity is all this proof is about, so nothing
        # downstream of the decision runs.
        return SessionDecision(status=SessionStatus.RUNNING, reason="deferred")

    controller = MagicMock()
    controller.decide_outcome.side_effect = _decide

    process_active_sessions(
        state=state,
        observer=observer,
        session_controller=controller,
        completion_handler=MagicMock(),
        action_applier=MagicMock(),
        worktree_manager=None,
        kill_session_fn=MagicMock(),
        config=config,
        pending_work_claims=SqlitePendingWorkClaimStore.for_repo(tmp_path / "claims"),
    )

    assert len(captured) == 1
    return captured[0]


def _restore(title: str, tmp_path: Path, *, issue_available: bool = True):
    """Restore one discovered terminal the way a restart does."""
    worktree = tmp_path / f"repo-{ISSUE_NUMBER}"
    worktree.mkdir()

    config = make_config(
        agents={"agent:backend": make_agent_config(tmp_path)}, repo=REPO
    )
    config.repo_root = tmp_path

    host = _FakeRepositoryHost(_issue(title) if issue_available else None)
    working_copy = MockWorkingCopy()
    working_copy.branches[worktree] = f"{ISSUE_NUMBER}-branch"

    restorer = SessionRestorer(config, host, working_copy)
    return restorer.restore_sessions(
        [make_discovered_session(ISSUE_NUMBER, worktree=worktree)],
        already_tracked=[],
    )


class TestPrefixedIdentityCoherence:
    """Required proof 1: one ``(issue, A)`` for all of a candidate's evidence."""

    def test_validation_evidence_is_filed_under_the_stable_id(
        self, tmp_path: Path
    ) -> None:
        key = _validation_evidence_key(PREFIXED_TITLE, tmp_path)

        assert key.stable_id() == "M1-011"
        assert key.scope() == REPO
        # The same key the issue snapshot itself derives - one rule, one owner.
        assert key == _issue(PREFIXED_TITLE).key

    def test_validation_and_review_evidence_land_on_one_attempt_record(
        self, tmp_path: Path
    ) -> None:
        """Admission asking ``(issue, A)`` must see both halves, not one.

        The execution-principal half is written under the key the review
        exchange derives; the validation half is filed under the completion
        path's key. Reading one back through the other is what proves they are
        the same attempt rather than two.
        """
        attempts = tmp_path / "repo-root"
        attempts.mkdir()
        store = AttemptExecutionIdentityStore(SidecarAttemptStore(attempts))
        store.record(
            AttemptKey(_review_evidence_key(PREFIXED_TITLE), CANDIDATE_SHA),
            _identities(),
        )

        validation_key = _validation_evidence_key(PREFIXED_TITLE, tmp_path)

        assert store.read(AttemptKey(validation_key, CANDIDATE_SHA)) == _identities()

    def test_a_number_only_derivation_would_split_the_evidence(
        self, tmp_path: Path
    ) -> None:
        """The failure direction the proof above pins (#21 §9).

        Re-introducing ``GitHubIssueKey(repo, str(number))`` at the validation
        site would file that half under this key, and admission would find the
        execution-principal half missing rather than fail loudly.
        """
        attempts = tmp_path / "repo-root"
        attempts.mkdir()
        store = AttemptExecutionIdentityStore(SidecarAttemptStore(attempts))
        store.record(
            AttemptKey(_review_evidence_key(PREFIXED_TITLE), CANDIDATE_SHA),
            _identities(),
        )

        number_only = GitHubIssueKey(repo=REPO, external_id=str(ISSUE_NUMBER))

        assert number_only != _review_evidence_key(PREFIXED_TITLE)
        assert store.read(AttemptKey(number_only, CANDIDATE_SHA)) is None


class TestRestartIdentityCoherence:
    """Required proofs 2 and 3b: restart may not change the work item."""

    def test_a_restored_session_keeps_the_work_items_identity(
        self, tmp_path: Path
    ) -> None:
        restored = _restore(PREFIXED_TITLE, tmp_path)

        assert len(restored) == 1
        session = restored[0]
        assert session.key.issue == session.issue.key
        assert session.key.issue == _review_evidence_key(PREFIXED_TITLE)
        assert session.key.issue.stable_id() == "M1-011"

    def test_restore_is_declined_when_the_authoritative_title_is_unavailable(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        """No canonical identity, no restoration - not a downgraded one.

        The tab text a terminal carries is a UI label, and a locally rebuilt
        issue has no repository either, so for ``[M1-011] ...`` neither can
        prove the stable id. Continuing would file the restored session under
        ``repo:38`` while every other record for that issue uses
        ``repo:M1-011``.
        """
        with caplog.at_level("WARNING"):
            restored = _restore(PREFIXED_TITLE, tmp_path, issue_available=False)

        assert restored == []
        assert "canonical identity cannot be proven" in caplog.text


class TestOrdinaryLaneIsUnchanged:
    """Required proof 4: unprefixed titles behave exactly as they did."""

    def test_validation_evidence_still_keys_on_the_issue_number(
        self, tmp_path: Path
    ) -> None:
        key = _validation_evidence_key(PLAIN_TITLE, tmp_path)

        assert key == GitHubIssueKey(repo=REPO, external_id=str(ISSUE_NUMBER))
        assert key == _review_evidence_key(PLAIN_TITLE)

    def test_a_restored_session_still_keys_on_the_issue_number(
        self, tmp_path: Path
    ) -> None:
        restored = _restore(PLAIN_TITLE, tmp_path)

        assert len(restored) == 1
        session = restored[0]
        assert session.key.issue == GitHubIssueKey(
            repo=REPO, external_id=str(ISSUE_NUMBER)
        )
        assert session.key.issue == session.issue.key
