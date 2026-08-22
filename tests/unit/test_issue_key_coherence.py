"""One canonical issue key, on every path that files or restores evidence (#40).

``github_issue_key(repo, number, title)`` is the one derivation of a work
item's stable identity. It is title-aware: an issue titled ``[M1-011] ...``
keys as ``repo:M1-011``, not ``repo:38``. Two paths used to spell a number-only
key by hand instead:

- the completion path's validation attempt identity, which is the key
  validation evidence for a candidate is filed under - now taken from the
  session's own ``key.issue``, the identity it was launched under, rather than
  re-derived from its work-item snapshot (which is synthetic on the rework and
  review paths), and
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
    GitHubIssueKey,
    IssueKey,
)
from issue_orchestrator.domain.models import (
    Issue as LegacyIssue,
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
from issue_orchestrator.ports.issue import Issue as IssueProtocol
from issue_orchestrator.domain.review_exchange_rework import ReviewExchangeRework
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.test_completion_review_exchange_async import (
    _build as build_review_exchange,
    _FakeJobRunner,
    _FakeReviewExchangeRunner,
)
from tests.unit.test_session_restorer import (
    MockWorkingCopy,
    make_agent_config,
    make_config,
    make_discovered_session,
)

REPO = "astro3141/issue-orchestrator"
ISSUE_NUMBER = 38
REWORK_PR_NUMBER = 99
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


def _review_evidence_key(title: str, tmp_path: Path) -> IssueKey:
    """The key ``completion_review_exchange`` actually files its #34 records under.

    Driven through ``run_review_exchange_loop`` and captured at the
    ``ReviewExchangeRunner`` port rather than re-derived here: re-deriving
    would only restate the rule this test is trying to prove both paths obey,
    and would keep passing if the exchange stopped obeying it. The captured
    value is what the execution-identity store keys its attempt record by.
    """
    captured: list[Any] = []

    class _CapturingRunner(_FakeReviewExchangeRunner):
        def run(self, **kwargs: Any) -> Any:
            captured.append(kwargs["issue_key"])
            return super().run(**kwargs)

    exchange_dir = tmp_path / f"exchange-{len(title)}"
    exchange_dir.mkdir(exist_ok=True)
    review, session_output = build_review_exchange(
        exchange_dir,
        _FakeJobRunner(),
        [],
        [],
        repo=REPO,
        review_exchange_runner=_CapturingRunner(),
    )
    review.run_review_exchange_loop(
        exchange_run=session_output.cached_review_run(),
        worktree=exchange_dir,
        issue_number=ISSUE_NUMBER,
        issue_title=title,
        session_name="coding-1",
        agent_label="agent:backend",
        rework=ReviewExchangeRework.IN_EXCHANGE,
    )

    assert len(captured) == 1
    return captured[0]


def _validation_evidence_key(
    title: str,
    tmp_path: Path,
    *,
    issue: IssueProtocol | None = None,
    session_issue_key: IssueKey | None = None,
) -> IssueKey:
    """The key the completion path actually reaches ``decide_outcome`` with.

    Driven through ``process_active_sessions`` rather than the private helper,
    so what is captured is the identity a real completion would file validation
    evidence under. ``issue`` overrides the work-item snapshot the session
    carries and ``session_issue_key`` its launched-under identity, so the two
    can be made to disagree the way the rework path makes them disagree.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)

    config = Config()
    config.repo = REPO
    config.repo_root = tmp_path

    work_item = issue if issue is not None else _issue(title)
    session = Session(
        # The session's own identity, which is what the completion path must
        # file under: canonical by construction at every launch site, unlike
        # the snapshot below.
        key=SessionKey(
            issue=(
                session_issue_key
                if session_issue_key is not None
                else _issue(title).key
            ),
            task=TaskKind.CODE,
        ),
        issue=work_item,
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

    def test_a_repoless_work_item_still_keys_on_the_stable_id(
        self, tmp_path: Path
    ) -> None:
        """The scope is the one part a work-item snapshot may not carry.

        The legacy ``domain.models.Issue`` defaults ``repo`` to ``""``, so its
        own ``.key`` is scoped to nothing. The completion path never reads it:
        it files under the session's identity, which carries both halves.
        """
        key = _validation_evidence_key(
            PREFIXED_TITLE,
            tmp_path,
            issue=LegacyIssue(
                number=ISSUE_NUMBER, title=PREFIXED_TITLE, labels=["agent:backend"]
            ),
        )

        assert key == _issue(PREFIXED_TITLE).key
        assert key.scope() == REPO

    def test_a_synthetic_snapshot_cannot_change_the_filed_identity(
        self, tmp_path: Path
    ) -> None:
        """The rework shape, which is the whole reason to ask the session.

        ``session_rework_launcher`` builds its work item as
        ``Issue(number, "Rework #<pr>")`` with no repo, while the session it
        launches keys on ``rework.issue_key`` - the title-aware key the adapter
        derived from the real issue. ``TaskKind.REWORK`` is not
        ``is_review_only``, so this completion does reach the validation gate.
        Deriving from the snapshot would file ``repo:38`` against a claim,
        review record and coding attempt that all say ``repo:M1-011``.
        """
        key = _validation_evidence_key(
            PREFIXED_TITLE,
            tmp_path,
            issue=LegacyIssue(
                number=ISSUE_NUMBER,
                title=f"Rework #{REWORK_PR_NUMBER}",
                labels=["agent:backend"],
            ),
            session_issue_key=_issue(PREFIXED_TITLE).key,
        )

        assert key == _issue(PREFIXED_TITLE).key
        assert key != GitHubIssueKey(repo=REPO, external_id=str(ISSUE_NUMBER))

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
            AttemptKey(_review_evidence_key(PREFIXED_TITLE, tmp_path), CANDIDATE_SHA),
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
            AttemptKey(_review_evidence_key(PREFIXED_TITLE, tmp_path), CANDIDATE_SHA),
            _identities(),
        )

        number_only = GitHubIssueKey(repo=REPO, external_id=str(ISSUE_NUMBER))

        assert number_only != _review_evidence_key(PREFIXED_TITLE, tmp_path)
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
        assert session.key.issue == _review_evidence_key(PREFIXED_TITLE, tmp_path)
        assert session.key.issue.stable_id() == "M1-011"

    def test_a_restored_session_files_its_evidence_under_that_identity(
        self, tmp_path: Path
    ) -> None:
        """The two halves of #40 composed: restart, then complete.

        Restoration decides the identity; the completion path files under it.
        Proving them together is what the issue actually claims - that a
        restart cannot move a candidate's evidence to a different work item.
        """
        restored = _restore(PREFIXED_TITLE, tmp_path)

        assert len(restored) == 1
        key = _validation_evidence_key(
            PREFIXED_TITLE, tmp_path, session_issue_key=restored[0].key.issue
        )

        assert key == _review_evidence_key(PREFIXED_TITLE, tmp_path)

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
        assert key == _review_evidence_key(PLAIN_TITLE, tmp_path)

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
