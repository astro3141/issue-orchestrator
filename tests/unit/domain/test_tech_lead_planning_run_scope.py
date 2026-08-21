"""The planning run's identity, shape, and least authority (#136).

``planning_investigation`` is the first tech-lead flavor whose subject is an
issue nobody asked anyone to recover. That makes it the first flavor that shares
a SHAPE with another one (a focused run over a single issue) while needing a
different IDENTITY, and the first that must be structurally unable to reach the
recovery kinds. These tests pin both halves in the domain, where the vocabulary
lives, so a control-layer change cannot quietly re-derive either.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.tech_lead_run import (
    GlobalBatchReviewScope,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    PlanningInvestigationScope,
    TechLeadRunScopeKind,
    global_scope_for_flavor,
    scope_for_flavor,
    scope_kind_of_flavor,
    scope_kind_of_run_key,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)


class TestRunIdentity:
    """Two focused runs on one issue are two runs, not one."""

    def test_the_planning_run_key_has_its_own_namespace(self) -> None:
        assert PlanningInvestigationScope(109).run_key == "planning:109"

    def test_it_never_collides_with_an_investigation_of_the_same_issue(self) -> None:
        planning = PlanningInvestigationScope(109)
        investigation = IssueInvestigationScope(109)

        assert planning.run_key != investigation.run_key
        assert planning.subject_issue_number == investigation.subject_issue_number

    def test_both_focused_namespaces_classify_back_to_the_issue_shape(self) -> None:
        """The shared ledger checks a key against its declared kind.

        A key that classified differently from the scope that produced it would
        be rejected as a corrupt row by the peer that read it.
        """
        assert scope_kind_of_run_key("planning:109") is TechLeadRunScopeKind.ISSUE
        assert scope_kind_of_run_key("issue:109") is TechLeadRunScopeKind.ISSUE

    def test_a_planning_key_naming_no_issue_is_refused(self) -> None:
        with pytest.raises(ValueError):
            scope_kind_of_run_key("planning:0")
        with pytest.raises(ValueError):
            scope_kind_of_run_key("planning:everything")

    def test_a_planning_scope_needs_a_positive_subject(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            PlanningInvestigationScope(-1)

        assert "PlanningInvestigationScope" in str(excinfo.value)


class TestRunShape:
    """Shape decides exclusivity; identity decides deduplication."""

    def test_a_planning_run_is_issue_scoped_not_global(self) -> None:
        scope = PlanningInvestigationScope(109)

        assert scope.kind is TechLeadRunScopeKind.ISSUE
        assert scope.kind.is_global is False

    def test_the_flavor_declares_the_same_shape(self) -> None:
        assert (
            scope_kind_of_flavor(TechLeadSessionFlavor.PLANNING_INVESTIGATION)
            is TechLeadRunScopeKind.ISSUE
        )

    def test_it_is_not_a_whole_repository_run(self) -> None:
        with pytest.raises(ValueError):
            global_scope_for_flavor(TechLeadSessionFlavor.PLANNING_INVESTIGATION)

    def test_every_global_kind_still_reads_as_global(self) -> None:
        """The is_global rule was restated as an issue-scoped denylist (#136).

        The conservative direction has to survive that: an unrecognised kind
        must still read as global.
        """
        assert TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW.is_global is True
        assert TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW.is_global is True


class TestFlavorToScopeResolution:
    """One resolver, so a queued item, a session, and a request agree."""

    @pytest.mark.parametrize(
        ("flavor", "expected"),
        [
            (
                TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                PlanningInvestigationScope(109),
            ),
            (
                TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                IssueInvestigationScope(109),
            ),
            (TechLeadSessionFlavor.HEALTH_REVIEW, GlobalHealthReviewScope()),
            (TechLeadSessionFlavor.BATCH_REVIEW, GlobalBatchReviewScope()),
        ],
    )
    def test_each_flavor_resolves_to_its_own_scope(
        self, flavor: TechLeadSessionFlavor, expected: object
    ) -> None:
        assert scope_for_flavor(flavor, issue_number=109) == expected

    def test_an_issue_scoped_flavor_without_a_subject_fails_loudly(self) -> None:
        """Silently inventing a subject is how a run gets the wrong identity."""
        with pytest.raises(ValueError):
            scope_for_flavor(TechLeadSessionFlavor.PLANNING_INVESTIGATION)


class TestFocusedFlavors:
    """The one question every "treat these two alike" call site asks."""

    @pytest.mark.parametrize(
        "flavor",
        [
            TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            TechLeadSessionFlavor.PLANNING_INVESTIGATION,
        ],
    )
    def test_focused_flavors_are_focused(self, flavor: TechLeadSessionFlavor) -> None:
        assert flavor.is_issue_focused is True

    @pytest.mark.parametrize(
        "flavor",
        [TechLeadSessionFlavor.BATCH_REVIEW, TechLeadSessionFlavor.HEALTH_REVIEW],
    )
    def test_whole_board_flavors_are_not(self, flavor: TechLeadSessionFlavor) -> None:
        assert flavor.is_issue_focused is False

    def test_a_planning_assignment_requires_its_focus_issue(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            TechLeadAssignment(
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                focus_issue_number=None,
            )

        assert "planning_investigation" in str(excinfo.value)

    def test_a_planning_authority_requires_its_focus_issue(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                anchor_issue_number=109,
            )

        assert "planning_investigation" in str(excinfo.value)


class TestPlanningAuthorityScope:
    """The target axis, standing independently behind the capability axis."""

    @staticmethod
    def _authority() -> TechLeadLaunchAuthority:
        return TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            anchor_issue_number=109,
            focus_issue_number=109,
        )

    def test_comments_and_escalations_target_only_the_focus_issue(self) -> None:
        assert self._authority().allowed_targets() == frozenset({109})

    def test_it_owns_no_act_level_target_at_all(self) -> None:
        """Second, independent guard on #136 acceptance 3.

        Even if a future capability row let this role propose a reset, it would
        have nothing in scope to reset — the recovery role owns the subject's
        runtime, not the preparation role.
        """
        assert self._authority().allowed_act_level_targets() == frozenset()

    def test_an_investigation_of_the_same_issue_still_owns_its_focus(self) -> None:
        """The narrowing must not leak onto the recovery role."""
        investigation = TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=109,
            focus_issue_number=109,
        )

        assert investigation.allowed_act_level_targets() == frozenset({109})
