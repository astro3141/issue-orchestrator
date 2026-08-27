"""``coding-done completed`` in an orchestrator-managed planning run (#319).

A ``planning_investigation`` Tech Lead prepares one bounded issue. It produces
no code candidate, and #289 refuses it the very gate commands the quick
contract names — so a planning completion must not read the candidate quick
configuration and must not run the gate. Every other principal keeps the gate
exactly as it was.

The proofs here are on the CALL and EFFECT boundaries rather than on a log
line: the loader and the runner are replaced with detonators, and the run's
own artifacts are inspected. Move the discrimination anywhere later than
"before the quick contract is read", or drop it, and these fail on a real
call — which is what makes them evidence rather than description.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.domain.models import COMPLETION_RECORD_PATH
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadAssignment,
    TechLeadSessionFlavor,
    tech_lead_assignment_path,
)
from issue_orchestrator.entrypoints.cli_tools.coding_done import main as coding_done_main
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.env import ENV_PREFIX, get_env

_MODULE = "issue_orchestrator.entrypoints.cli_tools.coding_done"
_ASSETS_MODULE = "issue_orchestrator.entrypoints.cli_tools.orchestrator_run_assets"
_SESSION = "test-planning-1"


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=root, capture_output=True, check=True
    )
    (root / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"], cwd=root, capture_output=True, check=True
    )


def _quick_gate_config(root: Path) -> None:
    config_dir = root / ".issue-orchestrator" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "default.yaml").write_text(
        "validation:\n  quick:\n    cmd: 'exit 0'\n    timeout_seconds: 10\n"
    )


def _managed_run(root: Path, session_id: str = _SESSION) -> Path:
    return FileSystemSessionOutput().start_run(root, session_id).run_dir


def _managed_env(run_dir: Path, session_id: str = _SESSION) -> dict[str, str]:
    return {
        f"{ENV_PREFIX}SESSION_ID": session_id,
        "ORCHESTRATOR_SESSION_ID": session_id,
        f"{ENV_PREFIX}RUN_DIR": str(run_dir),
    }


def _stage_assignment(
    run_dir: Path,
    flavor: TechLeadSessionFlavor,
    focus_issue_number: int | None = None,
) -> None:
    """Stage the launch-time copy the way the launcher itself stages it."""
    TechLeadAssignment(
        flavor=flavor, focus_issue_number=focus_issue_number
    ).write(tech_lead_assignment_path(run_dir))


def _stage_raw_assignment(run_dir: Path, payload: str) -> None:
    path = tech_lead_assignment_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)


@pytest.fixture
def managed_worktree(tmp_path, monkeypatch):
    """A git worktree with a configured quick gate and one managed run."""
    _git_repo(tmp_path)
    _quick_gate_config(tmp_path)
    run_dir = _managed_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path, run_dir


def _complete(argv_extra: tuple[str, ...] = ()) -> None:
    argv = [
        "coding-done",
        "completed",
        "--implementation",
        "Prepared the leaf",
        "--problems",
        "None",
        *argv_extra,
    ]
    with patch("sys.argv", argv):
        with patch(
            "issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id",
            return_value=_SESSION,
        ):
            coding_done_main()


def _completion_record(worktree: Path) -> dict:
    return json.loads((worktree / COMPLETION_RECORD_PATH).read_text())


@patch(f"{_MODULE}.check_dirty_files", return_value=[])
class TestPlanningRunSkipsTheCandidateQuickGate:
    def test_neither_the_loader_nor_the_runner_is_called(
        self, _dirty, managed_worktree
    ):
        """The call boundary: a planning completion touches neither."""
        worktree, run_dir = managed_worktree
        _stage_assignment(
            run_dir, TechLeadSessionFlavor.PLANNING_INVESTIGATION, focus_issue_number=319
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            with patch(f"{_MODULE}.load_validation_cmd") as loader:
                with patch(f"{_MODULE}.run_validation") as runner:
                    _complete()

        loader.assert_not_called()
        runner.assert_not_called()
        assert (worktree / COMPLETION_RECORD_PATH).exists()

    def test_the_real_quick_gate_never_executes_and_leaves_no_evidence(
        self, _dirty, managed_worktree
    ):
        """The effect boundary: no gate evidence, no fabricated PASS."""
        worktree, run_dir = managed_worktree
        _stage_assignment(
            run_dir, TechLeadSessionFlavor.PLANNING_INVESTIGATION, focus_issue_number=319
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert not (run_dir / "validation-record.json").exists()
        assert not (run_dir / "validation-stdout.log").exists()
        assert not (run_dir / "validation-stderr.log").exists()
        record = _completion_record(worktree)
        assert record.get("validation_record_path") is None
        assert record["outcome"] == "completed"
        assert record["implementation"] == "Prepared the leaf"

    def test_it_says_the_gate_was_skipped_rather_than_claiming_a_pass(
        self, _dirty, managed_worktree, capsys
    ):
        worktree, run_dir = managed_worktree
        _stage_assignment(
            run_dir, TechLeadSessionFlavor.PLANNING_INVESTIGATION, focus_issue_number=319
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        out = capsys.readouterr().out
        assert "Skipping code-candidate quick validation" in out
        assert "planning_investigation" in out
        assert "Validation: passed" not in out
        assert "Validation: failed" not in out

    def test_a_failing_gate_command_cannot_fail_a_planning_completion(
        self, _dirty, tmp_path, monkeypatch
    ):
        """The gate this run is refused is also the gate that would fail it."""
        _git_repo(tmp_path)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 1'\n    timeout_seconds: 10\n"
        )
        run_dir = _managed_run(tmp_path)
        monkeypatch.chdir(tmp_path)
        _stage_assignment(
            run_dir, TechLeadSessionFlavor.PLANNING_INVESTIGATION, focus_issue_number=319
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert (tmp_path / COMPLETION_RECORD_PATH).exists()

    def test_the_routing_hint_is_not_written_into_the_completion_record(
        self, _dirty, managed_worktree
    ):
        """Nothing decided from the agent-writable hint travels downstream.

        Zero-code/publication/effect authority stays with
        ``TechLeadLaunchAuthority`` plus orchestrator-observed HEAD
        (#202/#257); the record the orchestrator consumes carries no flavor,
        no route and no claim about which gate ran.
        """
        worktree, run_dir = managed_worktree
        _stage_assignment(
            run_dir, TechLeadSessionFlavor.PLANNING_INVESTIGATION, focus_issue_number=319
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        record = _completion_record(worktree)
        serialized = json.dumps(record)
        assert "planning_investigation" not in serialized
        assert "flavor" not in record
        assert "gate_route" not in record
        assert "completion_gate_routing" not in record


class TestPlanningRunKeepsThePreCompletionRefusals:
    def test_a_dirty_tree_is_still_refused_before_success(
        self, managed_worktree, capsys
    ):
        worktree, run_dir = managed_worktree
        _stage_assignment(
            run_dir, TechLeadSessionFlavor.PLANNING_INVESTIGATION, focus_issue_number=319
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            with patch(
                f"{_MODULE}.check_dirty_files",
                return_value=["M  src/issue_orchestrator/domain/models.py"],
            ):
                with pytest.raises(SystemExit) as exit_info:
                    _complete()

        assert exit_info.value.code == 1
        assert "WORKING TREE IS DIRTY" in capsys.readouterr().out
        assert not (worktree / COMPLETION_RECORD_PATH).exists()


@patch(f"{_MODULE}.check_dirty_files", return_value=[])
class TestEveryOtherPrincipalKeepsTheCandidateQuickGate:
    def test_ordinary_actor_runs_the_gate(self, _dirty, managed_worktree):
        worktree, run_dir = managed_worktree

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        validation_record = run_dir / "validation-record.json"
        assert validation_record.exists()
        assert json.loads(validation_record.read_text())["passed"] is True
        assert _completion_record(worktree)["validation_record_path"] == str(
            validation_record
        )

    def test_the_managed_run_is_proven_once_for_the_whole_completion(
        self, _dirty, managed_worktree
    ):
        """One proof, two phases: the router reads it, the gate spends it.

        Routing and the gate ask the same question of the same unchanging
        inputs — the injected env var, the run directory, its manifest — so
        two proofs could not disagree, only cost.
        """
        worktree, run_dir = managed_worktree

        with patch.dict(os.environ, _managed_env(run_dir)):
            with patch(
                f"{_ASSETS_MODULE}.get_env", wraps=get_env
            ) as injected_context_read:
                _complete()

        # The proof starts by reading the owner's injected RUN_DIR, so one
        # read is one proof — and the gate still got its assets.
        assert [
            call.args for call in injected_context_read.call_args_list
        ] == [("RUN_DIR",)]
        assert (run_dir / "validation-record.json").exists()

    @pytest.mark.parametrize(
        "flavor,focus",
        [
            (TechLeadSessionFlavor.BATCH_REVIEW, None),
            (TechLeadSessionFlavor.HEALTH_REVIEW, None),
            (TechLeadSessionFlavor.FAILURE_INVESTIGATION, 42),
        ],
    )
    def test_every_non_planning_tech_lead_flavor_runs_the_gate(
        self, _dirty, managed_worktree, flavor, focus
    ):
        worktree, run_dir = managed_worktree
        _stage_assignment(run_dir, flavor, focus_issue_number=focus)

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert (run_dir / "validation-record.json").exists()
        assert _completion_record(worktree)["validation_record_path"] == str(
            run_dir / "validation-record.json"
        )

    def test_a_failing_gate_still_refuses_an_ordinary_completion(
        self, _dirty, tmp_path, monkeypatch, capsys
    ):
        _git_repo(tmp_path)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 1'\n    timeout_seconds: 10\n"
        )
        run_dir = _managed_run(tmp_path)
        monkeypatch.chdir(tmp_path)

        with patch.dict(os.environ, _managed_env(run_dir)):
            with pytest.raises(SystemExit) as exit_info:
                _complete()

        assert exit_info.value.code == 1
        assert "VALIDATION FAILED" in capsys.readouterr().out
        assert not (tmp_path / COMPLETION_RECORD_PATH).exists()


@patch(f"{_MODULE}.check_dirty_files", return_value=[])
class TestPlanningEvidenceFailsSafe:
    def test_malformed_assignment_falls_back_to_the_candidate_gate(
        self, _dirty, managed_worktree
    ):
        worktree, run_dir = managed_worktree
        _stage_raw_assignment(run_dir, "{ this is not json")

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert (run_dir / "validation-record.json").exists()

    def test_a_non_object_assignment_falls_back_instead_of_crashing(
        self, _dirty, managed_worktree
    ):
        """A corrupted hint must not become an internal error (#319 F1).

        ``[]`` is valid JSON, so the read succeeds and only the parser can
        refuse it. While that refusal was an ``AttributeError`` rather than a
        ValueError it escaped the routing owner and propagated out of ``main``
        — ``safe_main`` then wrote an ERROR completion record and exited 1,
        which is the same "cannot fail out of the completion gracefully" class
        this change exists to close, reached through the agent-writable file.
        The completion here runs to a normal end, on the ordinary gate.
        """
        worktree, run_dir = managed_worktree
        _stage_raw_assignment(run_dir, "[]")

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert (run_dir / "validation-record.json").exists()
        assert _completion_record(worktree)["outcome"] == "completed"

    def test_planning_flavor_without_its_focus_issue_falls_back(
        self, _dirty, managed_worktree
    ):
        worktree, run_dir = managed_worktree
        _stage_raw_assignment(
            run_dir,
            json.dumps(
                {
                    "schema_version": 1,
                    "flavor": "planning_investigation",
                    "focus_issue_number": None,
                    "focus_reason": "",
                }
            ),
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert (run_dir / "validation-record.json").exists()

    def test_out_of_run_planning_evidence_grants_nothing(
        self, _dirty, managed_worktree
    ):
        """An assignment in some OTHER run's directory is not this run's.

        Nothing searches the worktree for planning evidence: only the
        directory the session owner injected, and only after this session's
        manifest proved it, is read at all.
        """
        worktree, run_dir = managed_worktree
        foreign_run = _managed_run(worktree, "some-other-session")
        _stage_assignment(
            foreign_run,
            TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            focus_issue_number=319,
        )

        with patch.dict(os.environ, _managed_env(run_dir)):
            _complete()

        assert (run_dir / "validation-record.json").exists()

    def test_an_injected_run_dir_for_another_session_is_refused_not_routed(
        self, _dirty, managed_worktree, capsys
    ):
        """Ordinary behaviour is preserved exactly, including its refusals.

        Fail-safe means falling back to what the ordinary path does — and
        what it does with an injected run directory belonging to a different
        session is refuse the completion, not skip its gate.
        """
        worktree, run_dir = managed_worktree
        foreign_run = _managed_run(worktree, "some-other-session")
        _stage_assignment(
            foreign_run,
            TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            focus_issue_number=319,
        )

        with patch.dict(os.environ, _managed_env(foreign_run)):
            with pytest.raises(SystemExit) as exit_info:
                _complete()

        assert exit_info.value.code == 1
        assert (
            "ISSUE_ORCHESTRATOR_RUN_DIR belongs to 'some-other-session'"
            in capsys.readouterr().err
        )
        assert not (worktree / COMPLETION_RECORD_PATH).exists()

    def test_a_standalone_invocation_is_never_routed_to_planning(
        self, _dirty, tmp_path, monkeypatch
    ):
        """No managed context, no planning route — whatever is on disk."""
        _git_repo(tmp_path)
        _quick_gate_config(tmp_path)
        stale_run = _managed_run(tmp_path)
        _stage_assignment(
            stale_run,
            TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            focus_issue_number=319,
        )
        monkeypatch.chdir(tmp_path)

        for name in (
            f"{ENV_PREFIX}SESSION_ID",
            "ORCHESTRATOR_SESSION_ID",
            f"{ENV_PREFIX}RUN_DIR",
        ):
            monkeypatch.delenv(name, raising=False)

        with patch(f"{_MODULE}.run_preflight_push_check", return_value=(True, None, None)):
            _complete()

        record = _completion_record(tmp_path)
        assert record["validation_record_path"] is not None
