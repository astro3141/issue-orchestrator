"""A planning-created proposal stays UNSCHEDULED until Human approval (#332).

#323 is the live evidence this file exists for: a proposal a real
``planning_investigation`` produced carried ``proposed-tech-lead`` AND
``agent:backend`` at once, projecting "Human approval pending" and "Actor
scheduling" simultaneously. The invariant these tests pin is that the two states
can never coexist:

    a planning-created executable proposal that is still pending Human approval
    is unscheduled.

Both sides of the boundary are covered, per the repo's command-surface rule:

* producer — the decision -> action planner composes the proposal's labels;
* effect  — the create-issue applier projects exactly those labels onto the
  repository host, and nothing re-adds a scheduler label on the way;
* consumer — the discovery owner (``FactGatherer.fetch_issues``) and the
  admission owner (``Scheduler.evaluate_issues``) both refuse it while pending,
  and admit it only after the explicit two-part Human act.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from issue_orchestrator.control.actions import CreateTechLeadIssueAction
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.proposal_dedup_gate import (
    DuplicateTargetGrant,
    OpenIssueCorpus,
)
from issue_orchestrator.control.scheduler import AvailabilityReason, Scheduler
from issue_orchestrator.control.tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
)
from issue_orchestrator.control.tech_lead_decision_contract import (
    validate_decision_for_authority,
)
from issue_orchestrator.control.tech_lead_issue_creation import (
    apply_create_tech_lead_issue,
)
from issue_orchestrator.control.tech_lead_issue_policy import (
    TechLeadIssueAdmission,
    is_scheduler_projection_label,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_artifacts import (
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
)
from issue_orchestrator.domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config

ANCHOR = 99
WORKER = "agent:backend"
EXPECTED = build_expected_for_mutation()
SOURCE_RUN = {
    "source_run_id": "run-1",
    "source_session_name": "issue-99",
    "observed_at": "2026-08-28T00:00:00+00:00",
}
# The exact fixture the issue's falsification section names: two informational
# labels and one hostile/erroneous scheduler label.
REQUESTED_LABELS = ("bug", "area:example", WORKER)


def _config() -> Config:
    config = Config()
    config.agents = {WORKER: Mock()}
    config.tech_lead_follow_up_agent = WORKER
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


def _anchor(labels: list[str] | None = None) -> Issue:
    return Issue(
        number=ANCHOR,
        title="Prepare one bounded issue",
        labels=labels if labels is not None else ["agent:tech-lead"],
        repo="owner/repo",
    )


def _decision(labels: tuple[str, ...] = REQUESTED_LABELS) -> TechLeadDecision:
    return TechLeadDecision(
        summary="one bounded proposal",
        findings=(
            TechLeadFinding(
                id="T1",
                title="Finding",
                classification="infra",
                evidence=("orchestrator log lines 10-20",),
            ),
        ),
        proposed_actions=(
            ProposedTechLeadAction(
                id="A1",
                action_type="create_issue",
                title="Bounded follow-up",
                body="What to implement.",
                labels=labels,
                finding_ids=("T1",),
            ),
        ),
    )


def _plan(
    *,
    flavor: TechLeadSessionFlavor,
    config: Config | None = None,
    labels: tuple[str, ...] = REQUESTED_LABELS,
    anchor: Issue | None = None,
) -> CreateTechLeadIssueAction:
    config = config or _config()
    planned = plan_tech_lead_decision_actions(
        _decision(labels),
        config,
        LabelManager(config),
        anchor_issue=anchor or _anchor(),
        expected=EXPECTED,
        flavor=flavor,
        op_ledger={},
        pattern_ledger={},
        active_session_run_id=lambda _n: None,
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
        **SOURCE_RUN,
    )
    [creation] = [a for a in planned if isinstance(a, CreateTechLeadIssueAction)]
    return creation


def _scheduler_labels(labels, config: Config) -> list[str]:
    return [
        label
        for label in labels
        if is_scheduler_projection_label(label, config=config)
    ]


class _RecordingHost:
    """Deterministic disposable GitHub boundary (never #323)."""

    def __init__(self, issue_number: int = 400) -> None:
        self.issue_number = issue_number
        self.created: list[dict] = []

    def list_labels(self) -> list[dict]:
        return [{"name": PROPOSED_TECH_LEAD_LABEL}]

    def list_milestones(self, _state: str) -> list[dict]:
        return []

    def create_issue(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {"number": self.issue_number}


class _DiscoveryHost:
    """A repository host whose label query has GitHub's AND semantics."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
        *,
        exhaustive: bool = False,
    ) -> list[Issue]:
        wanted = {name.casefold() for name in (labels or [])}
        return [
            issue
            for issue in self.issues
            if wanted <= {name.casefold() for name in issue.labels}
        ]


def _proposal_issue(labels) -> Issue:
    return Issue(
        number=400,
        title="Bounded follow-up",
        labels=list(labels),
        repo="owner/repo",
    )


# --------------------------------------------------------------------------- #
# A. Planning proposal with a hostile/erroneous scheduler label                #
# --------------------------------------------------------------------------- #


class TestPlanningProposalWithSchedulerLabel:
    def test_proposal_is_created_and_keeps_informational_labels(self) -> None:
        creation = _plan(flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        assert creation.title == "Bounded follow-up"
        assert "bug" in creation.labels
        assert "area:example" in creation.labels

    def test_pending_human_gate_is_projected(self) -> None:
        creation = _plan(flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        assert PROPOSED_TECH_LEAD_LABEL in creation.labels

    def test_no_scheduler_projection_survives(self) -> None:
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )

        assert WORKER not in creation.labels
        assert _scheduler_labels(creation.labels, config) == []
        # Not merely "the exact string is gone": no agent-family label at all.
        assert not [
            label for label in creation.labels if label.casefold().startswith("agent:")
        ]

    def test_case_flipped_scheduler_label_is_withheld_too(self) -> None:
        """GitHub folds label names; case-flipping must not launder a request."""
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            config=config,
            labels=("bug", "AGENT:Backend"),
        )

        assert _scheduler_labels(creation.labels, config) == []
        assert "bug" in creation.labels

    def test_withheld_label_is_reported_to_the_approver(self) -> None:
        """No silent degradation: the approver sees what was asked for."""
        creation = _plan(flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        assert WORKER in creation.body
        assert "withheld" in creation.body.lower()
        assert "UNSCHEDULED" in creation.body

    def test_configured_execute_authority_cannot_ungate_a_planning_proposal(
        self,
    ) -> None:
        """Approval is explicit: no config value fabricates the Human act."""
        config = _config()
        config.tech_lead.authority.create_issue = "execute"

        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )

        assert PROPOSED_TECH_LEAD_LABEL in creation.labels
        assert _scheduler_labels(creation.labels, config) == []

    def test_inherited_scheduler_label_is_withheld_too(self) -> None:
        """The anchor is a tech-lead issue; inheritance must not smuggle its
        agent label onto a pending proposal."""
        config = _config()
        config.tech_lead.inherit_labels = ["agent:tech-lead"]

        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            config=config,
            anchor=_anchor(["agent:tech-lead"]),
        )

        assert _scheduler_labels(creation.labels, config) == []

    def test_the_decision_is_admitted_rather_than_rejected_wholesale(self) -> None:
        """Proof A requires 'created proposal: yes' for the hostile fixture, so
        the contract must not reject the whole planning decision over a label
        the creation boundary was always going to withhold."""
        config = _config()

        detail = validate_decision_for_authority(
            _decision(),
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                anchor_issue_number=ANCHOR,
                focus_issue_number=ANCHOR,
            ),
            config=config,
            labels=LabelManager(config),
        )

        assert detail is None

    def test_other_roles_still_reject_a_protected_label_outright(self) -> None:
        """Regression scope: only the planning-proposal boundary changes."""
        config = _config()

        detail = validate_decision_for_authority(
            _decision(),
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
                anchor_issue_number=ANCHOR,
            ),
            config=config,
            labels=LabelManager(config),
        )

        assert detail is not None
        assert WORKER in detail


# --------------------------------------------------------------------------- #
# The effect boundary: what actually reaches the repository host               #
# --------------------------------------------------------------------------- #


class TestPlanningProposalEffect:
    def test_applier_projects_exactly_the_unscheduled_label_set(self) -> None:
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )
        host = _RecordingHost()

        result = apply_create_tech_lead_issue(
            creation,
            repository_host=host,
            events=Mock(),
            ops=None,
            add_comment=lambda number, comment: "https://example/comment",
            emit_labels_changed=lambda *args: None,
        )

        assert result.success
        [created] = host.created
        assert PROPOSED_TECH_LEAD_LABEL in created["labels"]
        assert "bug" in created["labels"]
        assert _scheduler_labels(created["labels"], config) == []


# --------------------------------------------------------------------------- #
# B. A planning proposal that asked for nothing schedulable                     #
# --------------------------------------------------------------------------- #


class TestPlanningProposalWithoutSchedulerLabels:
    def test_informational_only_proposal_is_unchanged_except_for_the_gate(
        self,
    ) -> None:
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            config=config,
            labels=("bug", "area:example"),
        )

        assert creation.labels == ("bug", "area:example", PROPOSED_TECH_LEAD_LABEL)
        assert _scheduler_labels(creation.labels, config) == []

    def test_nothing_is_reported_as_withheld(self) -> None:
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            labels=("bug", "area:example"),
        )

        assert "withheld" not in creation.body.lower()


# --------------------------------------------------------------------------- #
# C. Fail-closed scheduler direction                                           #
# --------------------------------------------------------------------------- #


class TestPendingProposalIsNotAdmissible:
    def test_discovery_never_fetches_a_pending_proposal(self) -> None:
        """No agent label means no per-agent query can return it: the proposal
        is structurally unreachable by every Actor lane, not merely filtered."""
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )
        gatherer = FactGatherer(
            config=config,
            repository_host=_DiscoveryHost([_proposal_issue(creation.labels)]),
        )

        assert gatherer.fetch_issues([]) == []

    def test_admission_refuses_a_pending_proposal_by_blocking_label(self) -> None:
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )

        [decision] = Scheduler(config).evaluate_issues(
            [_proposal_issue(creation.labels)], check_dependencies=False
        )

        assert decision.available is False
        assert decision.reason == AvailabilityReason.BLOCKED_LABEL

    def test_refusal_survives_a_re_read_by_a_fresh_process(self) -> None:
        """The pending state lives on the issue's labels, so a restarted
        orchestrator re-reads the same refusal — nothing in-memory carries it."""
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )
        issue = _proposal_issue(creation.labels)

        for _restart in range(3):
            gatherer = FactGatherer(
                config=_config(), repository_host=_DiscoveryHost([issue])
            )
            [decision] = Scheduler(_config()).evaluate_issues(
                [issue], check_dependencies=False
            )
            assert gatherer.fetch_issues([]) == []
            assert decision.available is False

    def test_model_content_cannot_request_an_executable_role(self) -> None:
        """The hostile fixture asked to be executable; admission still refuses."""
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )
        issue = _proposal_issue(creation.labels)
        assert WORKER in _decision().proposed_actions[0].labels

        gatherer = FactGatherer(
            config=config, repository_host=_DiscoveryHost([issue])
        )
        [decision] = Scheduler(config).evaluate_issues(
            [issue], check_dependencies=False
        )

        assert gatherer.fetch_issues([]) == []
        assert decision.available is False


# --------------------------------------------------------------------------- #
# D. The explicit Human gate stays separate                                    #
# --------------------------------------------------------------------------- #


class TestExplicitHumanGate:
    def test_ungating_alone_does_not_schedule_an_actor(self) -> None:
        """Approval authorizes the bounded proposal; it does not route it.
        Planning never granted scheduling authority, so removing the gate alone
        still leaves nothing for any lane to fetch."""
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )
        ungated = [
            label
            for label in creation.labels
            if label != PROPOSED_TECH_LEAD_LABEL
        ]
        gatherer = FactGatherer(
            config=config, repository_host=_DiscoveryHost([_proposal_issue(ungated)])
        )

        assert gatherer.fetch_issues([]) == []

    def test_the_human_act_makes_it_eligible_through_the_existing_owner(
        self,
    ) -> None:
        """After the Human ungates AND routes it, the SHIPPED discovery and
        admission owners admit it — no new approval primitive involved."""
        config = _config()
        creation = _plan(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION, config=config
        )
        approved = [
            label
            for label in creation.labels
            if label != PROPOSED_TECH_LEAD_LABEL
        ] + [WORKER]
        issue = _proposal_issue(approved)

        gatherer = FactGatherer(
            config=config, repository_host=_DiscoveryHost([issue])
        )
        [decision] = Scheduler(config).evaluate_issues(
            [issue], check_dependencies=False
        )

        assert [i.number for i in gatherer.fetch_issues([])] == [400]
        assert decision.available is True
        assert decision.reason == AvailabilityReason.AVAILABLE

    def test_the_body_names_both_halves_of_the_approval_act(self) -> None:
        creation = _plan(flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION)

        assert PROPOSED_TECH_LEAD_LABEL in creation.body
        assert WORKER in creation.body


# --------------------------------------------------------------------------- #
# E. Mutation / failure direction                                              #
# --------------------------------------------------------------------------- #


class TestCoexistenceCannotBeRestored:
    def test_a_pending_proposal_cannot_be_constructed_as_routed(self) -> None:
        """Restoring 'forward the scheduler label onto a pending proposal' is
        not a code path that exists: the admission type refuses it."""
        with pytest.raises(ValueError, match="unscheduled"):
            TechLeadIssueAdmission(
                destination_agent=WORKER,
                gate=True,
                pending_human_approval=True,
            )

    def test_a_pending_proposal_cannot_drop_its_gate(self) -> None:
        with pytest.raises(ValueError, match="approval gate"):
            TechLeadIssueAdmission(
                destination_agent="",
                gate=False,
                pending_human_approval=True,
            )

    def test_a_scheduled_creation_still_requires_its_destination(self) -> None:
        with pytest.raises(ValueError, match="destination worker agent"):
            TechLeadIssueAdmission(
                destination_agent="",
                gate=True,
                pending_human_approval=False,
            )


# --------------------------------------------------------------------------- #
# F. Regression: non-planning creation keeps its shipped semantics             #
# --------------------------------------------------------------------------- #


class TestNonPlanningCreationUnchanged:
    def test_failure_investigation_still_routes_its_gated_follow_up(self) -> None:
        config = _config()
        config.tech_lead.authority.create_issue = "propose"
        creation = _plan(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            config=config,
            labels=("bug",),
        )

        assert WORKER in creation.labels
        assert PROPOSED_TECH_LEAD_LABEL in creation.labels

    def test_batch_review_execute_authority_still_files_ungated_routed_work(
        self,
    ) -> None:
        config = _config()
        config.tech_lead.authority.create_issue = "execute"

        creation = _plan(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            config=config,
            labels=("bug",),
        )

        assert WORKER in creation.labels
        assert PROPOSED_TECH_LEAD_LABEL not in creation.labels
