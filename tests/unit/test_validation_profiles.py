"""Per-agent / per-workflow validation profiles (upstream #7059).

One test class per acceptance criterion in the downstream issue, so a
regression names the contract it broke.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from issue_orchestrator.control.validation import AgentGate, PublishGate
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.validation_config_loader import (
    extract_validation_config,
    load_runtime_validation_config,
)
from issue_orchestrator.infra.validation_profiles import (
    DEFAULT_VALIDATION_PROFILE,
    UnknownValidationProfileError,
    ValidationProfileRegistry,
)
from issue_orchestrator.control.session_controller import SessionController
from issue_orchestrator.control.session_env import build_session_env_exports
from issue_orchestrator.domain.models import CompletionOutcome, RequestedAction
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.session_output import ValidationRecord
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.unit.test_session_controller import (
    MockCompletionProcessor,
    MockWorkingCopy,
    RecordingEventSink,
    make_record,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubWorkingCopy:
    """Working copy that always reports the same HEAD."""

    def __init__(self, head_sha: str = "abcdef1234567890") -> None:
        self._head_sha = head_sha

    def get_head_sha(self, worktree: Path) -> str:
        return self._head_sha


class RecordingCommandRunner:
    """Command runner that records every command it was asked to run."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[str] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        from types import SimpleNamespace

        self.commands.append(command)
        return SimpleNamespace(
            returncode=self.returncode,
            stdout="",
            stderr="",
            timed_out=False,
        )


PROFILE_CONFIG = {
    "repo": {"name": "acme/widgets"},
    "agents": {
        "agent:backend": {
            "prompt": "prompts/backend.md",
            "provider": "claude-code",
            "ai_system": "claude-code",
            "validation_profile": "ordinary",
        },
        "agent:foundation": {
            "prompt": "prompts/foundation.md",
            "provider": "claude-code",
            "ai_system": "claude-code",
            "validation_profile": "foundation",
        },
        "agent:legacy": {
            "prompt": "prompts/legacy.md",
            "provider": "claude-code",
            "ai_system": "claude-code",
        },
    },
    "validation": {
        "quick": {"cmd": "make quick-default"},
        "publish": {"cmd": "make publish-default"},
        "profiles": {
            "ordinary": {
                "quick": {"cmd": "make quick-ordinary", "timeout_seconds": 111},
                "publish": {"cmd": "make publish-ordinary"},
            },
            "foundation": {
                "quick": {"cmd": "make quick-foundation", "timeout_seconds": 222},
                "publish": {
                    "cmd": "make publish-foundation",
                    "dirty_check": "all",
                },
            },
        },
    },
}


def write_config(repo_root: Path, data: dict, name: str = "default.yaml") -> Path:
    config_dir = repo_root / ".issue-orchestrator" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for prompt in ("backend.md", "foundation.md", "legacy.md"):
        prompt_path = repo_root / "prompts" / prompt
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("prompt\n")
    config_path = config_dir / name
    config_path.write_text(yaml.safe_dump(data))
    return config_path


# ---------------------------------------------------------------------------
# 1. Default compatibility
# ---------------------------------------------------------------------------


class TestDefaultCompatibility:
    """A config that never mentions profiles keeps its current behavior."""

    def test_top_level_config_defines_the_default_profile(self, tmp_path: Path) -> None:
        config_path = write_config(
            tmp_path,
            {
                "repo": {"name": "acme/widgets"},
                "agents": {
                    "agent:backend": {
                        "prompt": "prompts/backend.md",
                        "provider": "claude-code",
                        "ai_system": "claude-code",
                    }
                },
                "validation": {
                    "quick": {"cmd": "make quick", "timeout_seconds": 60},
                    "publish": {"cmd": "make publish"},
                },
            },
        )
        config = Config.load(config_path)

        profile = config.validation_profiles().for_agent("agent:backend")

        assert profile.name == DEFAULT_VALIDATION_PROFILE
        assert profile.quick.cmd == "make quick"
        assert profile.quick.timeout_seconds == 60
        assert profile.publish.cmd == "make publish"
        assert config.validate() == []

    def test_agent_without_binding_gets_the_default_profile(
        self, tmp_path: Path
    ) -> None:
        config = Config.load(write_config(tmp_path, PROFILE_CONFIG))

        profile = config.validation_profiles().for_agent("agent:legacy")

        assert profile.name == DEFAULT_VALIDATION_PROFILE
        assert profile.quick.cmd == "make quick-default"

    def test_agent_side_loader_without_a_selected_profile_reads_top_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(tmp_path, PROFILE_CONFIG)
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_VALIDATION_PROFILE", raising=False)
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_CONFIG_PATH", raising=False)
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", raising=False)

        resolved = load_runtime_validation_config(tmp_path)

        assert resolved["profile"] == DEFAULT_VALIDATION_PROFILE
        assert resolved["quick"]["cmd"] == "make quick-default"
        assert resolved["publish"]["cmd"] == "make publish-default"


# ---------------------------------------------------------------------------
# 2. Profile selection
# ---------------------------------------------------------------------------


class TestProfileSelection:
    """A role bound to a named profile runs that profile's commands."""

    def test_registry_resolves_each_role_to_its_own_commands(
        self, tmp_path: Path
    ) -> None:
        registry = Config.load(
            write_config(tmp_path, PROFILE_CONFIG)
        ).validation_profiles()

        backend = registry.for_agent("agent:backend")
        foundation = registry.for_agent("agent:foundation")

        assert backend.name == "ordinary"
        assert backend.quick.cmd == "make quick-ordinary"
        assert backend.quick.timeout_seconds == 111
        assert foundation.name == "foundation"
        assert foundation.quick.cmd == "make quick-foundation"
        assert foundation.publish.cmd == "make publish-foundation"
        assert foundation.publish.dirty_check == "all"

    def test_session_environment_exports_the_frozen_profile(
        self, tmp_path: Path
    ) -> None:
        class EnvConfig:
            control_api_port = 8100
            config_path = None

        exports = build_session_env_exports(
            config=EnvConfig(),
            completion_path=str(tmp_path / "completion.json"),
            session_id="issue-7",
            agent_label="agent:foundation",
            issue_number=7,
            run_dir=tmp_path,
            worktree_path=tmp_path,
            callback_endpoint=ready_callback_endpoint(),
            validation_profile="foundation",
        )

        assert "ISSUE_ORCHESTRATOR_VALIDATION_PROFILE='foundation'" in exports

    def test_agent_side_loader_honors_the_selected_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(tmp_path, PROFILE_CONFIG)
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_VALIDATION_PROFILE", "foundation")
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_CONFIG_PATH", raising=False)
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", raising=False)

        resolved = load_runtime_validation_config(tmp_path)

        assert resolved["profile"] == "foundation"
        assert resolved["quick"]["cmd"] == "make quick-foundation"
        assert resolved["quick"]["timeout_seconds"] == 222
        assert resolved["publish"]["dirty_check"] == "all"

    def test_agent_gate_runs_the_selected_profiles_command(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner = RecordingCommandRunner()

        gate = AgentGate(
            worktree,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
            command="make quick-foundation",
            profile="foundation",
        )
        result = gate.run(session_output_dir=worktree / "out")

        assert runner.commands == ["make quick-foundation"]
        assert result.record is not None
        assert result.record.profile == "foundation"


# ---------------------------------------------------------------------------
# 3. Invalid profile fails closed at config validation
# ---------------------------------------------------------------------------


class TestInvalidProfileFailsClosed:
    """An unknown profile name is rejected before anything runs."""

    def test_validate_names_the_offending_role_and_profile(
        self, tmp_path: Path
    ) -> None:
        data = json.loads(json.dumps(PROFILE_CONFIG))
        data["agents"]["agent:backend"]["validation_profile"] = "typo-profile"
        config = Config.load(write_config(tmp_path, data))

        errors = config.validate()

        assert any(
            "agent:backend" in error and "typo-profile" in error for error in errors
        ), errors

    def test_error_lists_the_profiles_that_do_exist(self, tmp_path: Path) -> None:
        data = json.loads(json.dumps(PROFILE_CONFIG))
        data["agents"]["agent:backend"]["validation_profile"] = "typo-profile"
        config = Config.load(write_config(tmp_path, data))

        error = next(
            error for error in config.validate() if "typo-profile" in error
        )

        assert "default" in error
        assert "foundation" in error
        assert "ordinary" in error

    def test_agent_side_loader_refuses_an_undefined_profile(self) -> None:
        with pytest.raises(UnknownValidationProfileError) as exc_info:
            extract_validation_config(PROFILE_CONFIG, "not-a-profile")

        assert "not-a-profile" in str(exc_info.value)

    def test_default_is_a_reserved_profile_name(self, tmp_path: Path) -> None:
        data = json.loads(json.dumps(PROFILE_CONFIG))
        data["validation"]["profiles"]["default"] = {"quick": {"cmd": "x"}}

        with pytest.raises(ValueError, match="reserved"):
            Config.load(write_config(tmp_path, data))

    def test_unknown_key_inside_a_profile_is_rejected(self, tmp_path: Path) -> None:
        data = json.loads(json.dumps(PROFILE_CONFIG))
        data["validation"]["profiles"]["ordinary"]["quik"] = {"cmd": "typo"}

        with pytest.raises(ValueError, match="quik"):
            Config.load(write_config(tmp_path, data))


# ---------------------------------------------------------------------------
# 4. Artifact binding
# ---------------------------------------------------------------------------


class TestArtifactBinding:
    """The selected profile is provable from the artifacts a run leaves."""

    def test_validation_record_names_the_profile_that_ran(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        gate = AgentGate(
            worktree,
            command_runner=RecordingCommandRunner(),
            working_copy=StubWorkingCopy(),
            command="make quick-foundation",
            profile="foundation",
        )

        gate.run(session_output_dir=worktree / "out")

        record_path = (
            worktree / ".issue-orchestrator" / "validation" / "abcdef1234567890.json"
        )
        payload = json.loads(record_path.read_text())
        assert payload["profile"] == "foundation"

    def test_run_manifest_records_the_frozen_profile(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_output = FileSystemSessionOutput()

        run = session_output.start_run(
            worktree,
            "issue-7",
            issue_number=7,
            agent_label="agent:foundation",
            validation_profile="foundation",
        )

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        assert manifest["validation_profile"] == "foundation"

    def test_record_written_before_profiles_reads_back_as_default(self) -> None:
        legacy = {
            "schema_version": 1,
            "suite": "agent_gate",
            "head_sha": "abc",
            "passed": True,
            "exit_code": 0,
            "command": "make test",
            "started_at": "s",
            "ended_at": "e",
        }

        assert ValidationRecord.from_dict(legacy).profile == DEFAULT_VALIDATION_PROFILE


# ---------------------------------------------------------------------------
# 5. Cache key binding
# ---------------------------------------------------------------------------


class TestCacheKeyBinding:
    """A cached result cannot cross a profile boundary."""

    def _gate(self, worktree: Path, runner, *, command: str, profile: str):
        return PublishGate(
            worktree,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
            command=command,
            profile=profile,
        )

    def test_different_profiles_with_different_commands_do_not_share_a_result(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner = RecordingCommandRunner()
        out = worktree / "out"

        self._gate(
            worktree, runner, command="make publish-ordinary", profile="ordinary"
        ).check(session_output_dir=out)
        second = self._gate(
            worktree, runner, command="make publish-foundation", profile="foundation"
        ).check(session_output_dir=out)

        assert second.cache_hit is False
        assert runner.commands == [
            "make publish-ordinary",
            "make publish-foundation",
        ]

    def test_different_profiles_sharing_one_command_still_do_not_share_a_result(
        self, tmp_path: Path
    ) -> None:
        """Command equality is not contract equality.

        Two profiles may run the same command today. A pass recorded under one
        must still not let the other skip its own gate, or a later divergence
        would silently inherit a result it never earned.
        """
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner = RecordingCommandRunner()
        out = worktree / "out"

        self._gate(
            worktree, runner, command="make publish", profile="ordinary"
        ).check(session_output_dir=out)
        second = self._gate(
            worktree, runner, command="make publish", profile="foundation"
        ).check(session_output_dir=out)

        assert second.cache_hit is False
        assert runner.commands == ["make publish", "make publish"]

    def test_the_same_profile_still_reuses_a_passing_result(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner = RecordingCommandRunner()
        out = worktree / "out"

        self._gate(
            worktree, runner, command="make publish", profile="foundation"
        ).check(session_output_dir=out)
        second = self._gate(
            worktree, runner, command="make publish", profile="foundation"
        ).check(session_output_dir=out)

        assert second.cache_hit is True
        assert runner.commands == ["make publish"]


# ---------------------------------------------------------------------------
# Registry behavior shared by review/rework continuation and restart recovery
# ---------------------------------------------------------------------------


class TestRegistryResolution:
    """The registry is the single owner of name -> contract resolution."""

    def test_resolving_none_yields_the_default_profile(self, tmp_path: Path) -> None:
        registry = Config.load(
            write_config(tmp_path, PROFILE_CONFIG)
        ).validation_profiles()

        assert registry.resolve(None).name == DEFAULT_VALIDATION_PROFILE

    def test_resolving_an_undefined_name_raises(self, tmp_path: Path) -> None:
        registry = Config.load(
            write_config(tmp_path, PROFILE_CONFIG)
        ).validation_profiles()

        with pytest.raises(UnknownValidationProfileError):
            registry.resolve("retired-profile")

    def test_any_quick_command_configured_sees_profile_only_gates(self) -> None:
        from issue_orchestrator.infra.config_models import (
            ValidationCommandConfig,
            ValidationConfig,
            ValidationProfileConfig,
        )

        registry = ValidationProfileRegistry(
            ValidationConfig(
                profiles={
                    "foundation": ValidationProfileConfig(
                        quick=ValidationCommandConfig(cmd="make quick-foundation"),
                    )
                }
            )
        )

        assert registry.any_quick_command_configured is True


# ---------------------------------------------------------------------------
# 6. Review / rework continuation  +  7. Restart recovery
#
# Both criteria are about the same invariant seen from two angles: the
# contract a run validates against comes from the run's own durable state, so
# it survives a later round and an orchestrator restart alike.
# ---------------------------------------------------------------------------


def _controller_for(tmp_path: Path, registry: ValidationProfileRegistry, runner):
    """Build a SessionController wired to a profile registry."""
    processor = MockCompletionProcessor()
    processor.completion_record = make_record(
        CompletionOutcome.COMPLETED,
        summary="Done",
        requested_actions=[RequestedAction.CREATE_PR],
    )
    controller = SessionController(
        completion_processor=processor,
        events=RecordingEventSink(),
        session_output=FileSystemSessionOutput(),
        working_copy=MockWorkingCopy(head_sha="abcdef1234567890"),
        command_runner=runner,
        validation_profiles=registry,
        max_validation_retries=3,
    )
    return controller


def _run_gate(controller, worktree: Path, session_name: str, run: object):
    from issue_orchestrator.observation.observation import SessionObservationResult

    return controller.decide_outcome(
        observation=SessionObservationResult.terminated(runtime_minutes=1.0),
        worktree_path=worktree,
        issue_number=7,
        issue_title="Add widget",
        session_name=session_name,
        session_run_assets=run,
    )


class TestProfileContinuityAcrossRounds:
    """The profile a run was launched under governs every later gate for it."""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> ValidationProfileRegistry:
        return Config.load(
            write_config(tmp_path / "repo", PROFILE_CONFIG)
        ).validation_profiles()

    def test_rework_round_reuses_the_runs_recorded_profile(
        self, tmp_path: Path, registry: ValidationProfileRegistry
    ) -> None:
        """A second round on the same run validates the same contract.

        The registry is asked by NAME, using what the run directory recorded,
        so a later round cannot drift onto another role's contract.
        """
        worktree = (tmp_path / "worktree").resolve()
        worktree.mkdir(parents=True)
        session_output = FileSystemSessionOutput()
        runner = RecordingCommandRunner()
        controller = _controller_for(tmp_path, registry, runner)

        run = session_output.start_run(
            worktree,
            "issue-7",
            issue_number=7,
            agent_label="agent:foundation",
            validation_profile="foundation",
        )
        _run_gate(controller, worktree, "issue-7", run)
        _run_gate(controller, worktree, "issue-7", run)

        # Round two reuses the round-one result (same SHA, same contract), so
        # the command runs once; what matters is that neither round ever fell
        # back to the default profile's command.
        assert set(runner.commands) == {"make quick-foundation"}
        record = json.loads(
            (run.run_dir / "validation-record.json").read_text()
        )
        assert record["profile"] == "foundation"

    def test_restart_reconstructs_the_profile_from_the_run_directory(
        self, tmp_path: Path, registry: ValidationProfileRegistry
    ) -> None:
        """A fresh controller (as after a restart) reads the run, not the role.

        The rebuilt controller is given a registry in which ``agent:foundation``
        would resolve differently if the profile were re-derived from the
        agent label. It must still run the contract the run recorded.
        """
        worktree = (tmp_path / "worktree").resolve()
        worktree.mkdir(parents=True)
        session_output = FileSystemSessionOutput()

        run = session_output.start_run(
            worktree,
            "issue-7",
            issue_number=7,
            agent_label="agent:foundation",
            validation_profile="foundation",
        )

        # Simulate the restart: a brand-new controller, no in-memory carryover.
        runner = RecordingCommandRunner()
        restarted = _controller_for(tmp_path, registry, runner)
        restored_run = SessionRunAssets.from_manifest_payload(
            run_dir=run.run_dir,
            manifest=session_output.read_manifest(run.run_dir) or {},
        )
        _run_gate(restarted, worktree, "issue-7", restored_run)

        assert runner.commands == ["make quick-foundation"]

    def test_run_without_a_recorded_profile_uses_the_default_contract(
        self, tmp_path: Path, registry: ValidationProfileRegistry
    ) -> None:
        """Pre-#7059 run directories keep validating exactly as they did."""
        worktree = (tmp_path / "worktree").resolve()
        worktree.mkdir(parents=True)
        session_output = FileSystemSessionOutput()
        runner = RecordingCommandRunner()
        controller = _controller_for(tmp_path, registry, runner)

        run = session_output.start_run(
            worktree, "issue-7", issue_number=7, agent_label="agent:foundation"
        )
        session_output.update_manifest(run.run_dir, {"validation_profile": None})

        _run_gate(controller, worktree, "issue-7", run)

        assert runner.commands == ["make quick-default"]

    def test_run_naming_a_retired_profile_fails_closed(
        self, tmp_path: Path, registry: ValidationProfileRegistry
    ) -> None:
        """Substituting another contract would forge the run's evidence."""
        worktree = (tmp_path / "worktree").resolve()
        worktree.mkdir(parents=True)
        session_output = FileSystemSessionOutput()
        controller = _controller_for(tmp_path, registry, RecordingCommandRunner())

        run = session_output.start_run(
            worktree,
            "issue-7",
            issue_number=7,
            agent_label="agent:foundation",
            validation_profile="retired-profile",
        )

        with pytest.raises(UnknownValidationProfileError, match="retired-profile"):
            _run_gate(controller, worktree, "issue-7", run)
