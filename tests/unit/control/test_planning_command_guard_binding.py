"""Which launches get the planning command guard, and what happens if it fails.

#289 requires the (flavor, provider) pair to be decided by ONE owner — the
ADR-0031 launch owner, which is the only place that already knows both — and
requires that pair to come from the launch authority and the loaded agent
block, never from a label, an issue title or a session name.

It also requires the failure direction to be a *launch* failure: a Codex
planning session whose guard cannot be established or verified must not spawn,
and the reason must be typed clearly enough to tell guard setup apart from a
model or task failure.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from issue_orchestrator.control.tech_lead_session_policy import (
    establish_planning_command_guard,
    resolve_tech_lead_provider,
)
from issue_orchestrator.domain.artifact_contracts import AgentProvider
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.ports.planning_command_guard import (
    PlanningCommandGuardError,
)
from tests.planning_command_guard_fakes import (
    FailingPlanningCommandGuard,
    RecordingPlanningCommandGuard,
)


def _config(tmp_path: Path, provider: str | None):
    from issue_orchestrator.domain.models import AgentConfig
    from issue_orchestrator.infra.config import Config

    config = Config(repo="owner/repo")
    config.tech_lead_review_agent = "agent:tech-lead"
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir(parents=True, exist_ok=True)
    config.agents["agent:tech-lead"] = AgentConfig(
        prompt_path=tmp_path / "prompt.md", provider=provider
    )
    return config


def _ctx(tmp_path: Path, manifest: dict | None = None):
    worktree = tmp_path / "product-tech-lead-289-abc123"
    worktree.mkdir(parents=True, exist_ok=True)
    entries = manifest if manifest is not None else {}
    return SimpleNamespace(
        run=SimpleNamespace(
            run_dir=worktree / "run", run_id="run-1", session_name="issue-289"
        ),
        worktree_path=worktree,
        update_manifest=entries.update,
    )


def _issue(**overrides):
    base = {
        "number": 289,
        "title": "P0 prerequisite: enforce planning_investigation gate refusal",
        "agent_type": "agent:tech-lead",
        "labels": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _establish(tmp_path: Path, *, flavor, provider, guard, manifest=None):
    return establish_planning_command_guard(
        config=cast("object", _config(tmp_path, provider)),
        issue=cast("object", _issue()),
        ctx=cast("object", _ctx(tmp_path, manifest)),
        flavor=flavor,
        planning_command_guard=guard,
    )


class TestFlavorAndProviderBinding:
    def test_a_codex_planning_launch_is_bound_to_the_guard(
        self, tmp_path: Path
    ) -> None:
        guard = RecordingPlanningCommandGuard()

        result = _establish(
            tmp_path,
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            provider="codex",
            guard=guard,
        )

        assert result is not None and result.enforced is True
        assert [provider for _path, provider in guard.calls] == ["codex"]

    @pytest.mark.parametrize(
        "flavor",
        [
            TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            TechLeadSessionFlavor.BATCH_REVIEW,
            TechLeadSessionFlavor.HEALTH_REVIEW,
        ],
    )
    def test_no_other_flavor_is_given_a_barrier_this_leaf_did_not_measure(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor
    ) -> None:
        guard = RecordingPlanningCommandGuard()

        assert _establish(tmp_path, flavor=flavor, provider="codex", guard=guard) is None
        assert guard.calls == []

    def test_the_provider_comes_from_the_agent_block_not_the_issue(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path, "codex")
        # A title and labels that suggest something else entirely.
        issue = _issue(title="codex reviewer batch", labels=["agent:web", "codex"])

        assert resolve_tech_lead_provider(
            cast("object", config), cast("object", issue)
        ) == AgentProvider("codex")

    def test_an_agent_with_a_custom_command_names_no_provider(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path, None)

        assert (
            resolve_tech_lead_provider(cast("object", config), cast("object", _issue()))
            is None
        )


class TestFailClosedBeforeSpawn:
    def test_a_codex_planning_launch_fails_when_the_guard_cannot_be_established(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PlanningCommandGuardError, match="execpolicy did not refuse"):
            _establish(
                tmp_path,
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                provider="codex",
                guard=FailingPlanningCommandGuard(),
            )

    def test_a_codex_planning_launch_fails_when_the_guard_is_not_enforcing(
        self, tmp_path: Path
    ) -> None:
        # The decorative-guard direction: an outcome that reports no verified
        # refusal must not be accepted as a barrier.
        with pytest.raises(PlanningCommandGuardError, match="unguarded"):
            _establish(
                tmp_path,
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                provider="codex",
                guard=RecordingPlanningCommandGuard(enforce=False),
            )

    def test_a_provider_with_no_mechanism_launches_and_is_reported_unguarded(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = RecordingPlanningCommandGuard(enforce=False)

        with caplog.at_level("WARNING"):
            result = _establish(
                tmp_path,
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                provider="claude-code",
                guard=guard,
            )

        assert result is not None and result.enforced is False
        assert "UNGUARDED" in caplog.text

    def test_an_unresolvable_provider_launches_and_is_reported_unguarded(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = RecordingPlanningCommandGuard()

        with caplog.at_level("WARNING"):
            result = _establish(
                tmp_path,
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                provider=None,
                guard=guard,
            )

        assert result is None
        assert guard.calls == []
        assert "names no provider" in caplog.text


class TestTheGuardIsRecordedAsAFact:
    def test_the_run_manifest_keeps_what_was_verified(self, tmp_path: Path) -> None:
        manifest: dict = {}

        _establish(
            tmp_path,
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            provider="codex",
            guard=RecordingPlanningCommandGuard(),
            manifest=manifest,
        )

        assert manifest["planning_command_guard_provider"] == "codex"
        assert "make validate-pr-raw" in manifest["planning_command_guard_refuses"]
        assert "git log" in manifest["planning_command_guard_allows"]

    def test_an_unguarded_launch_records_nothing_it_did_not_establish(
        self, tmp_path: Path
    ) -> None:
        manifest: dict = {}

        _establish(
            tmp_path,
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            provider="claude-code",
            guard=RecordingPlanningCommandGuard(enforce=False),
            manifest=manifest,
        )

        assert manifest == {}
