"""Doctor: a batch review that can never reach `tech-lead-reviewed` (#345).

A Tech Lead `pass` rests on an independent reviewer approval of the EXACT
commit it audited, and only the review exchange files one. Where reviews take
the classic lane, every `pass` is refused at completion and the merge-facing
label is unreachable — a pipeline that looks healthy while quietly producing no
merge authority at all. That is a supported configuration, so it has to be
reported at startup rather than inferred from refusal receipts.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.tech_lead_candidate_policy import (
    TechLeadCandidatePolicy,
)
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks.tech_lead import (
    check_tech_lead_merge_authority,
)
from issue_orchestrator.infra.tech_lead_merge_authority import (
    tech_lead_merge_authority_readiness,
)


def _config(**overrides: object) -> Config:
    config = Config()
    config.repo = "porchpin/porchpin"
    config.agents = {"agent:backend": AgentConfig(prompt_path="prompts/backend.md")}
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_review_threshold = 3
    config.code_review_agent = "agent:reviewer"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class TestReachable:
    @pytest.mark.parametrize("mode", ["auto", "via-mcp", "via-local-loop"])
    def test_an_exchange_deployment_can_reach_the_merge_gate(self, mode: str) -> None:
        [check] = check_tech_lead_merge_authority(_config(review_exchange_mode=mode))

        assert check.status == "ok"

    def test_a_repository_without_batch_review_is_not_asked(self) -> None:
        """No batch fires, so there is nothing to warn about."""
        config = _config(review_exchange_mode="via-draft-pr")
        config.tech_lead_review_threshold = 0

        assert check_tech_lead_merge_authority(config) == []

    def test_no_tech_lead_at_all_is_not_asked(self) -> None:
        config = _config(review_exchange_mode="via-draft-pr")
        config.tech_lead_review_agent = None

        assert check_tech_lead_merge_authority(config) == []


class TestUnreachable:
    def test_a_draft_pr_deployment_is_warned_that_pass_is_unreachable(self) -> None:
        [check] = check_tech_lead_merge_authority(
            _config(review_exchange_mode="via-draft-pr")
        )

        assert check.status == "warning"
        assert "via-draft-pr" in check.detail
        assert "tech-lead-reviewed" in check.detail

    def test_an_agent_with_no_reviewer_is_named(self) -> None:
        config = _config(review_exchange_mode="via-local-loop")
        config.code_review_agent = None

        [check] = check_tech_lead_merge_authority(config)

        assert check.status == "warning"
        assert "agent:backend" in check.detail

    def test_a_per_agent_reviewer_satisfies_the_pairing(self) -> None:
        config = _config(review_exchange_mode="via-local-loop")
        config.code_review_agent = None
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path="prompts/backend.md", reviewer="agent:reviewer"
            )
        }

        [check] = check_tech_lead_merge_authority(config)

        assert check.status == "ok"

    def test_the_warning_reports_every_reason_at_once(self) -> None:
        config = _config(review_exchange_mode="via-draft-pr")
        config.code_review_agent = None

        readiness = tech_lead_merge_authority_readiness(config)

        assert readiness.active is True
        assert readiness.reachable is False
        assert len(readiness.problems) == 2


    def test_the_unreachable_label_is_the_one_the_projection_would_apply(self) -> None:
        """The warning names a label a `pass` would actually add (review A1).

        Doctor asks ``TechLeadCandidatePolicy`` for the merge-facing spelling
        rather than reading the config field a second time. Under a custom
        label, a locally-derived spelling would send the operator looking for a
        label this deployment never applies.
        """
        config = _config(review_exchange_mode="via-draft-pr")
        config.tech_lead_reviewed_label = "porchpin-tech-lead-ok"

        [check] = check_tech_lead_merge_authority(config)

        expected, _failed = TechLeadCandidatePolicy.terminal_labels_for(config)
        assert expected == "porchpin-tech-lead-ok"
        assert expected in check.detail
        assert "tech-lead-reviewed" not in check.detail


def test_the_readiness_owner_and_the_check_cannot_disagree() -> None:
    """One owner for the rule, so doctor and the docs describe one thing."""
    config = _config(review_exchange_mode="via-draft-pr")

    readiness = tech_lead_merge_authority_readiness(config)
    [check] = check_tech_lead_merge_authority(config)

    assert readiness.reachable is False
    assert all(problem in check.detail for problem in readiness.problems)


class TestThePairingQuestionMatchesTheRuntimes:
    """N3: doctor asks it the way `resolve_review_exchange_mode` asks it."""

    def test_an_empty_string_reviewer_is_unpaired_here_too(self) -> None:
        """The runtime tests falsiness; `is None` would call this paired."""
        config = _config(review_exchange_mode="via-local-loop")
        config.code_review_agent = ""

        readiness = tech_lead_merge_authority_readiness(config)

        assert readiness.reachable is False
        assert any("agent:backend" in problem for problem in readiness.problems)

    def test_reviewer_and_tech_lead_agents_are_not_asked_about_pairing(self) -> None:
        """They never take the coder side, so their pairing changes nothing."""
        config = _config(review_exchange_mode="via-local-loop")
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path="prompts/backend.md", reviewer="agent:reviewer"
            ),
            "agent:reviewer": AgentConfig(prompt_path="prompts/review.md"),
            "agent:tech-lead": AgentConfig(prompt_path="prompts/tech-lead.md"),
        }
        config.code_review_agent = None

        [check] = check_tech_lead_merge_authority(config)

        assert check.status == "ok"
