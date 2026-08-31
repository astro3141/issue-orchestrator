"""The composition roots' wiring of the review-exchange runner (#399).

The staging owner that freezes an exchange's admitted leaf contract is
injected by a composition root and exercised only by a live exchange, so
every other test in the suite supplies its own. That leaves the
production wiring itself uncovered — delete it and the runner silently
falls back to the refusing default, killing every review exchange, with
a green suite the whole way.

This repository has shipped exactly that defect before:
``tests/unit/test_publication_gate.py::TestCompositionActuallyBuildsTheGate``
was written because ``CompletionProcessor`` honoured a publish gate the
composition root never constructed. These tests are that precedent
applied to the contract staging.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from issue_orchestrator.entrypoints import bootstrap_completion
from issue_orchestrator.entrypoints.bootstrap import create_attempt_store
from issue_orchestrator.entrypoints.bootstrap_completion import (
    build_review_exchange_runner,
    create_completion_components,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.persistent_review_exchange_runner import (
    PersistentReviewExchangeRunner,
)
from issue_orchestrator.execution.review_exchange_leaf_contract import (
    IssueTrackerLeafContractStaging,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.control.publication_authority import UnrecordedRefusals
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.review_exchange_leaf_contract import (
    UNSTAGEABLE_ADMITTED_LEAF_CONTRACT,
)


def _config(tmp_path: Path) -> Config:
    prompt = tmp_path / "prompts" / "backend.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("prompt\n")
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
                "validation": {"quick": {"cmd": "true"}},
            }
        )
    )
    return Config.load(config_path)


def _runner_built_by_the_composition_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    repository_host: Any,
) -> PersistentReviewExchangeRunner:
    """The exact runner ``create_completion_components`` handed the pipeline.

    The factory runs for real; the spy only records what came back, so
    this observes production wiring rather than a stand-in for it.
    """
    built: list[PersistentReviewExchangeRunner] = []
    real_factory = bootstrap_completion.build_review_exchange_runner

    def _spy(**kwargs: Any) -> PersistentReviewExchangeRunner:
        runner = real_factory(**kwargs)
        built.append(runner)
        return runner

    monkeypatch.setattr(bootstrap_completion, "build_review_exchange_runner", _spy)

    config = _config(tmp_path)
    processor, _controller, _factory = create_completion_components(
        config,
        MagicMock(name="github"),
        NullEventSink(),
        GitWorkingCopy(),
        FileSystemSessionOutput(),
        LocalCommandRunner(),
        repository_host=repository_host,
        agent_callback_endpoint=MagicMock(name="agent_callback_endpoint"),
        attempt_store=create_attempt_store(config),
        needs_human_block=MagicMock(name="needs_human_block"),
        unrecorded_refusals=UnrecordedRefusals.process_local(),
    )

    assert processor is not None
    assert len(built) == 1, "the completion pipeline builds exactly one runner"
    return built[0]


class TestTheFactoryChoosesTheStagingOwner:
    def test_a_tracker_yields_the_real_staging_owner(self) -> None:
        runner = build_review_exchange_runner(
            session_output=FileSystemSessionOutput(),
            pair_registry=MagicMock(name="pair_registry"),
            attempt_store=MagicMock(name="attempt_store"),
            tech_lead_completion_validator=MagicMock(name="validator"),
            repository_host=MagicMock(name="repository_host"),
        )

        assert isinstance(runner.leaf_contract_staging, IssueTrackerLeafContractStaging)

    def test_no_tracker_keeps_the_refusing_default(self) -> None:
        # There is nothing to read the admitted contract from, and the
        # answer to that is to lose the exchange — never to review
        # against an approximation.
        runner = build_review_exchange_runner(
            session_output=FileSystemSessionOutput(),
            pair_registry=MagicMock(name="pair_registry"),
            attempt_store=MagicMock(name="attempt_store"),
            tech_lead_completion_validator=MagicMock(name="validator"),
            repository_host=None,
        )

        assert runner.leaf_contract_staging is UNSTAGEABLE_ADMITTED_LEAF_CONTRACT


class TestTheCompositionRootWiresTheStagingOwner:
    def test_a_repository_host_reaches_the_runner_as_real_staging(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = _runner_built_by_the_composition_root(
            monkeypatch,
            tmp_path,
            repository_host=MagicMock(name="repository_host"),
        )

        assert isinstance(runner.leaf_contract_staging, IssueTrackerLeafContractStaging)

    def test_without_a_repository_host_the_root_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = _runner_built_by_the_composition_root(
            monkeypatch,
            tmp_path,
            repository_host=None,
        )

        assert runner.leaf_contract_staging is UNSTAGEABLE_ADMITTED_LEAF_CONTRACT
