"""A candidate verdict is admissible only for what the run actually audited.

The fifth axis of ``tech_lead_decision_contract`` (#345). It is judged against
the orchestrator-owned launch authority, never the agent-writable worktree
copies, so a decision cannot widen its own merge-facing scope by rewriting a
manifest mid-session.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.tech_lead_decision_contract import (
    validate_decision_for_authority,
)
from issue_orchestrator.domain.tech_lead_artifacts import TechLeadDecision
from issue_orchestrator.domain.tech_lead_candidate import TechLeadCandidate
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config

CANDIDATE_A = "a" * 40
CANDIDATE_B = "b" * 40


def _config(tmp_path: Path) -> Config:
    config = Config(repo="acme/repo", repo_root=tmp_path)
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


def _authority(*candidates: TechLeadCandidate) -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=tuple(candidate.pr_number for candidate in candidates),
        manifest_candidates=candidates,
    )


def _decision(*verdicts: dict[str, object]) -> TechLeadDecision:
    return TechLeadDecision.from_agent_payload(
        {
            "schema_version": 1,
            "summary": "Batch contract review.",
            "findings": [],
            "proposed_actions": [],
            "candidate_verdicts": list(verdicts),
        }
    )


def _violation(
    tmp_path: Path, authority: TechLeadLaunchAuthority, decision: TechLeadDecision
) -> str | None:
    config = _config(tmp_path)
    return validate_decision_for_authority(
        decision, authority, config=config, labels=LabelManager(config)
    )


def test_a_verdict_on_an_audited_candidate_is_admissible(tmp_path: Path) -> None:
    assert (
        _violation(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(
                {
                    "pr_number": 101,
                    "candidate_sha": CANDIDATE_A,
                    "disposition": "pass",
                    "rationale": "Conforms.",
                }
            ),
        )
        is None
    )


def test_a_verdict_on_an_unaudited_pull_request_rejects_the_decision(
    tmp_path: Path,
) -> None:
    detail = _violation(
        tmp_path,
        _authority(TechLeadCandidate(101, CANDIDATE_A)),
        _decision(
            {
                "pr_number": 999,
                "candidate_sha": CANDIDATE_A,
                "disposition": "pass",
                "rationale": "Conforms.",
            }
        ),
    )

    assert detail is not None
    assert "not launched to audit" in detail


def test_a_verdict_about_another_commit_rejects_the_decision(
    tmp_path: Path,
) -> None:
    """The whole point: a verdict is authority for one commit, not one number."""
    detail = _violation(
        tmp_path,
        _authority(TechLeadCandidate(101, CANDIDATE_A)),
        _decision(
            {
                "pr_number": 101,
                "candidate_sha": CANDIDATE_B,
                "disposition": "pass",
                "rationale": "Conforms.",
            }
        ),
    )

    assert detail is not None
    assert "not the candidate this session was launched to audit" in detail


def test_a_run_that_recorded_no_candidates_may_render_no_verdicts(
    tmp_path: Path,
) -> None:
    """A legacy authority row cannot bind anything, so it authorizes nothing."""
    legacy = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=(101,),
    )

    detail = _violation(
        tmp_path,
        legacy,
        _decision(
            {
                "pr_number": 101,
                "candidate_sha": CANDIDATE_A,
                "disposition": "pass",
                "rationale": "Conforms.",
            }
        ),
    )

    assert detail is not None
    assert "not launched to audit" in detail


def test_a_focused_flavor_may_render_no_candidate_verdict_at_all(
    tmp_path: Path,
) -> None:
    investigation = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        anchor_issue_number=7,
        focus_issue_number=7,
    )

    detail = _violation(
        tmp_path,
        investigation,
        _decision(
            {
                "pr_number": 101,
                "candidate_sha": CANDIDATE_A,
                "disposition": "pass",
                "rationale": "Conforms.",
            }
        ),
    )

    assert detail is not None
