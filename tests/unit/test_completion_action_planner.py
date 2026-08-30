"""Direct tests for completion action planning policy."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    CreateTechLeadCaseFileIssueAction,
    CreateTechLeadIssueAction,
    RemoveLabelAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
)
from issue_orchestrator.control.completion_action_planner import (
    CompletionActionPlanner,
    critical_processing_errors,
)
from issue_orchestrator.control.completion_effect_gate import (
    is_completion_gate_action,
)
from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUSH,
    ERROR_PREFIX_RESULT_UNDELIVERED,
    ResultOnlyDelivery,
)
from issue_orchestrator.control.pull_request_observation import (
    PullRequestObservation,
)
from issue_orchestrator.control.result_only_completion import (
    ResultOnlyCloseIssueAction,
)
from issue_orchestrator.control.completion_history_status import (
    resolve_history_status,
)
from issue_orchestrator.control.tech_lead_completion import (
    admit_tech_lead_completion,
)
from issue_orchestrator.control.tech_lead_completion_errors import (
    TECH_LEAD_ERROR_PREFIXES,
)
from issue_orchestrator.control.label_manager import LabelManager
from tests.conftest import make_provider_availability
from issue_orchestrator.domain.board_snapshot import BoardFailure, BoardSnapshot
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    SUBJECT_RECOVERY_ACTIONS,
    AgentConfig,
    Issue,
    RequestedAction,
    Session,
    SessionStatus,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_manifest import PRToReview, TechLeadManifest
from issue_orchestrator.domain.tech_lead_candidate import TechLeadCandidate
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.domain.tech_lead_capabilities import (
    TECH_LEAD_ACTION_CAPABILITIES,
)
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.infra.open_issue_corpus_store import (
    SqliteOpenIssueCorpusStore,
)
from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
    OpenIssueCorpusStore,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import RepositoryHost
from tests.unit.session_run_helpers import make_session_run_assets


def make_issue(
    number: int = 1,
    *,
    labels: list[str] | None = None,
) -> Issue:
    """Create an issue for planner tests."""
    return Issue(
        number=number,
        title=f"Test issue {number}",
        labels=labels or ["agent:test"],
        repo="owner/repo",
    )


NO_PULL_REQUEST = PullRequestObservation.observed_none(
    "branch carries no pull request and the session references none"
)
"""A lookup that RAN and found nothing — the only verdict that may close."""


def observed_pull_request(
    url: str = "https://example.test/owner/repo/pull/7", number: int = 7
) -> PullRequestObservation:
    """A lookup that ran and found an open pull request for the issue."""
    return PullRequestObservation.observed(
        url=url, number=number, detail="branch carries an open pull request"
    )


def make_session(
    tmp_path: Path,
    *,
    issue: Issue | None = None,
    terminal_id: str = "issue-1",
) -> Session:
    """Create a session for planner tests."""
    issue = issue or make_issue()
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md", timeout_minutes=45),
        terminal_id=terminal_id,
        worktree_path=tmp_path,
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(tmp_path, session_name=terminal_id),
    )


def pr_candidate_sha(pr_number: int) -> str:
    """The exact head commit these fixtures pin each manifest PR to (#345)."""
    return f"{pr_number:03d}".ljust(40, "a")


def make_planner(
    config: Config,
    *,
    issue_labels: list[str] | None = None,
    repository_host: RepositoryHost | None = None,
    open_issue_corpus_store: OpenIssueCorpusStore | None = None,
) -> CompletionActionPlanner:
    """Create a planner with a repository host that can answer label reads.

    Tech-Lead-configured tests rendezvous with ``record_authority`` through the
    SQLite adapter at ``config.repo_root`` (a tmp_path); everything else gets
    the in-memory port fake so no state files are written.
    """
    issue = SimpleNamespace(labels=issue_labels or [])
    repository_host = repository_host or cast(
        RepositoryHost,
        SimpleNamespace(
            get_issue=lambda _number: issue,
            # The completion-time candidate re-read (#345, #352): by default
            # every manifest PR is still OPEN and still stands at the commit
            # the fixtures pinned it to. Both halves are read — a merged pull
            # request bears no disposition however unchanged its head.
            get_pr=lambda number: SimpleNamespace(
                head_sha=pr_candidate_sha(number), state="open"
            ),
        ),
    )
    tech_lead_authority = (
        SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
        if config.tech_lead_review_agent
        else InMemoryTechLeadAuthorityStore()
    )
    if open_issue_corpus_store is None:
        open_issue_corpus_store = InMemoryOpenIssueCorpusStore()
    corpus_repository_host = cast(
        RepositoryHost,
        SimpleNamespace(
            list_issues=lambda **_kwargs: [],
            list_issues_delta=lambda **_kwargs: ([], None),
        ),
    )
    open_issue_corpus = OpenIssueCorpusManager(
        corpus_repository_host,
        open_issue_corpus_store,
        is_enabled=lambda: bool(
            config.tech_lead_review_agent and config.tech_lead.dedup.enabled
        ),
    )
    # Completion consumes only refreshed local facts. Keep this fixture at the
    # same post-refresh boundary without making planning read GitHub.
    open_issue_corpus.sync()
    return CompletionActionPlanner(
        config, repository_host, LabelManager(config), tech_lead_authority,
        open_issue_corpus,
        lambda _n: None,  # no live target session in these unit fixtures (#6779 R1)
        make_provider_availability(config),
    )


def added_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels added by a planner result."""
    return {action.label for action in actions if isinstance(action, AddLabelAction)}


def removed_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels removed by a planner result."""
    return {action.label for action in actions if isinstance(action, RemoveLabelAction)}


def comments(actions: tuple[object, ...]) -> list[str]:
    """Return comments emitted by a planner result."""
    return [action.comment for action in actions if isinstance(action, AddCommentAction)]


def test_timeout_issue_session_marks_blocked_failed_and_releases_claim(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.TIMED_OUT,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Timed Out" in comment for comment in comments(actions))


def test_failed_issue_session_without_retry_needs_human(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = False

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert "needs-human" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Needs Investigation" in comment for comment in comments(actions))


def test_blocked_issue_session_uses_reported_label_and_reason(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.BLOCKED,
        blocked_label="blocked-upstream",
        blocked_reason="Waiting on dependency",
    )

    assert "blocked-upstream" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Waiting on dependency" in comment for comment in comments(actions))


def test_completed_with_publish_error_tracks_publish_failure(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        processing_errors=[f"{ERROR_PREFIX_PUSH}: rejected"],
        diagnostic_path=".issue-orchestrator/diagnostics/publish.md",
    )

    assert {"publish-failed", "publish-fail-count-1"} <= added_labels(actions)
    assert {"in-progress", "needs-rework"} <= removed_labels(actions)
    assert any("Publishing Failed" in comment for comment in comments(actions))


def test_review_exchange_halt_puts_issue_on_hold(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        review_exchange_halted=True,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Review Exchange Halted" in comment for comment in comments(actions))


def make_tech_lead_config(tmp_path: Path) -> Config:
    from unittest.mock import Mock

    config = Config()
    config.repo_root = tmp_path  # authority store lives in the repo state dir
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_reviewed_label = "tech-lead-reviewed"
    config.tech_lead_failed_label = "tech-lead-failed"
    # Destination worker a create_issue proposal routes to (#6779 R5/R9).
    config.agents = {"agent:web": Mock()}
    config.tech_lead_follow_up_agent = "agent:web"
    return config


def record_authority(
    config: Config, session: Session, authority: TechLeadLaunchAuthority
) -> None:
    """Persist the orchestrator-owned launch authority for a session run."""
    SqliteTechLeadAuthorityStore.for_repo(config.repo_root).record(
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
        authority=authority,
    )


def arm_batch_session(
    config: Config,
    session: Session,
    tmp_path: Path,
    *,
    with_manifest: bool = True,
) -> None:
    """Plant matching worktree copies AND the launch authority for a batch."""
    plant_tech_lead_assignment(
        session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
    )
    if with_manifest:
        plant_tech_lead_manifest(tmp_path, session)
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=session.issue.number,
            manifest_pr_numbers=(101, 102) if with_manifest else (),
            manifest_candidates=(
                (
                    TechLeadCandidate(101, pr_candidate_sha(101)),
                    TechLeadCandidate(102, pr_candidate_sha(102)),
                )
                if with_manifest
                else ()
            ),
            reviewed_candidates=(
                (
                    TechLeadCandidate(101, pr_candidate_sha(101)),
                    TechLeadCandidate(102, pr_candidate_sha(102)),
                )
                if with_manifest
                else ()
            ),
            contracted_candidates=(
                (
                    TechLeadCandidate(101, pr_candidate_sha(101)),
                    TechLeadCandidate(102, pr_candidate_sha(102)),
                )
                if with_manifest
                else ()
            ),
            diffed_candidates=(
                (
                    TechLeadCandidate(101, pr_candidate_sha(101)),
                    TechLeadCandidate(102, pr_candidate_sha(102)),
                )
                if with_manifest
                else ()
            ),
            validated_candidates=(
                (
                    TechLeadCandidate(101, pr_candidate_sha(101)),
                    TechLeadCandidate(102, pr_candidate_sha(102)),
                )
                if with_manifest
                else ()
            ),
        ),
    )


def arm_investigation_session(
    config: Config, session: Session, *, focus: int = 1
) -> None:
    """Plant matching worktree copies AND the launch authority for a focus run."""
    plant_tech_lead_assignment(
        session,
        TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=focus,
            focus_reason="Investigate: timed out",
        ),
    )
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=session.issue.number,
            focus_issue_number=focus,
        ),
    )


def arm_planning_session(
    config: Config, session: Session, *, focus: int = 1
) -> None:
    """Plant the worktree copies AND launch authority for a planning run (#136)."""
    plant_tech_lead_assignment(
        session,
        TechLeadAssignment(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            focus_issue_number=focus,
            focus_reason="Prepare: open and unblocked",
        ),
    )
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            anchor_issue_number=session.issue.number,
            focus_issue_number=focus,
        ),
    )


def arm_health_review_session(
    config: Config,
    session: Session,
    *,
    problem_issue_numbers: tuple[int, ...] = (),
    context_failure_numbers: tuple[int, ...] = (),
) -> None:
    """Plant the snapshot, assignment, and immutable health authority.

    ``problem_issue_numbers`` is the OWNED cohort (act-level authority);
    ``context_failure_numbers`` are unrelated failures that appear on the
    board but grant nothing (#6780). Cohort members also appear in
    ``recent_failures`` — that list is the board a reviewer reads, and it is
    a superset of the grant, which is exactly why authority is carried on its
    own surface.
    """
    plant_tech_lead_assignment(
        session, TechLeadAssignment(flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
    )
    BoardSnapshot(
        generated_at="2026-07-14T12:00:00+00:00",
        orchestrator_paused=False,
        recent_failures=[
            BoardFailure(
                issue_number=number,
                issue_title=f"Problem {number}",
                failure_reason="failed",
                artifact_hints=[],
            )
            for number in (*problem_issue_numbers, *context_failure_numbers)
        ],
        problem_cohort=list(problem_issue_numbers),
    ).write(session.run_dir / "tech-lead-data" / "board-snapshot.json")
    record_authority(
        config,
        session,
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=session.issue.number,
            problem_issue_numbers=problem_issue_numbers,
        ),
    )


def make_tech_lead_session(tmp_path: Path, *, terminal_id: str = "issue-1") -> Session:
    issue = make_issue(labels=["agent:tech-lead"])  # agent_type derives from labels
    return make_session(tmp_path, issue=issue, terminal_id=terminal_id)


def plant_tech_lead_assignment(session: Session, assignment: TechLeadAssignment) -> None:
    """Write the launch-time assignment into the session's tech-lead-data dir."""
    assignment_path = session.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    assignment.write(assignment_path)
    run_manifest_path = session.run_dir / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text())
    run_manifest["tech_lead_assignment"] = str(assignment_path)
    run_manifest_path.write_text(json.dumps(run_manifest))


def plant_tech_lead_manifest(tmp_path: Path, session: Session) -> None:
    """Write a two-PR tech_lead manifest discoverable via the run manifest."""
    manifest = TechLeadManifest(
        prs=[
            PRToReview(
                number=101,
                title="PR 101",
                url="https://example/pr/101",
                branch="b1",
                head_sha=pr_candidate_sha(101),
            ),
            PRToReview(
                number=102,
                title="PR 102",
                url="https://example/pr/102",
                branch="b2",
                head_sha=pr_candidate_sha(102),
            ),
        ]
    )
    manifest_path = tmp_path / "tech-lead-manifest.json"
    manifest.write(manifest_path)
    run_manifest_path = session.run_dir / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text())
    run_manifest["tech_lead_manifest"] = str(manifest_path)
    run_manifest_path.write_text(json.dumps(run_manifest))


def pass_verdicts(*pr_numbers: int) -> list[dict[str, object]]:
    """PASS verdicts for the exact candidates these fixtures pinned (#345)."""
    return [
        {
            "pr_number": pr_number,
            "candidate_sha": pr_candidate_sha(pr_number),
            "disposition": "pass",
            "rationale": f"PR #{pr_number} conforms to the governing contract.",
            "finding_ids": ["T1"],
        }
        for pr_number in pr_numbers
    ]


def plant_tech_lead_decision_pair(
    session: Session,
    *,
    comment_targets: tuple[int, ...] = (101,),
    candidate_verdicts: list[dict[str, object]] | None = None,
) -> None:
    """Write a valid decision + report pair into the session's tech-lead-data dir.

    ``comment_targets`` controls the post_comment proposals. Targets must fall
    inside the session's launch scope (manifest PRs + anchor for batch, the
    focus issue for investigations) and investigations must include the focus
    issue (#6761 F2 + re-review F2).
    """
    data_dir = session.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    proposed_actions = [
        {
            "id": f"A{index}",
            "action_type": "post_comment",
            "target_number": target,
            "body": f"Diagnosis for #{target}: flaky CI.",
            "finding_ids": ["T1"],
        }
        for index, target in enumerate(comment_targets, start=1)
    ]
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "One systemic pattern found.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": proposed_actions,
                "candidate_verdicts": candidate_verdicts or [],
            }
        )
    )
    action_ids = ", ".join(action["id"] for action in proposed_actions)
    (data_dir / "tech-lead-report.md").write_text(
        f"# Report\n\nT1 leads to {action_ids or 'no actions'}.\n"
    )


def _tech_lead_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "tech-lead-reviewed"
    ]


def test_completed_tech_lead_session_labels_manifest_prs_and_plans_decision(
    tmp_path: Path,
) -> None:
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    plant_tech_lead_decision_pair(
        session, candidate_verdicts=pass_verdicts(101, 102)
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert {action.issue_number for action in _tech_lead_labels(actions)} == {101, 102}
    assert "in-progress" in removed_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction)
        and action.number == 101
        and action.comment.startswith("Diagnosis for #101: flaky CI.")
    ]
    assert len(decision_comments) == 1
    assert "ADR-0031" in decision_comments[0].comment
    # The candidate's PASS receipt is a SECOND comment on the same PR, and it
    # names the exact commit the disposition is authority for (#345).
    receipts = [
        action for action in actions
        if isinstance(action, AddCommentAction)
        and action.number == 101
        and pr_candidate_sha(101) in action.comment
    ]
    assert len(receipts) == 1


def test_a_manifest_pr_that_merged_since_the_manifest_receives_no_disposition(
    tmp_path: Path,
) -> None:
    """#352: the planner's completion re-read carries lifecycle, not just head.

    PR 101 merges while the batch review is running, at exactly the commit that
    was audited — so a head-only re-read answers "unchanged" and projects
    merge-facing authority onto a pull request that has already merged. The
    open sibling is unaffected: the refusal is per candidate.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    plant_tech_lead_decision_pair(
        session, candidate_verdicts=pass_verdicts(101, 102)
    )
    issue = SimpleNamespace(labels=[])
    merged_since_the_manifest = cast(
        RepositoryHost,
        SimpleNamespace(
            get_issue=lambda _number: issue,
            get_pr=lambda number: SimpleNamespace(
                head_sha=pr_candidate_sha(number),
                state="merged" if number == 101 else "open",
            ),
        ),
    )

    actions = make_planner(
        config, repository_host=merged_since_the_manifest
    ).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert {action.issue_number for action in _tech_lead_labels(actions)} == {102}
    assert 101 not in {
        action.issue_number
        for action in actions
        if isinstance(action, AddLabelAction)
    }


def test_completed_tech_lead_session_missing_pair_fails_labels_and_surfaces_rejection(
    tmp_path: Path,
) -> None:
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    # No decision artifact pair written: contract violation, no grace path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    failed_actions = [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "tech-lead-failed"
    ]
    assert {action.issue_number for action in failed_actions} == {101, 102}
    assert "tech-lead-reviewed" not in added_labels(actions)
    rejections = [
        action for action in actions if isinstance(action, SurfaceTechLeadProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert rejections[0].proposal_type == "decision"
    assert rejections[0].issue_number == session.issue.number
    assert "tech-lead-decision.json" in rejections[0].body_preview


def test_tech_lead_manifest_in_sibling_run_dir_is_ignored(tmp_path: Path) -> None:
    """Completion reads only ``session.run_dir`` (typed run contract).

    The pre-#6769 code scanned every run dir under the worktree's sessions
    root and could pick up a stale prior run's manifest. A manifest planted
    in a sibling run dir must now be invisible: no labels on its PRs.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    plant_tech_lead_assignment(
        session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
    )
    plant_tech_lead_decision_pair(session)
    stale_run_dir = session.run_dir.parent / "20250101T000000000000Z__issue-1"
    stale_run_dir.mkdir(parents=True)
    stale_manifest = TechLeadManifest(
        prs=[
            PRToReview(
                number=999, title="Stale PR", url="https://example/pr/999", branch="s"
            )
        ]
    )
    stale_manifest_path = tmp_path / "stale-tech-lead-manifest.json"
    stale_manifest.write(stale_manifest_path)
    (stale_run_dir / "manifest.json").write_text(
        json.dumps({"tech_lead_manifest": str(stale_manifest_path)})
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    tech_lead_label_targets = {
        action.issue_number
        for action in actions
        if isinstance(action, AddLabelAction)
        and action.label in ("tech-lead-reviewed", "tech-lead-failed")
    }
    assert tech_lead_label_targets == set()


def test_completed_tech_lead_investigation_session_plans_decision_without_labels(
    tmp_path: Path,
) -> None:
    """Failure investigations plan decision actions but never manifest labels.

    Flavor comes from the launch-time assignment (#6768 B4) — both tech_lead
    variants share the issue-N terminal, so the manifest planted here must
    still not be labeled.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "tech-lead-reviewed" not in added_labels(actions)
    assert "tech-lead-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 1
        and "Diagnosis" in action.comment
    ]
    assert len(decision_comments) == 1


def test_completed_non_tech_lead_session_is_unaffected(tmp_path: Path) -> None:
    config = make_tech_lead_config(tmp_path)
    session = make_session(tmp_path)  # agent:test, not the tech lead agent

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, SurfaceTechLeadProposalAction) for a in actions)
    assert added_labels(actions) == set()
    assert "in-progress" in removed_labels(actions)


def _close_actions(actions: tuple[object, ...]) -> list[CloseIssueAction]:
    return [a for a in actions if isinstance(a, CloseIssueAction)]


def _tech_lead_failed_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "tech-lead-failed"
    ]


def test_failed_batch_labels_prs_failed_and_closes_tracking_issue(
    tmp_path: Path,
) -> None:
    """A FAILED batch reaches the tech-lead-failed contract (#6768 r5).

    Manifest PRs carry the operator-visible tech-lead-failed label and the
    tracking issue closes (after the generic needs-human diagnosis and the PR
    labels) so restart recovery cannot requeue it with an empty manifest.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.FAILED,
    )

    assert {a.issue_number for a in _tech_lead_failed_labels(actions)} == {101, 102}
    (close,) = _close_actions(actions)
    assert close.issue_number == session.issue.number
    # Composes with (not replaces) the generic failure diagnosis...
    assert "needs-human" in added_labels(actions)
    # ...and the terminal close comes after every label action.
    assert actions.index(close) == len(actions) - 1


def test_timed_out_batch_labels_prs_failed_and_closes_tracking_issue(
    tmp_path: Path,
) -> None:
    """A TIMED_OUT batch gets the same terminal lifecycle as a failed one."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.TIMED_OUT,
    )

    assert {a.issue_number for a in _tech_lead_failed_labels(actions)} == {101, 102}
    (close,) = _close_actions(actions)
    assert close.issue_number == session.issue.number
    # Composes with the generic timeout diagnosis; close is last.
    assert "blocked-failed" in added_labels(actions)
    assert actions.index(close) == len(actions) - 1


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_failure_investigation_failure_paths_preserve_source_issue(
    tmp_path: Path, status: SessionStatus
) -> None:
    """Failed/timed-out investigations never touch manifest PRs or close their
    anchor — it IS the original failed work issue (#6768 r5 controls)."""
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(session, status)

    assert _close_actions(actions) == []
    assert _tech_lead_failed_labels(actions) == []
    assert _tech_lead_labels(actions) == []


def test_failure_investigation_tech_lead_session_never_labels_manifest_prs(
    tmp_path: Path,
) -> None:
    """A focused investigation must not label PRs even when a manifest exists (#6768 B4)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _tech_lead_labels(actions) == []
    assert "in-progress" in removed_labels(actions)


def test_completed_health_review_plans_decision_and_closes_anchor(
    tmp_path: Path,
) -> None:
    """Health review + valid pair: decision actions, close the anchor, no labels.

    The anchor issue is a walk-the-floor log entry (ADR-0031 §4) — a landed
    review closes it, and manifest labels never apply (there is no manifest).
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    # Even stray planted manifest noise must not be labeled for this flavor.
    plant_tech_lead_manifest(tmp_path, session)
    plant_tech_lead_decision_pair(session, comment_targets=(session.issue.number,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "tech-lead-reviewed" not in added_labels(actions)
    assert "tech-lead-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction)
        and action.number == session.issue.number
        and "Diagnosis" in action.comment
    ]
    assert len(decision_comments) == 1
    (close,) = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert close.issue_number == session.issue.number
    assert "Health review completed" in close.reason
    # Terminal ordering: a mid-apply crash leaves the anchor open.
    assert actions.index(close) == len(actions) - 1


def test_health_review_missing_pair_surfaces_rejection_and_keeps_anchor_open(
    tmp_path: Path,
) -> None:
    """Missing/invalid pair: rejection surfaced, anchor NOT closed (visibility)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    # No decision artifact pair written.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    rejections = [
        action for action in actions if isinstance(action, SurfaceTechLeadProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_health_review_decision_targeting_anchor_passes_scope_validation(
    tmp_path: Path,
) -> None:
    """The anchor issue is the ONE allowed target for health post_comment."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(session.issue.number,))

    error = admit_tech_lead_completion(
        config,
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    ).error

    assert error is None


def test_health_review_decision_targeting_other_issue_is_rejected(
    tmp_path: Path,
) -> None:
    """A health decision may not address arbitrary issues (#6761 rr F2 scope).

    Board-wide findings belong in scope-free create_issue/flag_pattern
    proposals; a post_comment outside the anchor is a contract violation on
    both completion seams (processing outcome AND planned effects).
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(999,))

    error = admit_tech_lead_completion(
        config,
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    ).error
    assert error is not None
    assert "outside this session's launch scope" in error

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )
    rejections = [
        action for action in actions if isinstance(action, SurfaceTechLeadProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    # And the out-of-scope comment is never planned.
    assert not any(
        isinstance(a, AddCommentAction) and a.number == 999 for a in actions
    )


def _plant_flag_pattern_decision(
    session: Session, *, signature: str = "db-timeout", area: str = "db"
) -> None:
    """Plant a health-review decision whose only action is a flag_pattern
    (scope-free board-wide finding, #6781)."""
    data_dir = session.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Recurring cross-job pattern found.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "DB pool exhausted",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": [
                    {
                        "id": "A1",
                        "action_type": "flag_pattern",
                        "body": "Three sessions hit the same DB pool timeout.",
                        "pattern_signature": signature,
                        "area": area,
                        "finding_ids": ["T1"],
                    }
                ],
            }
        )
    )
    (data_dir / "tech-lead-report.md").write_text("# Report\n\nT1 leads to A1.\n")


def test_health_review_flag_pattern_is_scope_free_and_opens_case_file(
    tmp_path: Path,
) -> None:
    """A health decision's flag_pattern carries no target: it passes scope
    validation and plans a durable case file for a first-seen signature
    (#6781 acceptance)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    _plant_flag_pattern_decision(session)

    # Scope-free: no out-of-scope error even though it is not the anchor.
    error = admit_tech_lead_completion(
        config,
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    ).error
    assert error is None

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    [case_file] = [
        a for a in actions if isinstance(a, CreateTechLeadCaseFileIssueAction)
    ]
    assert case_file.pattern_signature == "db-timeout"
    assert case_file.area == "db"
    assert TECH_LEAD_OBSERVATION_LABEL in case_file.labels


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_failed_health_review_closes_anchor_without_labels(
    tmp_path: Path, status: SessionStatus
) -> None:
    """FAILED/TIMED_OUT health sessions close the anchor (no manifest labels).

    An open dead anchor would be requeued at restart AND dedupe the next
    interval's trigger; closing it lets a fresh review fire on schedule.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(session, status)

    (close,) = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert close.issue_number == session.issue.number
    assert "Health review session failed" in close.reason
    assert _tech_lead_failed_labels(actions) == []
    assert _tech_lead_labels(actions) == []
    assert actions.index(close) == len(actions) - 1


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_a_failed_planning_run_never_closes_its_subject(
    tmp_path: Path, status: SessionStatus
) -> None:
    """A focused run's "anchor" is a live work item (#136).

    Batch and health anchors are bookkeeping issues that MUST close when their
    session dies. A planning run is aimed at an ordinary open issue the
    orchestrator still owes work on, so a terminal-effects guard that missed
    this flavor would close real work because a tech-lead session crashed.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)

    actions = make_planner(config).generate_completion_actions(session, status)

    assert [a for a in actions if isinstance(a, CloseIssueAction)] == []
    assert _tech_lead_failed_labels(actions) == []


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_a_dead_planning_run_never_blocks_its_healthy_subject(
    tmp_path: Path, status: SessionStatus
) -> None:
    """A crash must not do what the role itself may not (#136 review A1).

    The generic session-terminal path stamps ``blocked-failed`` (TIMED_OUT) or
    ``needs-human`` (FAILED) on every ``issue-`` session's issue without asking
    whose session it is. Admission accepts only an OPEN, non-blocked subject for
    a planning run, and the role's capability row omits every recovery kind — so
    letting that path stand would block healthy work nobody asked this role to
    recover, and make it eligible for the failure investigation the role was
    specifically built not to be able to invoke.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(session, status)

    assert labels.blocked_failed not in added_labels(actions)
    assert labels.needs_human not in added_labels(actions)
    # The claim is still released and the operator still gets the obituary:
    # the subject is left untouched, not left silently claimed.
    assert labels.in_progress in removed_labels(actions)
    assert any(
        "planning_investigation` session on this issue" in comment
        for comment in comments(actions)
    )


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_a_dead_failure_investigation_still_reports_its_subject(
    tmp_path: Path, status: SessionStatus
) -> None:
    """The substitution is scoped to roles with no recovery authority (#136 A1).

    A failure investigation holds the recovery kinds and its subject is blocked
    by definition, so the generic terminal effects stand exactly as they did
    before the bounded flavor existed. Without this, a fix aimed at planning
    would silently stop reporting every focused run's death.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    labels = LabelManager(config)
    expected = (
        labels.blocked_failed
        if status is SessionStatus.TIMED_OUT
        else labels.needs_human
    )

    actions = make_planner(config).generate_completion_actions(session, status)

    assert expected in added_labels(actions)
    assert labels.in_progress in removed_labels(actions)


# --- #182: the four remaining doors onto a subject's recovery state -----------
#
# The crash and rejection paths ask the owner (above). ``invalid_record_actions``,
# the BLOCKED completion path, the publish-failure path, and the review-exchange
# halt are GENERIC session machinery that never learns whose session it is, so
# the answer is threaded to them as a value. All four are reachable for a
# planning run: every tech_lead flavor runs in an ``issue-`` terminal, a rejected
# record is an incidental malfunction of any session, the tech_lead prompt itself
# instructs the agent to report a missing workspace via ``coding-done blocked``,
# and a focused run publishes onto its disposable branch — where a failed push
# lands a blocking label on the subject (#182 review F1).

_REJECTED_RECORD_DETAIL: dict[str, str] = {
    "failure_kind": "invalid_completion_record",
    "failure_reason": "Completion record rejected: unknown field",
    "completion_load_failure": "invalid_schema",
    "completion_parse_error": "unknown field",
}

_NO_RECOVERY_AUTHORITY_PHRASE = "holds no recovery authority over the issue"


def test_a_planning_run_with_a_rejected_record_never_blocks_its_subject(
    tmp_path: Path,
) -> None:
    """Door 3 (#182): a malfunctioning record must not do what the role may not.

    A rejected completion record is an incidental malfunction, not the role's
    designed failure route — but the generic path stamps ``needs-human`` on
    every ``issue-`` session's issue, which for a planning run is the live,
    unblocked subject its admission required to be exactly that.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.FAILED,
        completion_detail=dict(_REJECTED_RECORD_DETAIL),
    )

    assert labels.needs_human not in added_labels(actions)
    # The claim is still released and the operator still gets the obituary,
    # with the rejection detail and the one-voice explanation of the absence.
    assert labels.in_progress in removed_labels(actions)
    (comment,) = comments(actions)
    assert "Completion Record Rejected" in comment
    assert "unknown field" in comment
    assert _NO_RECOVERY_AUTHORITY_PHRASE in comment
    assert f"marked as `{labels.needs_human}`" not in comment


def test_a_failure_investigation_with_a_rejected_record_still_escalates(
    tmp_path: Path,
) -> None:
    """The suppression is scoped to roles with no recovery authority (#182).

    A failure investigation holds the recovery kinds, so door 3 behaves for it
    exactly as it did before the bounded flavor existed.
    """
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.FAILED,
        completion_detail=dict(_REJECTED_RECORD_DETAIL),
    )

    assert labels.needs_human in added_labels(actions)
    assert labels.in_progress in removed_labels(actions)
    assert _NO_RECOVERY_AUTHORITY_PHRASE not in comments(actions)[0]


def test_a_batch_review_with_a_rejected_record_still_escalates(
    tmp_path: Path,
) -> None:
    """A non-focused run's "subject" is its own anchor, so door 3 is unchanged."""
    config = make_tech_lead_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.FAILED,
        completion_detail=dict(_REJECTED_RECORD_DETAIL),
    )

    assert labels.needs_human in added_labels(actions)


def test_an_ordinary_issue_session_with_a_rejected_record_still_escalates(
    tmp_path: Path,
) -> None:
    """No tech_lead configured at all: door 3 keeps its generic behavior (#182)."""
    config = Config()
    config.retry.interrupted_sessions.enabled = False
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
        completion_detail=dict(_REJECTED_RECORD_DETAIL),
    )

    assert labels.needs_human in added_labels(actions)
    assert labels.in_progress in removed_labels(actions)
    assert _NO_RECOVERY_AUTHORITY_PHRASE not in comments(actions)[0]


def test_a_blocked_planning_run_never_blocks_its_subject(tmp_path: Path) -> None:
    """Door 4 (#182): traced to a conclusion, and it IS reachable.

    #136's exchange left this door untraced. It is reachable: a planning run
    occupies an ``issue-`` terminal like every other flavor, and the tech_lead
    prompt tells the agent to report a broken workspace with ``coding-done
    blocked``. Left open, an agent saying "I could not prepare this" would
    block the very issue it was sent to prepare.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.BLOCKED,
        blocked_reason="tech-lead-data directory missing",
    )

    assert labels.blocked not in added_labels(actions)
    assert added_labels(actions) == set()
    assert labels.in_progress in removed_labels(actions)
    (comment,) = comments(actions)
    assert "Session Blocked" in comment
    assert "tech-lead-data directory missing" in comment
    assert _NO_RECOVERY_AUTHORITY_PHRASE in comment
    assert "will not be automatically retried" not in comment


def test_a_blocked_planning_run_suppresses_a_reported_blocked_label(
    tmp_path: Path,
) -> None:
    """The suppression follows the label the block would have used (#182).

    A caller-supplied ``blocked_label`` is still a blocking label on the
    subject, so the rule applies to it and the operator note names it.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.BLOCKED,
        blocked_label="blocked-upstream",
        blocked_reason="Waiting on dependency",
    )

    assert added_labels(actions) == set()
    assert "`blocked-upstream` label was added" in comments(actions)[0]


def test_a_blocked_failure_investigation_still_blocks_its_subject(
    tmp_path: Path,
) -> None:
    """Door 4 is unchanged for a role that holds the recovery kinds (#182)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.BLOCKED,
        blocked_reason="Cannot reproduce",
    )

    assert labels.blocked in added_labels(actions)
    assert labels.in_progress in removed_labels(actions)
    assert _NO_RECOVERY_AUTHORITY_PHRASE not in comments(actions)[0]


def test_a_blocked_health_review_still_blocks_its_anchor(tmp_path: Path) -> None:
    """A non-focused run's blocking label is bookkeeping on its own anchor."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_health_review_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.BLOCKED,
        blocked_reason="Board snapshot unreadable",
    )

    assert labels.blocked in added_labels(actions)


def test_a_provider_blocked_planning_run_records_the_outage(tmp_path: Path) -> None:
    """The provider route is untouched by #182: it is not a recovery verdict.

    A dead credential says nothing about the issue's substance, and the outage
    is recorded through its own owner rather than as a blocking label on the
    subject — so there is no recovery-state change for the threaded answer to
    suppress, and the claim release must survive.
    """
    from issue_orchestrator.ports.provider_resilience import ProviderErrorType

    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.BLOCKED,
        blocked_reason="Provider not authenticated",
        provider_error_type=ProviderErrorType.AUTH,
    )

    assert labels.blocked not in added_labels(actions)
    assert labels.in_progress in removed_labels(actions)


# -- Door 7's planned twin: a suppressed escalation still reaches a human -----

_PLANNING_QUESTION = "which milestone should this target?"

# The completion outcome that carries each recovery REQUEST, and what that
# outcome needs to be planned. The completion-record seam refuses these
# requests for a bounded role (#257), so each one MUST have a planned path that
# tells the operator what happened — otherwise the escalation vanishes.
_RECOVERY_REQUEST_PLANNED_TWIN: dict[RequestedAction, tuple[SessionStatus, dict]] = {
    RequestedAction.ADD_BLOCKED_LABEL: (
        SessionStatus.BLOCKED,
        {"blocked_reason": "tech-lead-data directory missing"},
    ),
    RequestedAction.ADD_NEEDS_HUMAN_LABEL: (
        SessionStatus.NEEDS_HUMAN,
        {"completion_detail": {"question": _PLANNING_QUESTION}},
    ),
}


def test_every_refused_recovery_request_is_covered_by_a_planned_twin() -> None:
    """Derived from the domain's vocabulary, so the pairing cannot drift.

    A recovery action added to ``SUBJECT_RECOVERY_ACTIONS`` is refused at the
    completion-record seam the moment it joins the set. This assertion is what
    stops it from being refused SILENTLY: the edit that adds it must also name
    the outcome whose planned path speaks for it, or this fails.
    """
    assert set(_RECOVERY_REQUEST_PLANNED_TWIN) == set(SUBJECT_RECOVERY_ACTIONS)


@pytest.mark.parametrize(
    "requested_action", sorted(_RECOVERY_REQUEST_PLANNED_TWIN, key=str)
)
def test_a_suppressed_recovery_request_still_reaches_a_human(
    requested_action: RequestedAction, tmp_path: Path
) -> None:
    """Suppression is not silence: no label, but a comment and a released claim.

    The round-1 defect (#257 F1) was a green suite proving only the absence of
    a label. A bounded planning run that asked for one gets the same three
    things whichever request was refused — nothing added to the subject, the
    one voice explaining why, and the ``in-progress`` claim released so the
    issue is not held by a marker nothing is holding.
    """
    status, extra = _RECOVERY_REQUEST_PLANNED_TWIN[requested_action]
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session, status, **extra
    )

    assert added_labels(actions) == set()
    assert labels.in_progress in removed_labels(actions)
    (comment,) = comments(actions)
    assert _NO_RECOVERY_AUTHORITY_PHRASE in comment


def test_a_needs_human_planning_run_surfaces_the_question_it_asked(
    tmp_path: Path,
) -> None:
    """The escalation's whole content is the question; it must be readable.

    ``shape_requested_actions_for_tech_lead`` drops the agent's own
    ``post_comment``, so for a tech_lead run this comment is the ONLY place the
    question is ever written where an operator looks.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.NEEDS_HUMAN,
        completion_detail={"question": _PLANNING_QUESTION},
    )

    (comment,) = comments(actions)
    assert "Human Input Requested" in comment
    assert _PLANNING_QUESTION in comment
    assert f"`{LabelManager(config).needs_human}` label was added" in comment


def test_a_needs_human_run_that_may_escalate_keeps_the_generic_policy(
    tmp_path: Path,
) -> None:
    """Nothing changes for a role whose requested label lands (#257 scope).

    A run that MAY leave the label is already visible on the board through it,
    and the label is what holds the issue — so the claim stays, and the agent's
    own comment carries the question. Planning a second comment here, or
    releasing a claim the label is holding, would be a different decision than
    the one #257 asked for.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.NEEDS_HUMAN,
        completion_detail={"question": _PLANNING_QUESTION},
    )

    assert actions == ()


def test_an_ordinary_needs_human_session_keeps_the_generic_policy(
    tmp_path: Path,
) -> None:
    """The unbounded default: a non-tech_lead escalation is untouched."""
    actions = make_planner(Config()).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.NEEDS_HUMAN,
        completion_detail={"question": _PLANNING_QUESTION},
    )

    assert actions == ()


def test_a_needs_human_planning_run_outside_an_issue_terminal_plans_nothing(
    tmp_path: Path,
) -> None:
    """A review terminal's escalation does not map onto issue state.

    Same boundary ``agent_blocked_actions`` draws: the parent workflow owns
    whatever happens to that PR, so this path stays out of it.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path, terminal_id="review-1")
    arm_planning_session(config, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.NEEDS_HUMAN,
        completion_detail={"question": _PLANNING_QUESTION},
    )

    assert actions == ()


def test_a_needs_human_planning_run_without_a_question_still_speaks(
    tmp_path: Path,
) -> None:
    """A record whose question did not survive is still not silence.

    ``question`` is required by ``coding-done needs_human``, but the planner
    reads it from curated completion detail rather than from the record, so a
    missing one must degrade to a comment an operator can act on — not to the
    empty plan this issue is about.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session, SessionStatus.NEEDS_HUMAN
    )

    (comment,) = comments(actions)
    assert "No question provided." in comment
    assert _NO_RECOVERY_AUTHORITY_PHRASE in comment
    assert labels.in_progress in removed_labels(actions)


_PUSH_FAILURE = [f"{ERROR_PREFIX_PUSH}: remote rejected"]


def test_a_planning_run_that_cannot_publish_never_blocks_its_subject(
    tmp_path: Path,
) -> None:
    """Door 5 (#182 review F1): the door a run reaches by SUCCEEDING.

    A focused run publishes onto its own disposable branch, and a failed push
    lands ``publish-failed`` on ``issue-{N}`` — which for a focused flavor is
    the live subject. The whole mutation goes, not just the blocking label:
    ``needs-rework`` is left alone and the publish counter is not rolled, so
    the suppression note's promise that "the issue is left exactly as it was"
    is literally true.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
        processing_errors=list(_PUSH_FAILURE),
    )

    assert added_labels(actions) == set()
    assert removed_labels(actions) == {labels.in_progress}
    (comment,) = comments(actions)
    assert "Publishing Failed" in comment
    assert "remote rejected" in comment
    assert _NO_RECOVERY_AUTHORITY_PHRASE in comment
    assert "will not be automatically retried" not in comment
    assert "attempt" not in comment


def test_a_planning_run_at_the_escalation_threshold_still_never_escalates(
    tmp_path: Path,
) -> None:
    """The counter is the SUBJECT's history, and a bounded role cannot trip it.

    Suppressing only the label would leave the threshold reachable: a subject
    already carrying two publish failures would be escalated to ``needs-human``
    by a role whose capability row forbids proposing exactly that.
    """
    config = make_tech_lead_config(tmp_path)
    config.max_consecutive_publish_failures = 3
    issue = make_issue(labels=["agent:tech-lead", "publish-fail-count-2"])
    session = make_session(tmp_path, issue=issue, terminal_id="issue-1")
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
        processing_errors=list(_PUSH_FAILURE),
    )

    assert added_labels(actions) == set()
    assert removed_labels(actions) == {labels.in_progress}
    assert "Escalated" not in comments(actions)[0]


def test_a_failure_investigation_that_cannot_publish_still_marks_its_subject(
    tmp_path: Path,
) -> None:
    """Door 5 is unchanged for a role that holds the recovery kinds (#182)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
        processing_errors=list(_PUSH_FAILURE),
    )

    assert {labels.publish_failed, labels.publish_fail_count_label(1)} <= added_labels(
        actions
    )
    assert {labels.in_progress, labels.needs_rework} <= removed_labels(actions)
    assert _NO_RECOVERY_AUTHORITY_PHRASE not in comments(actions)[0]


def test_a_batch_review_that_cannot_publish_still_marks_its_anchor(
    tmp_path: Path,
) -> None:
    """A non-focused run's "subject" is its own anchor, so door 5 is unchanged."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
        processing_errors=list(_PUSH_FAILURE),
    )

    assert labels.publish_failed in added_labels(actions)


def test_an_ordinary_session_at_the_threshold_still_escalates(tmp_path: Path) -> None:
    """No tech_lead configured: door 5 keeps its escalation behavior (#182)."""
    config = Config()
    config.max_consecutive_publish_failures = 3
    issue = make_issue(labels=["agent:test", "publish-fail-count-2"])
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path, issue=issue),
        SessionStatus.COMPLETED,
        processing_errors=list(_PUSH_FAILURE),
    )

    assert labels.needs_human in added_labels(actions)
    assert labels.publish_failed not in added_labels(actions)
    assert {labels.in_progress, labels.needs_rework} <= removed_labels(actions)
    (comment,) = comments(actions)
    assert "Escalated" in comment
    assert "3 consecutive times" in comment
    assert f"marked as `{labels.needs_human}`" in comment


def test_a_publish_failure_releases_the_claim_only_after_labeling(
    tmp_path: Path,
) -> None:
    """The claim outlives every label mutation on this path (#182 review N5).

    Moving the publish-failure policy to its own owner reordered the result:
    the label mutations land first, then the comment, then the in-progress
    release. That ordering is pinned rather than left incidental, because it is
    the recoverable one — an applier that stopped partway through leaves the
    issue claimed and unlabeled, not unclaimed and unlabeled, which the next
    tick would pick up as if nothing had happened.
    """
    config = Config()
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        processing_errors=list(_PUSH_FAILURE),
    )

    last = actions[-1]
    assert isinstance(last, RemoveLabelAction)
    assert last.label == labels.in_progress
    assert labels.publish_failed in added_labels(actions[:-1])


def test_a_halted_exchange_never_blocks_a_planning_subject(tmp_path: Path) -> None:
    """Door 6 (#182 review F1): the halt markers are raised during CREATE_PR.

    A focused tech_lead run executes ``CREATE_PR`` like any other session, so
    whether it can reach a halted exchange is a deployment's reviewer
    configuration rather than a structural guarantee. The door is closed rather
    than argued shut: ``blocked-failed`` on the subject is a recovery-state
    change either way, and the halt is still reported.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_planning_session(config, session)
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
        review_exchange_halted=True,
    )

    assert added_labels(actions) == set()
    assert removed_labels(actions) == {labels.in_progress}
    (comment,) = comments(actions)
    assert "Review Exchange Halted" in comment
    assert _NO_RECOVERY_AUTHORITY_PHRASE in comment
    assert "will not be retried automatically" not in comment


def test_a_halted_exchange_still_blocks_an_ordinary_subject(tmp_path: Path) -> None:
    """Door 6 keeps its generic behavior for every authorized run (#182)."""
    config = Config()
    labels = LabelManager(config)

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        review_exchange_halted=True,
    )

    assert labels.blocked_failed in added_labels(actions)
    assert labels.in_progress in removed_labels(actions)
    assert _NO_RECOVERY_AUTHORITY_PHRASE not in comments(actions)[0]


def test_tech_lead_session_without_launch_authority_is_rejected(
    tmp_path: Path, caplog
) -> None:
    """No orchestrator launch-authority record => never trust worktree copies.

    The old fail-safe (skip effects, PRs re-enter the next batch) let a
    missing assignment reach the success path (#6761 re-review F1); now the
    rejection is surfaced and no labels are planned.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    plant_tech_lead_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _tech_lead_labels(actions) == []
    rejections = [
        a for a in actions
        if isinstance(a, SurfaceTechLeadProposalAction) and a.mode == "rejected"
    ]
    assert len(rejections) == 1
    assert "launch-authority" in rejections[0].body_preview
    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "Launch authority rejected" in caplog.text


def test_tech_lead_artifacts_in_sibling_run_dir_are_ignored(
    tmp_path: Path, caplog
) -> None:
    """Stale artifacts from a previous run must not leak into this completion.

    Reads go exclusively through the session's typed run_dir (#6768 B6): a
    sibling run carrying a batch assignment and a full manifest produces no
    labels and no decision actions for the current session.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    sibling = session.run_dir.parent / "issue-1__coding-0"
    (sibling / "tech-lead-data").mkdir(parents=True)
    TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW).write(
        sibling / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    )
    manifest = TechLeadManifest(
        prs=[PRToReview(number=301, title="Stale", url="https://example/pr/301", branch="s1")]
    )
    manifest_path = sibling / "tech-lead-manifest.json"
    manifest.write(manifest_path)
    (sibling / "manifest.json").write_text(
        json.dumps({"tech_lead_manifest": str(manifest_path)})
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, AddLabelAction) and a.label.startswith("tech-lead-") for a in actions)
    assert "Launch authority rejected" in caplog.text


def test_successful_batch_completion_closes_tracking_issue(tmp_path: Path) -> None:
    """Batch success gives the tracking issue a crash-safe terminal state.

    Open+agent-labeled tracking issues are what startup recovery requeues and
    what _find_existing_tech_lead_issue treats as the active batch (#6768 round
    4). Close is ordered after the PR labels so a mid-apply crash leaves the
    batch open and re-auditable. Success requires the valid decision pair.
    """
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    plant_tech_lead_decision_pair(
        session, candidate_verdicts=pass_verdicts(101, 102)
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    closes = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert [c.issue_number for c in closes] == [session.issue.number]
    label_indexes = [
        i for i, a in enumerate(actions)
        if isinstance(a, AddLabelAction) and a.label == "tech-lead-reviewed"
    ]
    assert label_indexes and actions.index(closes[0]) > max(label_indexes)


def test_batch_completion_with_rejected_pair_does_not_close_tracking_issue(
    tmp_path: Path,
) -> None:
    """A contract violation leaves the batch anchor open for re-audit."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    # No decision pair: rejection path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_successful_batch_without_manifest_still_closes(tmp_path: Path) -> None:
    """An empty batch (no PRs matched) must not anchor future batches forever."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path, with_manifest=False)
    plant_tech_lead_decision_pair(session, comment_targets=())

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert any(isinstance(a, CloseIssueAction) for a in actions)


def test_failure_investigation_completion_preserves_source_issue(
    tmp_path: Path,
) -> None:
    """An investigation's anchor IS the failed work issue - never close it."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_investigation_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "tech-lead-reviewed" not in added_labels(actions)
    assert "tech-lead-failed" not in added_labels(actions)


def _rejections(actions: tuple[object, ...]) -> list[SurfaceTechLeadProposalAction]:
    return [
        action for action in actions
        if isinstance(action, SurfaceTechLeadProposalAction) and action.mode == "rejected"
    ]


class TestFailureInvestigationDiagnosisRequired:
    """A failure investigation must publish its diagnosis to the originating
    issue via a post_comment proposal (#6761 finding 2)."""

    def test_empty_proposed_actions_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_decision_pair(session, comment_targets=())

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "originating issue #1" in rejection.body_preview

    def test_wrong_target_comment_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_decision_pair(session, comment_targets=(42,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview

    def test_correct_target_comment_passes(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_decision_pair(session, comment_targets=(1,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        diagnosis = [
            action for action in actions
            if isinstance(action, AddCommentAction) and action.number == 1
        ]
        assert diagnosis and "Diagnosis" in diagnosis[0].comment


def _plant_decision_with_actions(
    session: Session,
    proposed: list[dict],
    *,
    candidate_verdicts: list[dict[str, object]] | None = None,
) -> None:
    """Write a decision pair with explicit proposed actions (T1 evidence set)."""
    data_dir = session.run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Findings and proposals.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": proposed,
                "candidate_verdicts": candidate_verdicts or [],
            }
        )
    )
    ids = ", ".join(action["id"] for action in proposed)
    (data_dir / "tech-lead-report.md").write_text(
        f"# Report\n\nT1 leads to {ids or 'no actions'}.\n"
    )


class TestDecisionTargetScope:
    """Every targeted proposal must stay inside the immutable launch scope
    (#6761 re-review finding 2) — validated against the authority record,
    never the worktree copies."""

    def _batch(self, tmp_path: Path) -> tuple[Config, Session]:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        return config, session

    def test_in_scope_targets_pass(self, tmp_path: Path) -> None:
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 102,  # manifest PR
                    "body": "Diagnosis.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "escalate_to_human",
                    "target_number": 1,  # anchor tracking issue
                    "body": "Needs a human.",
                },
            ],
            candidate_verdicts=pass_verdicts(101, 102),
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        assert {a.issue_number for a in _tech_lead_labels(actions)} == {101, 102}

    def test_out_of_scope_comment_rejected(self, tmp_path: Path) -> None:
        """A batch comment to a non-manifest PR is a confused-deputy attempt."""
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 999,
                    "body": "Out of scope.",
                }
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "#999" in rejection.body_preview
        assert "outside this session's launch scope" in rejection.body_preview
        assert "tech-lead-reviewed" not in added_labels(actions)
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_out_of_scope_escalation_rejected(self, tmp_path: Path) -> None:
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "escalate_to_human",
                    "target_number": 555,
                    "body": "Escalate elsewhere.",
                }
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview
        # The escalation must not have been planned (no needs-human label).
        assert "needs-human" not in added_labels(actions)

    @pytest.mark.parametrize("act_type", ["reset_retry", "kill_hung_session"])
    def test_out_of_scope_act_level_rejected(
        self, tmp_path: Path, act_type: str
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis.",
                },
                {
                    "id": "A2",
                    "action_type": act_type,
                    "target_number": 777,  # not the focus issue
                    "body": "Rationale.",
                },
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview

    def test_batch_reset_retry_targeting_manifest_pr_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """A batch reset_retry aimed at a manifest PR is a confused deputy.

        The PR number is inside the general launch scope (comments/escalations
        may target manifest PRs), but the issue reset owner would treat it as
        an ``issue_number`` and reset the wrong entity. It is rejected — never
        planned or executed — by both independent axes (#6764 re-review F1,
        #133): a batch review's ROLE may not propose recovery kinds at all,
        which is why the recorded detail names the capability rather than the
        empty act-level target scope that stands behind it.
        """
        config, session = self._batch(tmp_path)
        # Even with execute authority the confused deputy must be blocked.
        config.tech_lead.authority.reset_retry = "execute"
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 101,  # a manifest PR, NOT a work issue
                    "body": "Scratch reset it.",
                    "finding_ids": ["T1"],
                },
            ],
        )

        # Authoritative processing seam rejects the whole completion.
        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error
        assert error is not None
        assert "A1 (reset_retry) is not an action kind" in error
        assert "batch_review" in error

        # Planning surfaces the rejection and plans NO reset / NO success
        # terminalization (no close, no tech-lead-reviewed labels).
        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )
        [rejection] = _rejections(actions)
        assert "is not an action kind" in rejection.body_preview
        assert not any(isinstance(a, ResetRetryIssueAction) for a in actions)
        assert not any(isinstance(a, CloseIssueAction) for a in actions)
        assert "tech-lead-reviewed" not in added_labels(actions)

    def test_health_reset_retry_accepts_snapshot_problem_cohort(
        self, tmp_path: Path
    ) -> None:
        """A health review CAN propose reset_retry for a cohort member.

        Asserted symmetrically with its negative twin: the acceptance criterion
        is that the proposal WORKS, so this checks the typed
        ``ResetRetryIssueAction`` is actually planned for the member — not
        merely that the completion was not rejected.
        """
        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.reset_retry = "execute"
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config, session, problem_issue_numbers=(41, 42, 43)
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 42,
                    "body": "Reset the stuck cohort member.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is None

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        [reset] = [a for a in actions if isinstance(a, ResetRetryIssueAction)]
        assert reset.issue_number == 42
        assert reset.proposal_id == "A1"

    def test_health_reset_retry_rejects_issue_outside_snapshot_cohort(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config, session, problem_issue_numbers=(41, 42, 43)
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 99,
                    "body": "Out-of-cohort reset attempt.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is not None
        assert "immutable problem cohort" in error
        assert "(#41, #42, #43)" in error

    def test_context_only_failures_do_not_widen_health_act_level_scope(
        self, tmp_path: Path
    ) -> None:
        """Unrelated board failures are context, not authority (#6780).

        #99 is on the board because its own investigation is pending. The
        health review can SEE it, but reset_retry'ing it is out of scope —
        before this fix the launch authority absorbed every board failure and
        this completion was accepted.
        """
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config,
            session,
            problem_issue_numbers=(41, 42, 43),
            context_failure_numbers=(99,),
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 99,
                    "body": "Reset an issue this review does not own.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is not None
        assert "immutable problem cohort" in error
        assert "(#41, #42, #43)" in error

    def test_context_only_failures_still_pass_the_tamper_check(
        self, tmp_path: Path
    ) -> None:
        """The tamper check reads the cohort surface, not the failure list.

        A snapshot whose ``recent_failures`` legitimately exceed the grant
        must NOT read as tampering (#6780) — otherwise every storm
        review launched alongside an unrelated pending investigation would
        fail its own completion.
        """
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config,
            session,
            problem_issue_numbers=(41, 42, 43),
            context_failure_numbers=(99,),
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 42,
                    "body": "Reset a cohort member.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is None

    def test_health_snapshot_problem_set_tampering_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Rewriting the snapshot's COHORT surface is still caught."""
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_health_review_session(
            config, session, problem_issue_numbers=(41, 42, 43)
        )
        BoardSnapshot(
            generated_at="2026-07-14T12:01:00+00:00",
            orchestrator_paused=False,
            recent_failures=[
                BoardFailure(99, "Injected problem", "failed", [])
            ],
            problem_cohort=[99],
        ).write(session.run_dir / "tech-lead-data" / "board-snapshot.json")

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is not None
        assert error.startswith("tech_lead_authority: scope_tampered")
        assert "problem set [99]" in error

    def test_duplicate_reset_retry_target_rejects_completion_without_effects(
        self, tmp_path: Path
    ) -> None:
        """One focus issue cannot carry contradictory act-level commands."""
        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.reset_retry = "execute"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis for the originating issue.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "reset_retry",
                    "target_number": 1,
                    "body": "Reset the corrupted worktree.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A3",
                    "action_type": "reset_retry",
                    "target_number": 1,
                    "body": "Retry the same issue from scratch.",
                    "finding_ids": ["T1"],
                },
            ],
        )

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error
        assert error is not None
        assert "multiple act-level proposed actions target #1: A2, A3" in error

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "multiple act-level proposed actions" in rejection.body_preview
        prohibited_effects = (
            ResetRetryIssueAction,
            AddLabelAction,
            AddCommentAction,
            CloseIssueAction,
        )
        assert not any(isinstance(action, prohibited_effects) for action in actions)
        assert removed_labels(actions) == {"in-progress"}


def _capability_probe_actions(
    flavor: TechLeadSessionFlavor, action_type: str
) -> list[dict]:
    """A one-kind decision body valid for *flavor*, with its required company.

    Targets come from what the flavor was armed with below: anchor #1 for every
    flavor, focus #1 for an investigation, cohort member #41 for a health
    review. A failure investigation must always publish its diagnosis, so the
    focus comment rides along when it is not itself the probed kind.
    """
    act_level_target = (
        41 if flavor is TechLeadSessionFlavor.HEALTH_REVIEW else 1
    )
    fields: dict[str, dict] = {
        "post_comment": {"target_number": 1, "body": "Diagnosis."},
        "create_issue": {"title": "Follow-up", "body": "Do the thing."},
        "escalate_to_human": {"target_number": 1, "body": "A human must decide."},
        "flag_pattern": {"body": "Recurring seam.", "pattern_signature": "flaky-ci"},
        "reset_retry": {
            "target_number": act_level_target,
            "body": "Reset the scratch.",
        },
        "kill_hung_session": {
            "target_number": act_level_target,
            "body": "It is genuinely stuck.",
        },
    }
    probe = {"id": "A2", "action_type": action_type, **fields[action_type]}
    if (
        flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        and action_type != "post_comment"
    ):
        return [
            {
                "id": "A1",
                "action_type": "post_comment",
                "target_number": 1,
                "body": "Diagnosis for the originating issue.",
            },
            probe,
        ]
    return [probe]


class TestFlavorActionKindCapabilities:
    """A role may propose only the action kinds its FLAVOR allows (#133).

    The capability axis is checked at the completion contract boundary against
    the orchestrator-owned launch authority, before authority translation or
    effect planning, and independently of target scope.
    """

    def _armed(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor
    ) -> tuple[Config, Session]:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        if flavor is TechLeadSessionFlavor.BATCH_REVIEW:
            arm_batch_session(config, session, tmp_path)
        elif flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
            arm_investigation_session(config, session)
        elif flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION:
            arm_planning_session(config, session)
        else:
            arm_health_review_session(
                config, session, problem_issue_numbers=(41, 42, 43)
            )
        return config, session

    def _processing_error(self, config: Config, session: Session) -> str | None:
        return admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

    @pytest.mark.parametrize(
        ("flavor", "action_type"),
        sorted(
            (flavor, action_type)
            for flavor, kinds in (
                TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds_by_flavor.items()
            )
            for action_type in kinds
        ),
    )
    def test_each_flavor_accepts_every_kind_it_currently_supports(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor, action_type: str
    ) -> None:
        """Regression floor: this leaf narrows nothing that works today.

        Driven off the shipped table (whose contents are pinned against the
        measured semantics in ``tests/unit/domain/test_tech_lead_capabilities``)
        so every granted kind has to survive the real completion boundary, not
        just the policy lookup.
        """
        config, session = self._armed(tmp_path, flavor)
        _plant_decision_with_actions(
            session,
            _capability_probe_actions(flavor, action_type),
            # A batch review owes a verdict for every candidate it was armed
            # with (#345); without them the coverage axis rejects the decision
            # before this test's own axis is reached.
            candidate_verdicts=(
                pass_verdicts(101, 102)
                if flavor is TechLeadSessionFlavor.BATCH_REVIEW
                else None
            ),
        )

        assert self._processing_error(config, session) is None

    @pytest.mark.parametrize("action_type", ["reset_retry", "kill_hung_session"])
    def test_batch_review_may_not_propose_recovery_kinds(
        self, tmp_path: Path, action_type: str
    ) -> None:
        """A batch review does no recovery — and never had a valid target for it.

        Its act-level scope has always been empty, so no such proposal has ever
        been accepted; the capability gate now says so by role, one step before
        the target scope it also fails.
        """
        config, session = self._armed(tmp_path, TechLeadSessionFlavor.BATCH_REVIEW)
        # Execute authority for the kind must not recover a forbidden kind.
        config.tech_lead.authority.reset_retry = "execute"
        _plant_decision_with_actions(
            session,
            _capability_probe_actions(TechLeadSessionFlavor.BATCH_REVIEW, action_type),
        )

        error = self._processing_error(config, session)

        assert error is not None
        assert f"A2 ({action_type}) is not an action kind" in error
        assert "batch_review" in error

    @pytest.mark.parametrize("action_type", ["reset_retry", "kill_hung_session"])
    def test_planning_investigation_may_not_propose_recovery_kinds(
        self, tmp_path: Path, action_type: str
    ) -> None:
        """The bounded flavor's whole point (#136 acceptance 3).

        A planning run's subject is an open issue nobody asked it to recover, so
        its row omits every recovery kind and the proposal dies at the CAPABILITY
        gate — before authority translation and before effect planning. Granting
        the row one of these kinds is what makes this test fail, which is the
        failure direction #136 asks to be provable.
        """
        config, session = self._armed(
            tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION
        )
        # Neither execute authority nor a legitimately-owned focus issue is
        # enough: capability is a separate axis from both.
        config.tech_lead.authority.reset_retry = "execute"
        config.tech_lead.authority.kill_hung_session = "execute"
        _plant_decision_with_actions(
            session,
            _capability_probe_actions(
                TechLeadSessionFlavor.PLANNING_INVESTIGATION, action_type
            ),
        )

        error = self._processing_error(config, session)

        assert error is not None
        assert f"A2 ({action_type}) is not an action kind" in error
        assert "planning_investigation" in error

    def test_planning_investigation_keeps_the_escalation_floor(
        self, tmp_path: Path
    ) -> None:
        """A least-authority role still reaches a human (#136 acceptance 4)."""
        config, session = self._armed(
            tmp_path, TechLeadSessionFlavor.PLANNING_INVESTIGATION
        )
        _plant_decision_with_actions(
            session,
            _capability_probe_actions(
                TechLeadSessionFlavor.PLANNING_INVESTIGATION, "escalate_to_human"
            ),
        )

        assert self._processing_error(config, session) is None

    def test_forbidden_kind_produces_zero_sibling_effects(
        self, tmp_path: Path
    ) -> None:
        """One forbidden action invalidates the decision, siblings included."""
        config, session = self._armed(tmp_path, TechLeadSessionFlavor.BATCH_REVIEW)
        config.tech_lead.authority.reset_retry = "execute"
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 102,  # a manifest PR: perfectly in scope
                    "body": "Audit note the batch review may publish.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "reset_retry",
                    "target_number": 1,
                    "body": "Recovery this role may not propose.",
                    "finding_ids": ["T1"],
                },
            ],
        )

        assert self._processing_error(config, session) is not None

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "A2 (reset_retry) is not an action kind" in rejection.body_preview
        # The allowed sibling published nothing, no reset was planned, and the
        # batch was not terminalized as a success.
        assert not any(isinstance(action, AddCommentAction) for action in actions)
        assert not any(isinstance(action, ResetRetryIssueAction) for action in actions)
        assert not any(isinstance(action, CloseIssueAction) for action in actions)
        assert "tech-lead-reviewed" not in added_labels(actions)

    def test_assignment_claiming_a_wider_role_cannot_widen_allowed_kinds(
        self, tmp_path: Path
    ) -> None:
        """The allowlist is keyed by the ORCHESTRATOR-owned launch flavor.

        The agent rewrites its worktree assignment to a role that may propose
        recovery kinds. That copy never selects the capability set: it is
        tamper evidence against the launch authority, so the completion is
        rejected outright and the reset is never planned.
        """
        config, session = self._armed(tmp_path, TechLeadSessionFlavor.BATCH_REVIEW)
        config.tech_lead.authority.reset_retry = "execute"
        plant_tech_lead_assignment(
            session,
            TechLeadAssignment(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                focus_issue_number=1,
                focus_reason="Claimed by the agent, granted by nobody",
            ),
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "reset_retry",
                    "target_number": 1,
                    "body": "Recovery the launched role may not propose.",
                    "finding_ids": ["T1"],
                }
            ],
        )

        error = self._processing_error(config, session)

        assert error is not None
        assert error.startswith("tech_lead_authority: scope_tampered")

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert not any(isinstance(action, ResetRetryIssueAction) for action in actions)

    def test_allowed_kind_with_out_of_scope_target_still_fails_target_scope(
        self, tmp_path: Path
    ) -> None:
        """The two axes are independent: passing one does not satisfy the other."""
        config, session = self._armed(
            tmp_path, TechLeadSessionFlavor.FAILURE_INVESTIGATION
        )
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis for the originating issue.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "reset_retry",  # a kind this role MAY propose
                    "target_number": 777,  # but not on this issue
                    "body": "Reset something out of scope.",
                    "finding_ids": ["T1"],
                },
            ],
        )

        error = self._processing_error(config, session)

        assert error is not None
        assert "is not an action kind" not in error
        assert "outside this session's launch scope" in error

    def test_allowed_kind_still_flows_into_graduated_authority(
        self, tmp_path: Path
    ) -> None:
        """An allowed kind reaches the existing execute|propose translation."""
        config, session = self._armed(
            tmp_path, TechLeadSessionFlavor.FAILURE_INVESTIGATION
        )
        config.tech_lead.authority.reset_retry = "execute"
        _plant_decision_with_actions(
            session,
            _capability_probe_actions(
                TechLeadSessionFlavor.FAILURE_INVESTIGATION, "reset_retry"
            ),
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        [reset] = [a for a in actions if isinstance(a, ResetRetryIssueAction)]
        assert reset.issue_number == 1
        assert reset.proposal_id == "A2"

    def test_replayed_decision_is_rechecked_at_the_completion_boundary(
        self, tmp_path: Path
    ) -> None:
        """Passing once buys nothing: every entry re-reads the artifact.

        A publish-failure retry re-enters completion processing for the same
        run, so a decision rewritten after its first accepted validation must
        be re-judged, not remembered.
        """
        config, session = self._armed(tmp_path, TechLeadSessionFlavor.BATCH_REVIEW)
        valid_actions = _capability_probe_actions(
            TechLeadSessionFlavor.BATCH_REVIEW, "post_comment"
        )
        verdicts = pass_verdicts(101, 102)
        _plant_decision_with_actions(
            session, valid_actions, candidate_verdicts=verdicts
        )

        assert self._processing_error(config, session) is None

        _plant_decision_with_actions(
            session,
            [
                *valid_actions,
                {
                    "id": "A3",
                    "action_type": "kill_hung_session",
                    "target_number": 1,
                    "body": "Smuggled in after the first pass.",
                },
            ],
            candidate_verdicts=verdicts,
        )

        error = self._processing_error(config, session)

        assert error is not None
        assert "A3 (kill_hung_session) is not an action kind" in error


class TestResetRetryExecutionPipeline:
    """Execute-authority reset_retry proposals flow from the decision
    artifact through planning into the reset owner (#6764 first slice)."""

    def _armed_investigation(
        self, tmp_path: Path, *, authority_mode: str
    ) -> tuple[Config, Session]:
        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.reset_retry = authority_mode
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "reset_retry",
                    "target_number": 1,  # the focus issue
                    "body": "Scratch reset: worktree unrecoverable.",
                    "finding_ids": ["T1"],
                },
            ],
        )
        return config, session

    def test_execute_authority_plans_typed_reset_action(self, tmp_path: Path) -> None:
        config, session = self._armed_investigation(tmp_path, authority_mode="execute")

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        [reset] = [a for a in actions if isinstance(a, ResetRetryIssueAction)]
        assert reset.issue_number == 1
        assert reset.anchor_issue_number == 1
        assert reset.proposal_id == "A2"
        assert reset.finding_ids == ("T1",)
        # No shadow surface for the executed proposal.
        surfaced = [
            a for a in actions
            if isinstance(a, SurfaceTechLeadProposalAction)
            and a.proposal_type == "reset_retry"
        ]
        assert surfaced == []

    def test_propose_authority_plans_gated_proposal_issue(self, tmp_path: Path) -> None:
        """Propose-authority reset_retry is a gated proposal issue carrying
        the stored op (#6778): never a shadow record or a direct execution."""
        from issue_orchestrator.control.actions import (
            CreateTechLeadProposalIssueAction,
        )
        from issue_orchestrator.domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        config, session = self._armed_investigation(tmp_path, authority_mode="propose")

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert not any(type(a) is ResetRetryIssueAction for a in actions)
        assert not any(
            isinstance(a, SurfaceTechLeadProposalAction)
            and a.proposal_type == "reset_retry"
            for a in actions
        )
        [proposal] = [
            a for a in actions if isinstance(a, CreateTechLeadProposalIssueAction)
        ]
        assert proposal.op.op_type == "reset_retry"
        assert proposal.op.target_issue_number == 1
        assert proposal.op.source_action_id == "A2"
        assert proposal.op.source_run_id == session.run_assets.run_id
        assert proposal.op.source_session_name == session.run_assets.session_name
        assert PROPOSED_TECH_LEAD_LABEL in proposal.labels

    def test_full_pipeline_invokes_reset_owner(self, tmp_path: Path) -> None:
        """Completed investigation + execute authority -> the reset owner is
        invoked through planner -> applier with the target's fresh labels."""
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.tech_lead_reset_retry import (
            ResetRetryRunOutcome,
            TechLeadResetRetryExecutor,
        )
        from issue_orchestrator.domain.models import Issue as DomainIssue

        config, session = self._armed_investigation(tmp_path, authority_mode="execute")
        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        run_reset = MagicMock(
            return_value=ResetRetryRunOutcome(success=True, details={"queued_now": True})
        )
        executor = TechLeadResetRetryExecutor(
            events=MagicMock(),
            label_manager=LabelManager(config),
            read_issue=lambda number: DomainIssue(
                number=number,
                title="Focus issue",
                labels=["agent:test", "blocked-failed"],
                repo="owner/repo",
            ),
            has_active_issue_runtime=lambda _number: False,
            run_reset=run_reset,
        )
        applier = ActionApplier(
            labels=MagicMock(),
            sessions=MagicMock(),
            events=MagicMock(),
            repository_host=MagicMock(),
        )
        applier.tech_lead_reset_retry = executor

        results = applier.apply_all(list(actions))

        run_reset.assert_called_once_with(1, ["agent:test", "blocked-failed"])
        assert all(r.result_type is not None for r in results)


class TestLaunchScopeTamperResistance:
    """The agent-writable worktree copies carry zero authority (#6761
    re-review finding 1)."""

    def test_flavor_flip_after_launch_fails_session(self, tmp_path: Path) -> None:
        """The reviewer's repro: an investigation flipping its worktree
        assignment to batch_review must fail, never close the originating
        issue."""
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        # Agent tampering: rewrite the worktree copy to the batch flavor.
        plant_tech_lead_assignment(
            session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        )
        plant_tech_lead_decision_pair(session, comment_targets=())

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error
        assert error is not None and error.startswith(
            "tech_lead_authority: scope_tampered"
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED, processing_errors=[error]
        )

        assert not any(isinstance(a, CloseIssueAction) for a in actions)
        assert "tech-lead-reviewed" not in added_labels(actions)
        assert "blocked-failed" in added_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number

    def test_deleted_assignment_fails_session(self, tmp_path: Path) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        assignment_path = (
            session.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        )
        assignment_path.unlink()

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is not None and error.startswith(
            "tech_lead_authority: scope_tampered"
        )
        assert "missing" in error

    def test_tampered_manifest_prs_never_get_labels(self, tmp_path: Path) -> None:
        """Substituted PR numbers in the worktree manifest must not be
        labeled — labels come from the authority set, and the divergence
        itself fails the session."""
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        # Agent tampering: substitute the manifest PR set.
        tampered = TechLeadManifest(
            prs=[PRToReview(number=999, title="Sub", url="https://x/999", branch="s")]
        )
        manifest_path = tmp_path / "tech-lead-manifest.json"
        tampered.write(manifest_path)

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error
        assert error is not None and error.startswith(
            "tech_lead_authority: scope_tampered"
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED, processing_errors=[error]
        )

        assert not any(
            isinstance(a, AddLabelAction) and a.issue_number == 999 for a in actions
        )
        # The authority set still records the failure on the REAL PRs.
        assert {a.issue_number for a in _tech_lead_failed_labels(actions)} == {101, 102}

    def test_missing_authority_is_critical_in_processing_path(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        plant_tech_lead_assignment(
            session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        )

        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error

        assert error is not None and error.startswith(
            "tech_lead_authority: missing_authority"
        )


def test_protected_agent_label_on_create_issue_rejects_decision(
    tmp_path: Path,
) -> None:
    """Untrusted agent labels may not touch workflow truth (#6761 finding 4)."""
    config = make_tech_lead_config(tmp_path)
    session = make_tech_lead_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    _plant_decision_with_actions(
        session,
        [
            {
                "id": "A1",
                "action_type": "create_issue",
                "title": "Follow-up",
                "body": "Fix it.",
                "labels": ["in-progress"],
                "finding_ids": ["T1"],
            }
        ],
        candidate_verdicts=pass_verdicts(101, 102),
    )

    actions = make_planner(config).generate_completion_actions(
        session, SessionStatus.COMPLETED
    )

    [rejection] = _rejections(actions)
    assert "protected" in rejection.body_preview
    assert "in-progress" in rejection.body_preview
    assert not any(
        isinstance(a, AddLabelAction) and a.label == "tech-lead-reviewed" for a in actions
    )


class TestTechLeadDecisionFailureTransition:
    """A rejected pair rides the critical-error seam: FAILED history plus the
    blocked/failed labeling path for the session's own issue (#6761 finding 3)."""

    ERROR = "tech_lead_decision: contract_violation: finding T1 has no evidence"

    @pytest.mark.parametrize("prefix", TECH_LEAD_ERROR_PREFIXES)
    def test_every_refusal_the_owner_issues_is_critical(self, prefix: str) -> None:
        """Driven from the owner tuple, not from a list written out here.

        #385 round 1 F1: the owner gained a third refusal and this classifier
        still named two of them inline, so a refused completion validation
        matched no branch, was appended to neither list, and settled as an
        ordinary success. Parametrizing over the owner is what makes the next
        prefix impossible to add without reaching this path.
        """
        error = f"{prefix}: some_failure: some detail"

        critical, downgraded = critical_processing_errors([error])

        assert critical == [error]
        assert downgraded == []

    def test_batch_flavor_fails_manifest_and_blocks_own_issue(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        failed = [
            a for a in actions
            if isinstance(a, AddLabelAction) and a.label == "tech-lead-failed"
        ]
        assert {a.issue_number for a in failed} == {101, 102}
        assert "blocked-failed" in added_labels(actions)
        assert "publish-failed" not in added_labels(actions)
        assert "in-progress" in removed_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert "finding T1 has no evidence" in rejection.body_preview
        assert any(
            "Tech Lead completion rejected" in comment for comment in comments(actions)
        )
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_investigation_flavor_blocks_own_issue_without_manifest_labels(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        plant_tech_lead_manifest(tmp_path, session)  # planted noise: must stay unread

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        assert "tech-lead-failed" not in added_labels(actions)
        assert "blocked-failed" in added_labels(actions)
        assert "in-progress" in removed_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert any(
            "Tech Lead completion rejected" in comment for comment in comments(actions)
        )

    def test_a_rejected_planning_run_never_blocks_its_healthy_subject(
        self, tmp_path: Path
    ) -> None:
        """The bounded role's DESIGNED failure route (#136 review F2).

        End-to-end through the real capability gate: the agent proposes
        ``reset_retry``, the #133 table refuses the whole decision, and the
        rejection lands here. Blocking the subject would let the role that may
        not PROPOSE a recovery action cause one by proposing it — taking an
        open, unblocked issue (admission accepted it for exactly that reason)
        off the board and making it eligible for the failure investigation this
        role was built not to be able to invoke.

        Everything that makes the rejection visible stays: the surfaced
        rejection, the operator comment, the released claim.
        """
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_planning_session(config, session)
        labels = LabelManager(config)
        _plant_decision_with_actions(
            session,
            _capability_probe_actions(
                TechLeadSessionFlavor.PLANNING_INVESTIGATION, "reset_retry"
            ),
        )
        error = admit_tech_lead_completion(
            config,
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        ).error
        assert error is not None and "reset_retry" in error

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[error],
        )

        assert labels.blocked_failed not in added_labels(actions)
        assert labels.needs_human not in added_labels(actions)
        assert "tech-lead-failed" not in added_labels(actions)
        # ...and the rejection is still fully surfaced.
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert labels.in_progress in removed_labels(actions)
        assert any(
            "Tech Lead completion rejected" in comment for comment in comments(actions)
        )
        assert any(
            "no recovery authority" in comment for comment in comments(actions)
        )


class TestARefusedCompletionValidationProjectsAFailedRun:
    """The other half of #385's R3, on the side of the boundary the gate's own
    tests stopped short of.

    ``process_completion`` refusing is only half the promise: it buys zero
    push/PR/comment. What an operator and every downstream consumer actually
    read is the PLANNED disposition and the history row, and in round 1 both
    said the run completed cleanly — the claim label was released, no
    ``tech-lead-failed`` was applied, and nothing was surfaced on the anchor —
    because the refusal's prefix was missing from the critical set.
    """

    ERROR = (
        "tech_lead_completion_validation: validation_failed: "
        "uncommitted content (dirty_check='tracked'): src/thing.py"
    )

    def test_the_refusal_routes_to_the_tech_lead_failure_owner(
        self, tmp_path: Path
    ) -> None:
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        failed = [
            a for a in actions
            if isinstance(a, AddLabelAction) and a.label == "tech-lead-failed"
        ]
        assert {a.issue_number for a in failed} == {101, 102}
        assert "blocked-failed" in added_labels(actions)
        # The tech_lead owner's route, not the generic publish-failure lane.
        assert "publish-failed" not in added_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert "uncommitted content" in rejection.body_preview
        assert any(
            "Tech Lead completion rejected" in comment for comment in comments(actions)
        )

    def test_the_claim_is_released_as_a_refusal_not_as_a_success(
        self, tmp_path: Path
    ) -> None:
        """The round-1 symptom, pinned where the two paths actually differ.

        Both dispositions release ``in-progress``; what round 1 produced was
        the *success* release with nothing else attached, which is
        indistinguishable from a clean run. The release must be the refusal's.
        """
        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_batch_session(config, session, tmp_path)

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        releases = [
            a for a in actions
            if isinstance(a, RemoveLabelAction) and a.label == "in-progress"
        ]
        assert [a.reason for a in releases] == [
            "Tech Lead completion rejected - releasing claim"
        ]
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_history_records_the_run_as_failed(self) -> None:
        history = resolve_history_status(
            status=SessionStatus.COMPLETED,
            issue_number=1,
            pr_url=None,
            processing_errors=[self.ERROR],
            review_exchange_halted=False,
            completion_detail=None,
        )

        assert history.status is SessionStatus.FAILED
        assert history.reason is not None

    def test_a_pr_url_cannot_downgrade_the_refusal(self) -> None:
        """No "but it landed anyway" evidence exists for an ungated completion.

        A discoverable PR downgrades ``create_pr``; it says nothing about
        whether the mandatory validation cleared this candidate, so it must not
        soften this prefix.
        """
        critical, downgraded = critical_processing_errors(
            [self.ERROR], pr_url="https://example.test/owner/repo/pull/7"
        )

        assert critical == [self.ERROR]
        assert downgraded == []


def test_interrupted_retry_adds_guard_and_keeps_retry_loop_bounded(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = True
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert config.retry.interrupted_sessions.coding_guard_label in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Interrupted" in comment for comment in comments(actions))


def test_create_pr_error_is_downgraded_when_pr_exists(caplog) -> None:
    critical, downgraded = critical_processing_errors(
        [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"],
        pr_url="https://github.com/owner/repo/pull/5",
        issue_number=5,
        log_downgraded=True,
        context="test",
    )

    assert critical == []
    assert downgraded == [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"]
    assert "Ignoring non-blocking create_pr processing errors" in caplog.text


class TestProductionOpenIssueDedupCorpus:
    """Completion reads the SQL-backed corpus; planning performs no GitHub read."""

    @staticmethod
    def _plant_duplicate_pair(session: Session) -> None:
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis for the focus issue.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "create_issue",
                    "title": "Stabilize flaky CI runner",
                    "body": "Runner disconnects mid-build.",
                    "duplicate_of": 1,
                    "finding_ids": ["T1"],
                },
            ],
        )

    def test_execute_routes_verified_open_issue_duplicate_to_comment(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.domain.open_issue_corpus import (
            build_open_issue_fingerprint,
        )

        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_duplicate_pair(session)
        store = SqliteOpenIssueCorpusStore.for_repo(tmp_path)
        store.replace_all(
            (
                build_open_issue_fingerprint(
                    1, "Stabilize flaky CI runner", "Runner disconnects mid-build."
                ),
            ),
            watermark="2026-07-23T12:00:00Z",
        )
        repository_host = MagicMock(spec=RepositoryHost)

        actions = make_planner(
            config,
            repository_host=repository_host,
            open_issue_corpus_store=store,
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

        repository_host.list_issues.assert_not_called()
        repository_host.list_issues_delta.assert_not_called()
        assert not any(
            isinstance(action, CreateTechLeadIssueAction) for action in actions
        )
        assert any(
            isinstance(action, AddCommentAction)
            and action.number == 1
            and "deduplicated follow-up" in action.comment
            for action in actions
        )

    def test_propose_gates_verified_open_issue_duplicate(
        self, tmp_path: Path
    ) -> None:
        from issue_orchestrator.domain.open_issue_corpus import (
            build_open_issue_fingerprint,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            PROPOSED_TECH_LEAD_LABEL,
        )

        config = make_tech_lead_config(tmp_path)
        config.tech_lead.authority.create_issue = "propose"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_duplicate_pair(session)
        store = SqliteOpenIssueCorpusStore.for_repo(tmp_path)
        store.replace_all(
            (
                build_open_issue_fingerprint(
                    1, "Stabilize flaky CI runner", "Runner disconnects mid-build."
                ),
            ),
            watermark="2026-07-23T12:00:00Z",
        )

        actions = make_planner(
            config,
            open_issue_corpus_store=store,
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

        [gated] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert PROPOSED_TECH_LEAD_LABEL in gated.labels
        assert "DUPLICATE of #1" in gated.body
        assert not any(
            isinstance(action, AddCommentAction)
            and "deduplicated follow-up" in action.comment
            for action in actions
        )


class TestMilestoneResolutionBoundary:
    """The completion seam plans milestone INTENT only (#6769 finding 4).

    Under ``create_issue: propose`` (shadow) the decision must complete with
    ZERO GitHub reads — the reviewer reproduced a ``list_milestones`` lookup
    failure failing the completion for an issue that would never be created.
    Under execute, the name still travels as intent; the applier resolves it.
    """

    def _plant_pair_with_create_issue(self, session: Session) -> None:
        data_dir = session.run_dir / "tech-lead-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "tech-lead-decision.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": "One systemic pattern found.",
                    "findings": [
                        {
                            "id": "T1",
                            "title": "Flaky CI",
                            "classification": "infra",
                            "evidence": ["orchestrator log lines 10-20"],
                        }
                    ],
                    "proposed_actions": [
                        {
                            "id": "A1",
                            "action_type": "post_comment",
                            "target_number": 1,
                            "body": "Diagnosis for #1: flaky CI.",
                            "finding_ids": ["T1"],
                        },
                        {
                            "id": "A2",
                            "action_type": "create_issue",
                            "title": "Stabilize CI runner",
                            "body": "Runner disconnects mid-build.",
                            "labels": ["bug"],
                            "finding_ids": ["T1"],
                        },
                    ],
                }
            )
        )
        (data_dir / "tech-lead-report.md").write_text(
            "# Report\n\nFinding T1: flaky CI.\n\nProposals: A1, A2.\n"
        )

    def _completed_actions(self, config: Config, session: Session, host) -> tuple:
        return make_planner(
            config, repository_host=cast(RepositoryHost, host)
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

    def test_gated_create_issue_with_explicit_milestone_makes_zero_reads(
        self, tmp_path: Path
    ) -> None:
        """Propose-authority create_issue is a GATED creation now (#6778):
        it still plans milestone INTENT with zero GitHub reads, and the
        planned issue carries the proposed-tech-lead gate label."""
        from unittest.mock import MagicMock

        from issue_orchestrator.domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        config = make_tech_lead_config(tmp_path)
        config.tech_lead.milestone_strategy.explicit = "M5"
        config.tech_lead.authority.create_issue = "propose"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_pair_with_create_issue(session)
        host = MagicMock()

        actions = self._completed_actions(config, session, host)

        host.list_milestones.assert_not_called()
        assert not any(
            isinstance(action, SurfaceTechLeadProposalAction)
            and action.proposal_type == "create_issue"
            for action in actions
        )
        [create] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert PROPOSED_TECH_LEAD_LABEL in create.labels

    def test_execute_create_issue_plans_name_intent_without_reads(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.actions import TechLeadMilestoneIntent

        config = make_tech_lead_config(tmp_path)
        config.tech_lead.milestone_strategy.explicit = "M5"
        assert config.tech_lead.authority.mode_for("create_issue") == "execute"
        session = make_tech_lead_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_pair_with_create_issue(session)
        host = MagicMock()

        actions = self._completed_actions(config, session, host)

        host.list_milestones.assert_not_called()
        [create] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert create.milestone == TechLeadMilestoneIntent(explicit_name="M5")

    def test_a_planning_run_still_settles_its_authorized_create_issue(
        self, tmp_path: Path
    ) -> None:
        """The zero-code lane suppresses PUBLICATION intent, nothing else (#202).

        A planning run's authorized ``create_issue`` settles through the same
        owner it always did — the effect planner reads the decision artifact,
        which the completion path neither consumes nor rewrites.
        """
        from unittest.mock import MagicMock

        config = make_tech_lead_config(tmp_path)
        session = make_tech_lead_session(tmp_path)
        arm_planning_session(config, session)
        self._plant_pair_with_create_issue(session)

        actions = self._completed_actions(config, session, MagicMock())

        [create] = [
            action for action in actions if isinstance(action, CreateTechLeadIssueAction)
        ]
        assert create.title == "Stabilize CI runner"


def test_provider_blocked_rework_restores_its_needs_rework_trigger(
    tmp_path: Path,
) -> None:
    """The durable half of returning a provider-killed rework (#6999 F2).

    The rework launcher strips ``needs-rework`` when the session starts. The
    in-memory queue restore (``InFlightWorkLedger``) covers this process; the
    label is what an orchestrator restarted during the outage would read, so a
    credential outage must not leave a PR that asked for rework no longer
    saying so.
    """
    from issue_orchestrator.ports.provider_resilience import ProviderErrorType

    config = Config()
    session = make_session(tmp_path, terminal_id="rework-1")
    session.pr_number = 70

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.BLOCKED,
        provider_error_type=ProviderErrorType.AUTH,
    )

    restored = [
        action
        for action in actions
        if isinstance(action, AddLabelAction)
        and action.label == LabelManager(config).needs_rework
    ]
    assert len(restored) == 1
    # On the PR, which is where the launcher removed it from.
    assert restored[0].issue_number == 70


def test_provider_blocked_issue_session_adds_no_rework_trigger(
    tmp_path: Path,
) -> None:
    """Restoring the trigger is scoped to rework sessions, nothing wider."""
    from issue_orchestrator.ports.provider_resilience import ProviderErrorType

    config = Config()

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path, terminal_id="issue-1"),
        SessionStatus.BLOCKED,
        provider_error_type=ProviderErrorType.AUTH,
    )

    assert LabelManager(config).needs_rework not in added_labels(actions)
    # The claim is still released, exactly as before.
    assert "in-progress" in removed_labels(actions)


class TestAResultOnlyRunReachesATerminalDisposition:
    """A finished run with no pull request must leave the schedulable pool (#337).

    An ordinary success is safe to release from ``in-progress`` only because
    ``pr-pending`` takes the claim over and the eventual merge closes the issue.
    A run the completion settlement PROVED offers no code candidate opens no
    pull request, so neither half happens: released and unlabelled, the issue is
    indistinguishable to ``Scheduler`` from work never started, and the next tick
    launches the same measurement, runs the same review exchange, and posts a
    second RESULT — every tick, unbounded.

    The pre-#337 behaviour of the same run was a bounded publish FAILURE. These
    pin that the repair did not trade a bounded stop for an unbounded repeat.
    """

    @staticmethod
    def _closes(actions: tuple[object, ...]) -> list[CloseIssueAction]:
        return [a for a in actions if isinstance(a, CloseIssueAction)]

    def test_a_settled_result_only_run_closes_its_issue(self, tmp_path: Path) -> None:
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        closes = self._closes(actions)
        assert len(closes) == 1
        assert closes[0].issue_number == 1
        assert "in-progress" in removed_labels(actions)

    def test_the_close_is_planned_as_the_gate_for_the_release(
        self, tmp_path: Path
    ) -> None:
        """Ordering alone is not fail-stop; the close must be a GATE (#337 r3 F1).

        ``ActionApplier`` reports a failed close as a FAILED result and applies
        the rest of the batch, so a close ordered first and then failing would
        still be followed by the release of ``in-progress`` — an open, finished,
        unclaimed issue, exactly the state the ordering was meant to prevent.
        The typed action is what the completion gate withholds the remainder on.
        """
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert isinstance(actions[0], ResultOnlyCloseIssueAction)
        assert is_completion_gate_action(actions[0])
        releases = [
            i
            for i, a in enumerate(actions)
            if isinstance(a, RemoveLabelAction) and a.label == "in-progress"
        ]
        assert releases and releases[0] > 0
        assert not any(is_completion_gate_action(actions[i]) for i in releases)

    def test_the_close_explains_why_no_pull_request_exists(
        self, tmp_path: Path
    ) -> None:
        """The one fact an operator cannot get from the issue itself."""
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert "no pull request" in self._closes(actions)[0].comment

    def test_the_close_carries_the_reconciliation_expectation(
        self, tmp_path: Path
    ) -> None:
        """A mutating action with no expectation is refused by the applier."""
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert self._closes(actions)[0].expected is not None

    def test_an_ordinary_completion_is_never_closed(self, tmp_path: Path) -> None:
        """The negative control: a PR-carried run keeps today's lifecycle."""
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=observed_pull_request(),
        )

        assert self._closes(actions) == []
        assert "in-progress" in removed_labels(actions)

    def test_a_completion_whose_push_failed_is_never_closed(
        self, tmp_path: Path
    ) -> None:
        """A missing PR is not proof of a result-only run.

        A publish that FAILED is also missing its pr_url, and that run needs the
        bounded publish-failure routing — not a terminal close. Only the carried
        settlement opens this path, so a failure that (impossibly) arrived
        carrying one still takes the critical-error branch.
        """
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            processing_errors=[f"{ERROR_PREFIX_PUSH}: remote rejected"],
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert self._closes(actions) == []

    def test_a_halted_review_exchange_is_never_closed(self, tmp_path: Path) -> None:
        """Review cannot be bypassed into a terminal disposition either."""
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            review_exchange_halted=True,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert self._closes(actions) == []

    @pytest.mark.parametrize(
        "status",
        [
            SessionStatus.FAILED,
            SessionStatus.TIMED_OUT,
            SessionStatus.BLOCKED,
            SessionStatus.NEEDS_HUMAN,
        ],
    )
    def test_only_a_completed_run_can_reach_the_disposition(
        self, tmp_path: Path, status: SessionStatus
    ) -> None:
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            status,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert self._closes(actions) == []


class TestAnUndeliveredResultIsBoundedNotClosed:
    """A settled run whose comment never landed takes the bounded failure path.

    Withdrawing the terminal disposition alone would put the finished issue
    straight back in the schedulable pool — the round-1 F1 loop, on a failure
    path. On the zero-code lane the comment IS the publication, so its loss is
    a publish failure: counted by ``publish-fail-count-N`` and escalated to
    ``needs-human``, which is the bounded, fail-closed behaviour #336 recorded
    for this same run before the lane existed.
    """

    UNDELIVERED = (
        f"{ERROR_PREFIX_RESULT_UNDELIVERED}: the run for issue #1 offered no"
        " code candidate, so its issue comment was its whole delivery, and"
        " that comment did not reach the issue; nothing was published"
    )

    def _actions(self, tmp_path: Path) -> tuple[object, ...]:
        return make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            processing_errors=[self.UNDELIVERED],
            pull_request=NO_PULL_REQUEST,
            # The settlement is already withdrawn upstream; passing it settled
            # here proves the routing does not depend on that withdrawal alone.
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

    def test_the_issue_is_not_closed(self, tmp_path: Path) -> None:
        actions = self._actions(tmp_path)

        assert [a for a in actions if isinstance(a, CloseIssueAction)] == []

    def test_the_run_is_counted_against_the_publish_failure_budget(
        self, tmp_path: Path
    ) -> None:
        """Bounded: the counter is what eventually escalates to needs-human."""
        actions = self._actions(tmp_path)

        assert any(
            label.startswith("publish-fail-count-") for label in added_labels(actions)
        )
        assert "publish-failed" in added_labels(actions)

    def test_the_error_is_classified_critical(self) -> None:
        """The classification is what routes it; pinned at its own owner."""
        critical, downgraded = critical_processing_errors([self.UNDELIVERED])

        assert critical == [self.UNDELIVERED]
        assert downgraded == []

    def test_a_pr_url_cannot_downgrade_it(self) -> None:
        """Unlike create_pr, there is no "but it landed anyway" evidence.

        A create_pr error is downgraded when reconciliation finds a PR. Nothing
        can find a comment that was never posted, so this prefix is critical
        unconditionally.
        """
        critical, _downgraded = critical_processing_errors(
            [self.UNDELIVERED], pr_url="https://example.test/owner/repo/pull/7"
        )

        assert critical == [self.UNDELIVERED]


class TestTheDispositionRefusesAnIssueWithWorkInFlight:
    """Fact 5 proves "this RUN produced no code", not "nothing is in flight".

    The two come apart in a shape this repository has seen: a rework worktree
    that arrives reset to the base, its pull request's commits reachable only
    from the remote branch. Such a run has a clean tree, sits at the base, and
    adds no commit — every fact the lane proves — and closing its issue would
    close one whose pull request is open and unmerged.
    """

    def test_a_settled_run_with_an_open_pull_request_is_not_closed(
        self, tmp_path: Path
    ) -> None:
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=observed_pull_request(),
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert [a for a in actions if isinstance(a, CloseIssueAction)] == []
        assert "in-progress" in removed_labels(actions)

    def test_an_unreadable_pull_request_lookup_refuses_the_close(
        self, tmp_path: Path
    ) -> None:
        """UNKNOWN must not mean OBSERVED_NONE (#337 round 3, F2).

        The review/rework fallback reads ``get_pr(session.pr_number)`` for a
        session that is KNOWN to carry a pull request. When that read raises,
        the old ``str | None`` collapsed the failure to "there is no pull
        request" — the one input that authorises the close.
        """
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=PullRequestObservation.unknown(
                "pull request #7 for this session could not be read: 502"
            ),
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert [a for a in actions if isinstance(a, CloseIssueAction)] == []
        assert "in-progress" in removed_labels(actions)

    def test_a_caller_that_never_looked_refuses_the_close(
        self, tmp_path: Path
    ) -> None:
        """The default is the fail-closed verdict, not an observed absence."""
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert [a for a in actions if isinstance(a, CloseIssueAction)] == []

    def test_the_genuine_evidence_run_still_closes(self, tmp_path: Path) -> None:
        """The falsification control: an OBSERVED absence is the lane's premise."""
        actions = make_planner(Config()).generate_completion_actions(
            make_session(tmp_path),
            SessionStatus.COMPLETED,
            pull_request=NO_PULL_REQUEST,
            result_only=ResultOnlyDelivery.settled("adds no commit over origin/main"),
        )

        assert [a for a in actions if isinstance(a, CloseIssueAction)]
