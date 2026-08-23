"""Tests for the ADR-0031 tech_lead session policy owner."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control import tech_lead_session_policy
from issue_orchestrator.control.completion_pr_collision import NoCommitsBetweenError
from issue_orchestrator.control.tech_lead_evidence import EVIDENCE_MAP_FILENAME
from issue_orchestrator.control.tech_lead_session_policy import (
    _stage_evidence_map,
    is_benign_tech_lead_no_commits,
    is_tech_lead_session,
    read_tech_lead_assignment,
    shape_requested_actions_for_tech_lead,
)
from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadSessionFlavor,
)


class TestIsTechLeadSession:
    @pytest.mark.parametrize(
        ("tech_lead_agent", "agent_type", "expected"),
        [
            ("agent:tech-lead", "agent:tech-lead", True),
            ("agent:tech-lead", "agent:web", False),
            ("agent:tech-lead", None, False),
            (None, "agent:tech-lead", False),
            (None, None, False),
            ("", "agent:tech-lead", False),
            ("", "", False),
        ],
    )
    def test_matrix(
        self, tech_lead_agent: str | None, agent_type: str | None, expected: bool
    ) -> None:
        assert is_tech_lead_session(tech_lead_agent, agent_type) is expected


class TestShapeRequestedActionsForTechLead:
    def test_drops_only_post_comment(self) -> None:
        requested = (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.CREATE_PR,
            RequestedAction.POST_COMMENT,
        )

        shaped = shape_requested_actions_for_tech_lead(requested)

        assert shaped == (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR)

    def test_preserves_order_and_other_actions(self) -> None:
        requested = (
            RequestedAction.POST_COMMENT,
            RequestedAction.PUSH_BRANCH,
            RequestedAction.ADD_BLOCKED_LABEL,
            RequestedAction.POST_COMMENT,
        )

        shaped = shape_requested_actions_for_tech_lead(requested)

        assert shaped == (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.ADD_BLOCKED_LABEL,
        )

    def test_no_post_comment_is_identity(self) -> None:
        requested = (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR)

        assert shape_requested_actions_for_tech_lead(requested) == requested


class TestIsBenignTechLeadNoCommits:
    def test_true_only_for_create_pr_with_no_commits_error(self) -> None:
        error = NoCommitsBetweenError(base="main", head="issue-1")

        assert is_benign_tech_lead_no_commits(RequestedAction.CREATE_PR, error) is True

    @pytest.mark.parametrize(
        "action",
        [a for a in RequestedAction if a is not RequestedAction.CREATE_PR],
    )
    def test_false_for_other_actions(self, action: RequestedAction) -> None:
        error = NoCommitsBetweenError(base="main", head="issue-1")

        assert is_benign_tech_lead_no_commits(action, error) is False

    def test_false_for_other_errors_on_create_pr(self) -> None:
        assert (
            is_benign_tech_lead_no_commits(
                RequestedAction.CREATE_PR, RuntimeError("boom")
            )
            is False
        )


class TestReadTechLeadAssignment:
    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert read_tech_lead_assignment(tmp_path) is None

    def test_reads_assignment_from_tech_lead_data(self, tmp_path: Path) -> None:
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=99,
            focus_reason="hang",
        )
        assignment.write(tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME)

        assert read_tech_lead_assignment(tmp_path) == assignment

    def test_malformed_content_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1, "flavor": "bogus"}')

        with pytest.raises(ValueError, match="flavor"):
            read_tech_lead_assignment(tmp_path)


class TestDiscardTechLeadAuthorityAfterCompletion:
    """The retention owner drops BOTH tech_lead records at a run's terminal.

    The run-keyed launch authority and the anchor-keyed storm cohort (#6780)
    have different keys but the same end: once the review's run is over,
    neither may outlive it. The cohort's discard is what releases its members'
    held run artifacts for cleanup.
    """

    @staticmethod
    def _config():
        from issue_orchestrator.infra.config import Config

        config = Config(repo="test/repo")
        config.tech_lead_review_agent = "agent:tech-lead"
        return config

    @staticmethod
    def _session(agent_type: str):
        from unittest.mock import MagicMock

        session = MagicMock()
        session.issue.number = 999
        session.issue.agent_type = agent_type
        session.run_assets.run_id = "r1"
        session.run_assets.session_name = "issue-999"
        return session

    @staticmethod
    def _store_with_both_rows():
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        store = InMemoryTechLeadAuthorityStore()
        store.record(
            run_id="r1",
            session_name="issue-999",
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
                anchor_issue_number=999,
                problem_issue_numbers=(41, 42),
            ),
        )
        store.record_storm_cohort(
            anchor_issue_number=999,
            cohort=tuple(
                DiscoveredFailure(number, f"Problem {number}", "failed")
                for number in (41, 42)
            ),
        )
        return store

    def test_terminal_completion_discards_authority_and_cohort(self) -> None:
        from issue_orchestrator.control.tech_lead_completion import (
            discard_tech_lead_authority_after_completion,
        )

        store = self._store_with_both_rows()

        discard_tech_lead_authority_after_completion(
            self._config(),
            store,
            self._session("agent:tech-lead"),
            processing_errors=None,
        )

        assert store.load(run_id="r1", session_name="issue-999") is None
        assert store.load_storm_cohort(anchor_issue_number=999) is None
        assert store.list_storm_cohorts() == ()

    def test_publish_failure_retains_both_for_the_retry(self) -> None:
        """A publish-stage failure re-enters completion for this same run, so
        neither record may be dropped yet."""
        from issue_orchestrator.control.completion_types import ERROR_PREFIX_PUSH
        from issue_orchestrator.control.tech_lead_completion import (
            discard_tech_lead_authority_after_completion,
        )

        store = self._store_with_both_rows()

        discard_tech_lead_authority_after_completion(
            self._config(),
            store,
            self._session("agent:tech-lead"),
            processing_errors=[f"{ERROR_PREFIX_PUSH}: remote rejected"],
        )

        assert store.load(run_id="r1", session_name="issue-999") is not None
        assert store.load_storm_cohort(anchor_issue_number=999) is not None

    def test_non_tech_lead_session_touches_nothing(self) -> None:
        from issue_orchestrator.control.tech_lead_completion import (
            discard_tech_lead_authority_after_completion,
        )

        store = self._store_with_both_rows()

        discard_tech_lead_authority_after_completion(
            self._config(),
            store,
            self._session("agent:coder"),
            processing_errors=None,
        )

        assert store.load(run_id="r1", session_name="issue-999") is not None
        assert store.load_storm_cohort(anchor_issue_number=999) is not None


class TestStageEvidenceMap:
    """The evidence-map wiring: flavor gating, best-effort, manifest recording."""

    @staticmethod
    def _config(tmp_path: Path) -> SimpleNamespace:
        repo_root = tmp_path / "repo"
        repo_root.mkdir(exist_ok=True)
        return SimpleNamespace(
            repo_root=repo_root,
            repo="owner/repo",
            worktree_base=tmp_path,
            worktree_base_branch_override=None,
        )

    @staticmethod
    def _ctx() -> tuple[SimpleNamespace, dict]:
        manifest: dict = {}
        return SimpleNamespace(update_manifest=manifest.update), manifest

    @staticmethod
    def _register_worktree(repo_root: Path, worktree: Path) -> None:
        """Make ``worktree`` a real linked git worktree of ``repo_root`` (#6824 R4)."""
        git_common = repo_root / ".git"
        git_common.mkdir(parents=True, exist_ok=True)
        wt_gitdir = git_common / "worktrees" / worktree.name
        wt_gitdir.mkdir(parents=True, exist_ok=True)
        (wt_gitdir / "commondir").write_text("../..\n")
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text(f"gitdir: {wt_gitdir}\n")

    @staticmethod
    def _host() -> SimpleNamespace:
        return SimpleNamespace(
            get_default_branch=lambda: "main",
            get_issue=lambda n: SimpleNamespace(
                number=n, state="open", labels=["blocked-failed"]
            ),
            get_prs_for_issue=lambda n, state="open": [
                SimpleNamespace(
                    number=6770,
                    state="merged",
                    base_branch="6593-predecessor",
                    branch="6335-work",
                    url="https://example/pr/6770",
                )
            ],
        )

    @staticmethod
    def _board(recent_failures: tuple = ()) -> SimpleNamespace:
        return SimpleNamespace(recent_failures=list(recent_failures))

    def test_failure_investigation_writes_map_and_records_manifest(
        self, tmp_path: Path
    ) -> None:
        ctx, manifest = self._ctx()
        run_dir = tmp_path / "run"
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=self._host(),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=6335,
            board_snapshot=self._board(),
        )
        path = run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME
        assert path.is_file()
        assert manifest["evidence_map"] == str(path)
        data = json.loads(path.read_text())
        assert data["focus_issue_number"] == 6335
        assert data["github"]["issue"]["state"] == "OPEN"
        pr = data["github"]["prs"][0]
        assert pr["merged"] is True
        assert pr["base_ref"] == "6593-predecessor"

    def test_batch_review_stages_nothing(self, tmp_path: Path) -> None:
        ctx, manifest = self._ctx()
        run_dir = tmp_path / "run"
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=self._host(),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            focus_issue_number=None,
            board_snapshot=self._board(),
        )
        assert not (run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME).exists()
        assert "evidence_map" not in manifest

    def test_health_review_stages_whole_system_map(self, tmp_path: Path) -> None:
        # A health review has no focus, so it gets the full SYSTEM substrate:
        # a null github block, but run-dirs enumerated across ALL worktrees.
        ctx, _manifest = self._ctx()
        config = self._config(tmp_path)
        run_dir = tmp_path / "run"
        # R4 (#6824): only REGISTERED worktrees of this repo are swept.
        self._register_worktree(config.repo_root, tmp_path / "repo-100")
        whole_system = (
            tmp_path
            / "repo-100"
            / ".issue-orchestrator"
            / "sessions"
            / "20260101T000000__coding-1"
        )
        whole_system.mkdir(parents=True)
        _stage_evidence_map(
            config=config,
            repository_host=self._host(),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            focus_issue_number=None,
            board_snapshot=self._board(),
        )
        data = json.loads(
            (run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME).read_text()
        )
        assert data["focus_issue_number"] is None
        assert data["github"] is None
        # Whole-system run-dirs are enumerated across worktrees, not empty.
        assert str(whole_system.resolve()) in data["run_dirs"]

    def test_github_read_failure_does_not_fail_launch(self, tmp_path: Path) -> None:
        # A GitHub/network error degrades the warm-cache to null; the evidence
        # map is still written and the launch proceeds.
        def _boom(*_a, **_k):
            raise RuntimeError("github down")

        ctx, manifest = self._ctx()
        run_dir = tmp_path / "run"
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=SimpleNamespace(get_issue=_boom, get_prs_for_issue=_boom),
            ctx=ctx,
            run_dir=run_dir,
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=6335,
            board_snapshot=self._board(),
        )
        path = run_dir / "tech-lead-data" / EVIDENCE_MAP_FILENAME
        assert path.is_file()
        assert json.loads(path.read_text())["github"] is None
        assert manifest["evidence_map"] == str(path)

    def test_write_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The outer best-effort catch: a write failure must neither raise nor
        # record a manifest entry pointing at a file that was not written.
        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(tech_lead_session_policy, "write_evidence_map", _boom)
        ctx, manifest = self._ctx()
        _stage_evidence_map(
            config=self._config(tmp_path),
            repository_host=self._host(),
            ctx=ctx,
            run_dir=tmp_path / "run",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=6335,
            board_snapshot=self._board(),
        )
        assert "evidence_map" not in manifest


class TestLaunchBaseSha:
    """The launch owner records the commit the run was handed (#202).

    The zero-code lane at completion rests entirely on this fact being
    orchestrator-observed at launch: the agent-writable run directory carries
    copies for the agent to READ, and none of them may stand in for it.
    """

    LAUNCH_SHA = "e" * 40

    @staticmethod
    def _config(tmp_path: Path):
        from issue_orchestrator.infra.config import Config

        config = Config(repo="owner/repo")
        config.tech_lead_review_agent = "agent:tech-lead"
        config.repo_root = tmp_path / "repo"
        config.repo_root.mkdir(parents=True, exist_ok=True)
        return config

    @staticmethod
    def _ctx(tmp_path: Path):
        worktree = tmp_path / "scratch"
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "run"
        run_dir.mkdir(parents=True)
        return SimpleNamespace(
            run=SimpleNamespace(
                run_dir=run_dir, run_id="run-1", session_name="issue-109"
            ),
            worktree_path=worktree,
            update_manifest=lambda _entries: None,
        )

    @staticmethod
    def _board_provider():
        from issue_orchestrator.domain.board_snapshot import BoardSnapshot

        return SimpleNamespace(
            snapshot=lambda focus, problems=(): BoardSnapshot(
                generated_at="2026-08-23T00:00:00Z", orchestrator_paused=False
            )
        )

    def _record(self, tmp_path: Path, working_copy):
        from issue_orchestrator.control.tech_lead_session_policy import (
            prepare_tech_lead_session_data,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchScope
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        store = InMemoryTechLeadAuthorityStore()
        prepare_tech_lead_session_data(
            config=self._config(tmp_path),
            repository_host=SimpleNamespace(),
            manifest_downloader=SimpleNamespace(),
            tech_lead_authority=store,
            board_snapshot_provider=self._board_provider(),
            working_copy=working_copy,
            issue=SimpleNamespace(
                number=109, title="Investigate", agent_type="agent:tech-lead", labels=[]
            ),
            ctx=self._ctx(tmp_path),
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
            ),
        )
        return store.load(run_id="run-1", session_name="issue-109")

    def test_the_recorded_base_is_the_checkout_head_the_orchestrator_read(
        self, tmp_path: Path
    ) -> None:
        seen: list[Path] = []

        def _head(worktree: Path) -> str:
            seen.append(worktree)
            return self.LAUNCH_SHA

        authority = self._record(
            tmp_path, SimpleNamespace(get_head_sha=_head)
        )

        assert authority is not None
        assert authority.launch_base_sha == self.LAUNCH_SHA
        # Read from the run's OWN checkout, not the repo root or the run dir.
        assert seen == [tmp_path / "scratch"]

    def test_an_unreadable_head_records_nothing_and_still_launches(
        self, tmp_path: Path
    ) -> None:
        """Forfeiting the lane is the fail-closed cost; failing the launch is not."""
        authority = self._record(
            tmp_path, SimpleNamespace(get_head_sha=lambda _worktree: None)
        )

        assert authority is not None
        assert authority.launch_base_sha == ""


class TestFocusedScratchWorktree:
    """A focused run reads its subject's branch; it must never write to it."""

    @staticmethod
    def _config(tmp_path: Path):
        return SimpleNamespace(repo_root=tmp_path / "repo")

    @staticmethod
    def _issue(number: int = 109):
        return SimpleNamespace(number=number, title="Prepare the thing")

    @pytest.mark.parametrize(
        ("flavor", "branch_stem"),
        [
            (
                TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                "tech-lead-investigation-109-",
            ),
            (
                TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                "tech-lead-planning-109-",
            ),
        ],
    )
    def test_a_focused_run_gets_a_disposable_worktree(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor, branch_stem: str
    ) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            focused_tech_lead_scratch_identity,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchScope

        identity = focused_tech_lead_scratch_identity(
            self._config(tmp_path),
            self._issue(),
            TechLeadLaunchScope(flavor=flavor),
        )

        assert identity is not None
        assert identity.branch_name.startswith(branch_stem)
        # The disposable branch must not look like the subject's own branch to
        # ``extract_issue_number_from_branch``.
        assert not identity.branch_name[0].isdigit()
        assert "tech-lead-109-" in identity.worktree_name

    @pytest.mark.parametrize(
        "flavor",
        [TechLeadSessionFlavor.BATCH_REVIEW, TechLeadSessionFlavor.HEALTH_REVIEW],
    )
    def test_whole_board_runs_keep_their_anchor_worktree(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor
    ) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            focused_tech_lead_scratch_identity,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchScope

        assert (
            focused_tech_lead_scratch_identity(
                self._config(tmp_path),
                self._issue(),
                TechLeadLaunchScope(flavor=flavor),
            )
            is None
        )

    def test_two_planning_runs_of_one_issue_do_not_collide(
        self, tmp_path: Path
    ) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            focused_tech_lead_scratch_identity,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchScope

        scope = TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION
        )
        first = focused_tech_lead_scratch_identity(
            self._config(tmp_path), self._issue(), scope
        )
        second = focused_tech_lead_scratch_identity(
            self._config(tmp_path), self._issue(), scope
        )

        assert first is not None and second is not None
        assert first.branch_name != second.branch_name
        assert first.worktree_name != second.worktree_name


class TestRecoveredLaunchScope:
    """A restarted session comes back as the run it was launched as (#136)."""

    @staticmethod
    def _config():
        return SimpleNamespace(tech_lead_review_agent="agent:tech-lead")

    @staticmethod
    def _run():
        return SimpleNamespace(run_id="run-1", session_name="issue-109")

    @staticmethod
    def _issue():
        # An ordinary board issue: no marker label, no batch title signature —
        # indistinguishable from a failure investigation's subject.
        return SimpleNamespace(
            number=109,
            title="Prepare the thing",
            labels=["agent:tech-lead"],
            agent_type="agent:tech-lead",
        )

    class _Authority:
        def __init__(self, recorded=None):
            self._recorded = recorded

        def load(self, *, run_id, session_name):
            assert (run_id, session_name) == ("run-1", "issue-109")
            return self._recorded

        def load_storm_cohort(self, *, anchor_issue_number):
            return None

    def test_a_restored_planning_run_is_not_downgraded_to_an_investigation(
        self,
    ) -> None:
        """The recorded authority is the only signal that can tell them apart.

        Guessing from labels would restore the least-authority role holding the
        recovery role's scope.
        """
        from issue_orchestrator.control.tech_lead_session_policy import (
            recover_tech_lead_launch_scope,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadLaunchAuthority,
        )

        recovered = recover_tech_lead_launch_scope(
            self._config(),
            self._issue(),
            self._Authority(
                TechLeadLaunchAuthority(
                    flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                    anchor_issue_number=109,
                    focus_issue_number=109,
                )
            ),
            run=self._run(),
        )

        assert recovered is not None
        assert recovered.flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION

    def test_a_run_predating_the_ledger_still_falls_back_to_inference(self) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            recover_tech_lead_launch_scope,
        )

        recovered = recover_tech_lead_launch_scope(
            self._config(), self._issue(), self._Authority(None), run=self._run()
        )

        assert recovered is not None
        assert recovered.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
