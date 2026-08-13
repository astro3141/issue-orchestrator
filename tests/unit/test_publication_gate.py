"""The publish gate executes ``validation.publish``, and says so (#25).

The defect: the orchestrator's post-completion gate ran the profile's *quick*
command while ``PublishGate`` stamped ``suite=publish_gate`` onto the record,
and the completion processor's publish-gate seam was never wired at all — so
``validation.publish.cmd`` was executed nowhere in the orchestrator path while
records claimed it had been. The suite label proved nothing.

These tests pin the four values that must agree — requested gate, resolved
contract, executed command, persisted record — plus the cache door the same
conflation could come back through.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.publication_gate import (
    PublicationGate,
    RunValidationContracts,
    publish_gate_output_dir,
)
from issue_orchestrator.control.validation import ValidationGate
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
    ValidationProfileConfig,
)
from issue_orchestrator.infra.validation_profiles import (
    ValidationProfile,
    ValidationProfileRegistry,
)
from tests.validation_contract_helpers import publish_contract, quick_contract

QUICK_SENTINEL = "run-the-quick-contract"
PUBLISH_SENTINEL = "run-the-publish-contract"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


class RecordingCommandRunner:
    """Records every command it was asked to run."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[str] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.commands.append(command)
        return SimpleNamespace(
            returncode=self.returncode, stdout="", stderr="", timed_out=False
        )


class StubWorkingCopy:
    def __init__(self, head_sha: str = HEAD_SHA) -> None:
        self._head_sha = head_sha

    def get_head_sha(self, worktree: Path) -> str:
        return self._head_sha


def sentinel_registry(profile_name: str = "default") -> ValidationProfileRegistry:
    """A registry whose quick and publish contracts are distinguishable."""
    quick = ValidationCommandConfig(cmd=QUICK_SENTINEL, timeout_seconds=111)
    publish = PublishValidationConfig(cmd=PUBLISH_SENTINEL, timeout_seconds=222)
    if profile_name == "default":
        return ValidationProfileRegistry(
            ValidationConfig(quick=quick, publish=publish)
        )
    return ValidationProfileRegistry(
        ValidationConfig(
            profiles={
                profile_name: ValidationProfileConfig(quick=quick, publish=publish)
            }
        )
    )


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


def start_run(worktree: Path, *, profile: str = "default"):
    return FileSystemSessionOutput().start_run(
        worktree,
        "issue-25",
        issue_number=25,
        validation_profile=profile,
    )


class TestThePublishGateRunsThePublishContract:
    """One test, four values, pinned together.

    Split across separate tests these can drift back into agreeing names and
    disagreeing commands, which is exactly the shape of the original defect.
    """

    def test_requested_gate_contract_command_and_record_all_say_publish(
        self, worktree: Path
    ) -> None:
        session_output = FileSystemSessionOutput()
        registry = sentinel_registry()
        run = start_run(worktree)
        runner = RecordingCommandRunner()
        contracts = RunValidationContracts(session_output, registry)
        gate = PublicationGate(
            contracts=contracts,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        )

        # A stale QUICK_SENTINEL result for this exact HEAD and profile must
        # not satisfy the publish request. Seeded first, so a cache hit would
        # short-circuit the run and leave `runner.commands` empty.
        quick_gate = ValidationGate(
            worktree=worktree,
            command_runner=RecordingCommandRunner(),
            working_copy=StubWorkingCopy(),
            contract=quick_contract(cmd=QUICK_SENTINEL, timeout_seconds=111),
        )
        quick_result = quick_gate.check(session_output_dir=run.run_dir)
        assert quick_result.allowed is True
        assert quick_result.record is not None
        assert quick_result.record.suite == "quick_gate"

        result = gate.check(worktree=worktree, run_assets=run)

        # 1. The requested gate kind resolves the publish contract...
        contract = contracts.contract_for_run(
            run.run_dir, ValidationGateKind.PUBLISH
        )
        assert contract.kind is ValidationGateKind.PUBLISH
        assert contract.cmd == PUBLISH_SENTINEL
        assert contract.timeout_seconds == 222
        assert contract.suite == "publish_gate"

        # 2. ...and only the publish command was executed.
        assert runner.commands == [PUBLISH_SENTINEL]
        assert QUICK_SENTINEL not in runner.commands

        # 3. The cached quick result was a MISS, not a hit.
        assert result.cache_hit is False
        assert result.allowed is True

        # 4. The persisted record carries the exact publish command.
        assert result.record is not None
        assert result.record.suite == "publish_gate"
        assert result.record.command == PUBLISH_SENTINEL
        assert result.record.head_sha == HEAD_SHA

        persisted = json.loads(
            (publish_gate_output_dir(run.run_dir) / "validation-record.json").read_text()
        )
        assert persisted["suite"] == "publish_gate"
        assert persisted["command"] == PUBLISH_SENTINEL

    def test_substituting_the_quick_contract_at_the_seam_fails_this_test(
        self, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falsification: the mutation the defect *was* must break the pin.

        Rather than asserting a command string somewhere, this replaces the
        publish contract resolution with the quick one — the exact mutation
        that produced the original bug — and requires the pinning assertions
        above to reject it. A test that survives this has pinned nothing.
        """
        original = ValidationProfile.contract

        def always_quick(self: ValidationProfile, kind: ValidationGateKind):
            return original(self, ValidationGateKind.QUICK)

        monkeypatch.setattr(ValidationProfile, "contract", always_quick)

        session_output = FileSystemSessionOutput()
        run = start_run(worktree)
        runner = RecordingCommandRunner()
        gate = PublicationGate(
            contracts=RunValidationContracts(session_output, sentinel_registry()),
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        )

        result = gate.check(worktree=worktree, run_assets=run)

        # The mutation is observable in every one of the pinned values.
        assert runner.commands == [QUICK_SENTINEL]
        assert result.record is not None
        assert result.record.command == QUICK_SENTINEL
        assert result.record.suite != "publish_gate"


class TestTheQuickPathStillRunsTheQuickContract:
    """The quick gate must not start running the publish contract instead."""

    def test_quick_gate_executes_the_quick_sentinel_only(self, worktree: Path) -> None:
        runner = RecordingCommandRunner()
        gate = ValidationGate(
            worktree=worktree,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
            contract=quick_contract(cmd=QUICK_SENTINEL, timeout_seconds=111),
        )

        result = gate.check(session_output_dir=worktree / "out")

        assert runner.commands == [QUICK_SENTINEL]
        assert PUBLISH_SENTINEL not in runner.commands
        assert result.record is not None
        assert result.record.suite == "quick_gate"
        assert result.record.command == QUICK_SENTINEL

    def test_a_quick_record_never_claims_the_publish_suite(
        self, worktree: Path
    ) -> None:
        gate = ValidationGate(
            worktree=worktree,
            command_runner=RecordingCommandRunner(),
            working_copy=StubWorkingCopy(),
            contract=quick_contract(cmd=QUICK_SENTINEL),
        )

        gate.check(session_output_dir=worktree / "out")

        stored = json.loads(
            (
                worktree
                / ".issue-orchestrator"
                / "validation"
                / "quick"
                / f"{HEAD_SHA}.json"
            ).read_text()
        )
        assert stored["suite"] == "quick_gate"
        assert stored["command"] == QUICK_SENTINEL


class TestCacheIdentityIsTheContract:
    """A quick result may never satisfy a publish request, and vice versa."""

    def _gate(self, worktree: Path, runner, contract):
        return ValidationGate(
            worktree=worktree,
            command_runner=runner,
            working_copy=StubWorkingCopy(),
            contract=contract,
        )

    def test_cached_quick_pass_does_not_satisfy_the_publish_gate(
        self, worktree: Path
    ) -> None:
        quick_runner = RecordingCommandRunner()
        self._gate(
            worktree, quick_runner, quick_contract(cmd=QUICK_SENTINEL)
        ).check(session_output_dir=worktree / "out")
        assert quick_runner.commands == [QUICK_SENTINEL]

        publish_runner = RecordingCommandRunner()
        result = self._gate(
            worktree, publish_runner, publish_contract(cmd=PUBLISH_SENTINEL)
        ).check(session_output_dir=worktree / "out")

        assert result.cache_hit is False
        assert publish_runner.commands == [PUBLISH_SENTINEL]

    def test_a_publish_pass_is_reused_for_the_same_head_command_and_profile(
        self, worktree: Path
    ) -> None:
        first = RecordingCommandRunner()
        self._gate(
            worktree, first, publish_contract(cmd=PUBLISH_SENTINEL)
        ).check(session_output_dir=worktree / "out")

        second = RecordingCommandRunner()
        result = self._gate(
            worktree, second, publish_contract(cmd=PUBLISH_SENTINEL)
        ).check(session_output_dir=worktree / "out")

        assert result.cache_hit is True
        assert second.commands == []

    def test_a_publish_pass_from_another_profile_is_not_reused(
        self, worktree: Path
    ) -> None:
        first = RecordingCommandRunner()
        self._gate(
            worktree,
            first,
            publish_contract(cmd=PUBLISH_SENTINEL, profile="foundation"),
        ).check(session_output_dir=worktree / "out")

        second = RecordingCommandRunner()
        result = self._gate(
            worktree, second, publish_contract(cmd=PUBLISH_SENTINEL)
        ).check(session_output_dir=worktree / "out")

        assert result.cache_hit is False
        assert second.commands == [PUBLISH_SENTINEL]

    def test_the_quick_gate_does_not_overwrite_the_publish_record(
        self, worktree: Path
    ) -> None:
        """Storage identity is the contract, not the SHA.

        One file per SHA let the quick gate — which runs later in the same
        tick — destroy the only evidence of what the publish contract did.
        """
        self._gate(
            worktree,
            RecordingCommandRunner(),
            publish_contract(cmd=PUBLISH_SENTINEL),
        ).check(session_output_dir=worktree / "out")
        self._gate(
            worktree, RecordingCommandRunner(), quick_contract(cmd=QUICK_SENTINEL)
        ).check(session_output_dir=worktree / "out")

        base = worktree / ".issue-orchestrator" / "validation"
        publish_record = json.loads((base / "publish" / f"{HEAD_SHA}.json").read_text())
        quick_record = json.loads((base / "quick" / f"{HEAD_SHA}.json").read_text())

        assert publish_record["command"] == PUBLISH_SENTINEL
        assert publish_record["suite"] == "publish_gate"
        assert quick_record["command"] == QUICK_SENTINEL
        assert quick_record["suite"] == "quick_gate"


class TestTheRunsFrozenProfileSelectsTheContract:
    """The publish contract comes from the run, not from composition time."""

    def test_named_profile_publish_command_is_the_one_executed(
        self, worktree: Path
    ) -> None:
        session_output = FileSystemSessionOutput()
        registry = ValidationProfileRegistry(
            ValidationConfig(
                quick=ValidationCommandConfig(cmd="default-quick"),
                publish=PublishValidationConfig(cmd="default-publish"),
                profiles={
                    "foundation": ValidationProfileConfig(
                        quick=ValidationCommandConfig(cmd=QUICK_SENTINEL),
                        publish=PublishValidationConfig(cmd=PUBLISH_SENTINEL),
                    )
                },
            )
        )
        run = start_run(worktree, profile="foundation")
        runner = RecordingCommandRunner()

        result = PublicationGate(
            contracts=RunValidationContracts(session_output, registry),
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        ).check(worktree=worktree, run_assets=run)

        assert runner.commands == [PUBLISH_SENTINEL]
        assert result.record is not None
        assert result.record.profile == "foundation"
        assert result.record.suite == "publish_gate"

    def test_a_failing_publish_contract_refuses_publication(
        self, worktree: Path
    ) -> None:
        session_output = FileSystemSessionOutput()
        run = start_run(worktree)
        runner = RecordingCommandRunner(returncode=1)

        result = PublicationGate(
            contracts=RunValidationContracts(session_output, sentinel_registry()),
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        ).check(worktree=worktree, run_assets=run)

        assert runner.commands == [PUBLISH_SENTINEL]
        assert result.allowed is False
        assert result.record is not None
        assert result.record.passed is False

    def test_an_unconfigured_publish_contract_runs_nothing(
        self, worktree: Path
    ) -> None:
        session_output = FileSystemSessionOutput()
        run = start_run(worktree)
        runner = RecordingCommandRunner()
        registry = ValidationProfileRegistry(
            ValidationConfig(
                quick=ValidationCommandConfig(cmd=QUICK_SENTINEL),
                publish=PublishValidationConfig(cmd=None),
            )
        )

        result = PublicationGate(
            contracts=RunValidationContracts(session_output, registry),
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        ).check(worktree=worktree, run_assets=run)

        assert runner.commands == []
        assert result.allowed is True
        assert result.record is None


class TestTheGateReportsWhereItsEvidenceLives:
    """Where the gate wrote and what a caller attaches are one answer (#25).

    ``PublicationGate`` writes into ``publish-gate/`` so the quick gate
    cannot overwrite it. That isolation is worth nothing if the caller
    attaching the result names the paths itself — it named the run root's,
    and the manifest ended up pairing the publish gate's record with the
    quick gate's stdout and stderr.
    """

    def _gate(self, worktree: Path, runner, registry=None):
        return PublicationGate(
            contracts=RunValidationContracts(
                FileSystemSessionOutput(), registry or sentinel_registry()
            ),
            command_runner=runner,
            working_copy=StubWorkingCopy(),
        )

    def test_evidence_paths_are_the_files_the_gate_actually_wrote(
        self, worktree: Path
    ) -> None:
        run = start_run(worktree)

        outcome = self._gate(worktree, RecordingCommandRunner()).check(
            worktree=worktree, run_assets=run
        )

        publish_dir = publish_gate_output_dir(run.run_dir)
        assert outcome.evidence.paths.record_path == publish_dir / "validation-record.json"
        assert outcome.evidence.paths.stdout_path == publish_dir / "validation-stdout.log"
        assert outcome.evidence.paths.stderr_path == publish_dir / "validation-stderr.log"
        # Not a claim about intent — the files are there.
        assert outcome.evidence.paths.record_path.exists()
        assert outcome.evidence.paths.stdout_path.exists()
        assert outcome.evidence.paths.stderr_path.exists()

    def test_evidence_is_never_the_run_roots_quick_gate_paths(
        self, worktree: Path
    ) -> None:
        run = start_run(worktree)

        outcome = self._gate(worktree, RecordingCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=run
        )

        quick = run.validation_artifacts
        assert outcome.evidence.paths.stdout_path != quick.stdout_path
        assert outcome.evidence.paths.stderr_path != quick.stderr_path
        assert outcome.evidence.paths.record_path != quick.record_path
        # The manifest still hangs off the run, not off the subdirectory.
        assert outcome.evidence.paths.run_dir == run.run_dir

    def test_evidence_accompanies_a_refusal_too(self, worktree: Path) -> None:
        """A failing gate is exactly when someone reads the logs."""
        run = start_run(worktree)

        outcome = self._gate(worktree, RecordingCommandRunner(returncode=1)).check(
            worktree=worktree, run_assets=run
        )

        assert outcome.allowed is False
        assert outcome.evidence.paths.stdout_path.exists()
        recorded = json.loads(outcome.evidence.paths.record_path.read_text())
        assert recorded["command"] == PUBLISH_SENTINEL
        assert recorded["passed"] is False


class TestCompositionActuallyBuildsTheGate:
    """The seam existed for a long time; nothing ever built one (#25).

    ``CompletionProcessor`` has accepted a publish gate since well before this
    issue, and the pre-publish check honoured it — but the composition root
    never constructed one, so in production the gate was ``None`` and
    ``validation.publish.cmd`` ran nowhere. A test on the gate alone would
    have stayed green through the entire defect.
    """

    def _components(self, tmp_path: Path, publish_cmd: str | None):
        from unittest.mock import MagicMock

        import yaml

        from issue_orchestrator.entrypoints.bootstrap_completion import (
            create_completion_components,
        )
        from issue_orchestrator.execution.command_runner import LocalCommandRunner
        from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.ports import NullEventSink

        prompt = tmp_path / "prompts" / "backend.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("prompt\n")
        validation: dict[str, object] = {"quick": {"cmd": QUICK_SENTINEL}}
        if publish_cmd is not None:
            validation["publish"] = {"cmd": publish_cmd}
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "repo": {"name": "acme/widgets"},
                    "agents": {
                        "agent:backend": {
                            "prompt": "prompts/backend.md",
                            "provider": "claude-code",
                            "ai_system": "claude-code",
                        }
                    },
                    "validation": validation,
                }
            )
        )
        processor, _controller, _factory = create_completion_components(
            Config.load(config_path),
            MagicMock(),
            NullEventSink(),
            GitWorkingCopy(),
            FileSystemSessionOutput(),
            LocalCommandRunner(),
            agent_callback_endpoint=MagicMock(),
            needs_human_block=MagicMock(),
        )
        return processor

    def test_a_configured_publish_command_yields_a_real_gate(
        self, tmp_path: Path
    ) -> None:
        processor = self._components(tmp_path, PUBLISH_SENTINEL)

        assert processor is not None
        assert isinstance(processor.publication_gate, PublicationGate)

    def test_the_gate_is_built_even_with_no_publish_command_configured(
        self, tmp_path: Path
    ) -> None:
        """Whether a run has a publish contract is the gate's question.

        Deciding it at composition time — from whether any profile happens to
        configure one — would be a second place answering it, and a second
        place that can answer it differently.
        """
        processor = self._components(tmp_path, None)

        assert processor is not None
        assert isinstance(processor.publication_gate, PublicationGate)


# Which completions the publish contract applies to — a blocked or
# needs-human push offers no change and is not held to it — is pinned through
# the public completion path in
# ``tests/unit/test_completion_processor.py::TestCompletionProcessorPublishGate``.
