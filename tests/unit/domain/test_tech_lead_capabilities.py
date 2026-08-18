"""Flavor-scoped Tech Lead action-kind capabilities (#133).

The policy owner itself: what each shipped role may propose, that the table is
total and checked, and that a least-authority role structurally cannot reach
the recovery kinds. The completion-boundary enforcement lives in
``tests/unit/test_completion_action_planner.py``.
"""

import pytest

from issue_orchestrator.domain.tech_lead_artifacts import (
    VALID_TECH_LEAD_ACTION_TYPES,
    ProposedTechLeadAction,
    TechLeadDecision,
)
from issue_orchestrator.domain.tech_lead_capabilities import (
    TECH_LEAD_ACTION_CAPABILITIES,
    TechLeadActionCapabilityPolicy,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

# Every shipped role's set, spelled out rather than derived, so a change to the
# production table has to be restated here deliberately. Measured from the
# completion contract: a batch review has never had a valid act-level target
# (``TechLeadLaunchAuthority.allowed_act_level_targets`` is empty for it), the
# other two flavors do.
_COMMENT_AND_ROUTING = {
    "post_comment",
    "create_issue",
    "escalate_to_human",
    "flag_pattern",
}
_RECOVERY = {"reset_retry", "kill_hung_session"}
EXPECTED_SHIPPED_CAPABILITIES = {
    TechLeadSessionFlavor.BATCH_REVIEW: _COMMENT_AND_ROUTING,
    TechLeadSessionFlavor.FAILURE_INVESTIGATION: _COMMENT_AND_ROUTING | _RECOVERY,
    TechLeadSessionFlavor.HEALTH_REVIEW: _COMMENT_AND_ROUTING | _RECOVERY,
}

# Minimal contract-valid fields per action kind, so each single-action decision
# below is a decision the artifact contract itself accepts.
_ACTION_FIELDS: dict[str, dict[str, object]] = {
    "post_comment": {"target_number": 7, "body": "Diagnosis."},
    "create_issue": {"title": "Follow-up", "body": "Do the thing."},
    "escalate_to_human": {"target_number": 7, "body": "A human must decide."},
    "flag_pattern": {"body": "Recurring seam.", "pattern_signature": "flaky-ci"},
    "reset_retry": {"target_number": 7, "body": "Reset the scratch."},
    "kill_hung_session": {"target_number": 7, "body": "It is genuinely stuck."},
}


def decision_proposing(action_type: str) -> TechLeadDecision:
    """A valid one-action decision proposing *action_type*."""
    action = ProposedTechLeadAction(
        id="A1",
        action_type=action_type,  # type: ignore[arg-type]
        **_ACTION_FIELDS[action_type],  # type: ignore[arg-type]
    )
    decision = TechLeadDecision(summary="One proposal.", proposed_actions=(action,))
    decision.validate()  # the artifact contract accepts it; capability is separate
    return decision


class TestShippedCapabilities:
    """The shipped table preserves exactly what each flavor supports today."""

    @pytest.mark.parametrize(
        ("flavor", "expected"), sorted(EXPECTED_SHIPPED_CAPABILITIES.items())
    )
    def test_shipped_role_allows_exactly_its_measured_kinds(
        self, flavor: TechLeadSessionFlavor, expected: set[str]
    ) -> None:
        assert TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds(flavor) == expected

    @pytest.mark.parametrize(
        ("flavor", "action_type"),
        sorted(
            (flavor, action_type)
            for flavor, kinds in EXPECTED_SHIPPED_CAPABILITIES.items()
            for action_type in kinds
        ),
    )
    def test_shipped_role_accepts_every_kind_it_supports(
        self, flavor: TechLeadSessionFlavor, action_type: str
    ) -> None:
        assert (
            TECH_LEAD_ACTION_CAPABILITIES.violation(
                decision_proposing(action_type), flavor
            )
            is None
        )

    @pytest.mark.parametrize(
        ("flavor", "action_type"),
        sorted(
            (flavor, action_type)
            for flavor, kinds in EXPECTED_SHIPPED_CAPABILITIES.items()
            for action_type in sorted(VALID_TECH_LEAD_ACTION_TYPES - kinds)
        ),
    )
    def test_shipped_role_rejects_every_kind_it_does_not_support(
        self, flavor: TechLeadSessionFlavor, action_type: str
    ) -> None:
        detail = TECH_LEAD_ACTION_CAPABILITIES.violation(
            decision_proposing(action_type), flavor
        )

        assert detail is not None
        assert f"A1 ({action_type}) is not an action kind" in detail
        assert flavor.value in detail

    def test_escalate_to_human_floor_is_untouched(self) -> None:
        """Every shipped role keeps its escalation channel (#133 property 5)."""
        for flavor in TechLeadSessionFlavor:
            assert TECH_LEAD_ACTION_CAPABILITIES.permits(flavor, "escalate_to_human")


class TestCapabilityMutationDirection:
    """Failure-direction proof: the acceptance above is load-bearing.

    Removing any single shipped flavor/kind mapping must turn the matching
    acceptance case into a rejection — otherwise the acceptance tests would
    pass against a policy that does not actually grant anything.
    """

    @pytest.mark.parametrize(
        ("flavor", "action_type"),
        sorted(
            (flavor, action_type)
            for flavor, kinds in EXPECTED_SHIPPED_CAPABILITIES.items()
            for action_type in kinds
        ),
    )
    def test_removing_one_mapping_rejects_what_it_granted(
        self, flavor: TechLeadSessionFlavor, action_type: str
    ) -> None:
        mutated = TechLeadActionCapabilityPolicy(
            {
                declared_flavor: (
                    kinds - {action_type} if declared_flavor is flavor else kinds
                )
                for declared_flavor, kinds in (
                    TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds_by_flavor.items()
                )
            }
        )

        detail = mutated.violation(decision_proposing(action_type), flavor)

        assert detail is not None
        assert f"A1 ({action_type}) is not an action kind" in detail
        # Only the mutated role loses it; the other roles are untouched.
        for other in TechLeadSessionFlavor:
            if other is flavor:
                continue
            assert mutated.permits(
                other, action_type
            ) is TECH_LEAD_ACTION_CAPABILITIES.permits(other, action_type)


class TestLeastAuthorityRole:
    """The API can express a role that structurally cannot recover (#133).

    A synthetic policy — not a shipped flavor — declares the least-authority
    set a future planning role would carry. No planning flavor is added by this
    leaf, so the strict set is bound to an existing key purely to exercise the
    owner's structure.
    """

    PLANNING_KINDS = frozenset(("post_comment", "create_issue", "escalate_to_human"))

    def _least_authority_policy(self) -> TechLeadActionCapabilityPolicy:
        return TechLeadActionCapabilityPolicy(
            {
                **dict(TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds_by_flavor),
                TechLeadSessionFlavor.HEALTH_REVIEW: self.PLANNING_KINDS,
            }
        )

    @pytest.mark.parametrize("action_type", sorted(_RECOVERY))
    def test_least_authority_role_rejects_recovery_kinds(
        self, action_type: str
    ) -> None:
        detail = self._least_authority_policy().violation(
            decision_proposing(action_type), TechLeadSessionFlavor.HEALTH_REVIEW
        )

        assert detail is not None
        assert f"A1 ({action_type}) is not an action kind" in detail

    @pytest.mark.parametrize("action_type", sorted(PLANNING_KINDS))
    def test_least_authority_role_keeps_its_declared_kinds(
        self, action_type: str
    ) -> None:
        assert (
            self._least_authority_policy().violation(
                decision_proposing(action_type), TechLeadSessionFlavor.HEALTH_REVIEW
            )
            is None
        )

    def test_first_forbidden_action_invalidates_the_whole_decision(self) -> None:
        """A forbidden kind is reported even when every sibling is allowed.

        The caller rejects the decision rather than dropping the action, which
        is what keeps siblings from producing effects.
        """
        decision = TechLeadDecision(
            summary="Mixed proposals.",
            proposed_actions=(
                ProposedTechLeadAction(
                    id="A1",
                    action_type="post_comment",
                    target_number=7,
                    body="Diagnosis.",
                ),
                ProposedTechLeadAction(
                    id="A2",
                    action_type="kill_hung_session",
                    target_number=7,
                    body="It is stuck.",
                ),
            ),
        )

        detail = self._least_authority_policy().violation(
            decision, TechLeadSessionFlavor.HEALTH_REVIEW
        )

        assert detail is not None
        assert "A2 (kill_hung_session)" in detail


class TestPolicyConstructionIsTotal:
    """A role's capability must be declared, not defaulted."""

    def test_missing_flavor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="every tech_lead flavor"):
            TechLeadActionCapabilityPolicy(
                {TechLeadSessionFlavor.BATCH_REVIEW: frozenset(("post_comment",))}
            )

    def test_unknown_action_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown action kinds"):
            TechLeadActionCapabilityPolicy(
                {
                    **dict(TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds_by_flavor),
                    TechLeadSessionFlavor.BATCH_REVIEW: frozenset(("merge_pr",)),
                }
            )

    def test_role_without_any_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            TechLeadActionCapabilityPolicy(
                {
                    **dict(TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds_by_flavor),
                    TechLeadSessionFlavor.BATCH_REVIEW: frozenset(),
                }
            )

    def test_declared_kinds_are_not_aliased_to_the_callers_mapping(self) -> None:
        """The policy owns its table; a caller's later edit cannot widen it."""
        declared: dict[TechLeadSessionFlavor, frozenset[str]] = dict(
            TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds_by_flavor
        )
        policy = TechLeadActionCapabilityPolicy(declared)

        declared[TechLeadSessionFlavor.BATCH_REVIEW] = frozenset(
            VALID_TECH_LEAD_ACTION_TYPES
        )

        assert not policy.permits(TechLeadSessionFlavor.BATCH_REVIEW, "reset_retry")
