"""The completion gate routing owner (#293 / #319 / #370 / #388).

One question, one owner: given the owner-injected managed-run context, does
``coding-done completed`` run the code-candidate quick gate? Everything the
routing owner cannot read as a tech-lead run must come back as the ordinary
gate — the unsafe direction is skipping a real coder candidate's validation.

Two signals, because the run directory cannot answer for the review exchange's
coder side: that run belongs to the exchange, not to the tech-lead launch that
stages an assignment, so the launcher declares the principal instead (#388).
Both fail safe to the ordinary gate.
"""

import json

import pytest

from issue_orchestrator.control.completion_gate_routing import (
    CompletionGateRoute,
    CompletionGateRouting,
    route_completion_gate,
)
from issue_orchestrator.domain.review_exchange_coder_principal import (
    EXCHANGE_CODER_PRINCIPAL_ENV_SUFFIX,
    ReviewExchangeCoderPrincipal,
)
from issue_orchestrator.entrypoints.cli_tools.coding_done import (
    resolve_completion_gate_routing,
    resolve_exchange_coder_principal,
)
from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadAssignment,
    TechLeadSessionFlavor,
    tech_lead_assignment_path,
)


def _stage(run_dir, assignment: TechLeadAssignment):
    assignment.write(tech_lead_assignment_path(run_dir))
    return run_dir


def _stage_raw(run_dir, payload: str):
    path = tech_lead_assignment_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    return run_dir


class TestPlanningRouting:
    def test_planning_investigation_skips_the_candidate_quick_gate(self, tmp_path):
        run_dir = _stage(
            tmp_path,
            TechLeadAssignment(
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                focus_issue_number=136,
            ),
        )

        routing = route_completion_gate(run_dir)

        assert routing.route is CompletionGateRoute.PLANNING_NO_CANDIDATE_GATE
        assert routing.runs_candidate_quick_gate is False
        assert "planning_investigation" in routing.reason

    def test_the_writer_and_the_reader_agree_on_where_the_assignment_lives(
        self, tmp_path
    ):
        """The launcher's own writer places it where the router looks.

        A router that computed the path itself would read "absent" — which is
        indistinguishable from "not a tech_lead run" — instead of failing.
        """
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
            focus_issue_number=136,
        )
        assignment.write(tech_lead_assignment_path(tmp_path))

        assert route_completion_gate(tmp_path).runs_candidate_quick_gate is False


class TestOrdinaryRouting:
    def test_no_managed_run_context_routes_to_the_candidate_gate(self):
        routing = route_completion_gate(None)

        assert routing.route is CompletionGateRoute.CANDIDATE_QUICK_GATE
        assert routing.runs_candidate_quick_gate is True

    def test_managed_run_without_an_assignment_routes_to_the_candidate_gate(
        self, tmp_path
    ):
        assert route_completion_gate(tmp_path).runs_candidate_quick_gate is True


class TestTechLeadValidationOwnership:
    """#370: no Tech Lead flavor runs repository validation in its sandbox.

    #364 proved the coupling was fatal, not merely wasteful: the model session
    could not complete at all, because the repository validation path needs
    host/repository-owned effects outside the model's scratch write boundary.
    """

    @pytest.mark.parametrize(
        "flavor,focus",
        [
            (TechLeadSessionFlavor.BATCH_REVIEW, None),
            (TechLeadSessionFlavor.HEALTH_REVIEW, None),
            (TechLeadSessionFlavor.FAILURE_INVESTIGATION, 42),
        ],
    )
    def test_every_non_planning_flavor_leaves_validation_to_the_orchestrator(
        self, tmp_path, flavor, focus
    ):
        run_dir = _stage(
            tmp_path,
            TechLeadAssignment(flavor=flavor, focus_issue_number=focus),
        )

        routing = route_completion_gate(run_dir)

        assert (
            routing.route
            is CompletionGateRoute.TECH_LEAD_ORCHESTRATOR_OWNED_VALIDATION
        )
        assert routing.runs_candidate_quick_gate is False
        assert flavor.value in routing.reason
        assert "orchestrator" in routing.reason

    def test_every_tech_lead_flavor_is_routed_away_from_its_own_gate(self, tmp_path):
        """No flavor may be left behind when a new one is added.

        Enumerated from the enum rather than a literal list, so a future
        flavor that nobody thought about here fails this test instead of
        silently inheriting the in-sandbox gate the repair removed.
        """
        for flavor in TechLeadSessionFlavor:
            run_dir = _stage(
                tmp_path / flavor.value,
                TechLeadAssignment(
                    flavor=flavor,
                    focus_issue_number=42 if flavor.is_issue_focused else None,
                ),
            )

            assert route_completion_gate(run_dir).runs_candidate_quick_gate is False


class TestFailSafe:
    def test_unparseable_assignment_routes_to_the_candidate_gate(self, tmp_path):
        run_dir = _stage_raw(tmp_path, "{not json")

        assert route_completion_gate(run_dir).runs_candidate_quick_gate is True

    def test_unknown_flavor_routes_to_the_candidate_gate(self, tmp_path):
        run_dir = _stage_raw(
            tmp_path,
            json.dumps({"schema_version": 1, "flavor": "sabotage_investigation"}),
        )

        assert route_completion_gate(run_dir).runs_candidate_quick_gate is True

    def test_planning_without_its_focus_issue_routes_to_the_candidate_gate(
        self, tmp_path
    ):
        """A focused flavor with no focus issue is not a valid planning run."""
        run_dir = _stage_raw(
            tmp_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "flavor": "planning_investigation",
                    "focus_issue_number": None,
                    "focus_reason": "",
                }
            ),
        )

        assert route_completion_gate(run_dir).runs_candidate_quick_gate is True

    def test_unsupported_schema_version_routes_to_the_candidate_gate(self, tmp_path):
        run_dir = _stage_raw(
            tmp_path,
            json.dumps(
                {
                    "schema_version": 99,
                    "flavor": "planning_investigation",
                    "focus_issue_number": 136,
                }
            ),
        )

        assert route_completion_gate(run_dir).runs_candidate_quick_gate is True

    @pytest.mark.parametrize("payload", ["[]", "null", "3", '"planning_investigation"'])
    def test_a_valid_json_non_object_routes_to_the_candidate_gate(
        self, tmp_path, payload
    ):
        """The one shape of "will not parse" that used to escape the fallback.

        ``json.loads`` succeeds on all of these, so the parser is the only
        thing that can refuse them — and it must refuse as ValueError. An
        ``AttributeError`` out of ``data.get`` would pass straight through
        this owner's ``except`` and crash the completion instead of falling
        back to the gate.
        """
        run_dir = _stage_raw(tmp_path, payload)

        assert route_completion_gate(run_dir).runs_candidate_quick_gate is True

    def test_an_assignment_shaped_directory_is_not_an_assignment(self, tmp_path):
        """A directory where the file should be must not read as planning."""
        tech_lead_assignment_path(tmp_path).mkdir(parents=True)

        assert route_completion_gate(tmp_path).runs_candidate_quick_gate is True


class TestRoutingValue:
    def test_a_routing_must_say_what_it_was_decided_from(self):
        with pytest.raises(ValueError, match="decided from"):
            CompletionGateRouting(
                route=CompletionGateRoute.CANDIDATE_QUICK_GATE, reason="  "
            )


class TestReviewExchangeCoderPrincipalRouting:
    """The second owner-injected signal (#388).

    The exchange's coder run directory belongs to the exchange, so no tech-lead
    assignment is staged there for the first signal to read. The launcher's
    declaration is what says whose completion contract that side runs under, and
    it is asked first — before the directory is read at all.
    """

    def test_a_tech_lead_on_the_exchange_lane_does_not_run_the_candidate_gate(
        self, tmp_path
    ):
        routing = route_completion_gate(
            tmp_path, exchange_coder=ReviewExchangeCoderPrincipal.TECH_LEAD
        )

        assert routing.route is (
            CompletionGateRoute.TECH_LEAD_ORCHESTRATOR_OWNED_VALIDATION
        )
        assert "review-exchange coder side" in routing.reason

    def test_it_does_not_need_a_run_directory_to_answer(self):
        routing = route_completion_gate(
            None, exchange_coder=ReviewExchangeCoderPrincipal.TECH_LEAD
        )

        assert not routing.runs_candidate_quick_gate

    def test_a_staged_planning_assignment_does_not_override_the_declaration(
        self, tmp_path
    ):
        """Both signals say "not this session's gate"; neither is bypassed."""
        run_dir = _stage(
            tmp_path,
            TechLeadAssignment(
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
                focus_issue_number=7,
                focus_reason="prepare",
            ),
        )

        routing = route_completion_gate(
            run_dir, exchange_coder=ReviewExchangeCoderPrincipal.TECH_LEAD
        )

        assert not routing.runs_candidate_quick_gate

    def test_an_actor_declaration_leaves_every_prior_answer_alone(self, tmp_path):
        assert route_completion_gate(
            tmp_path, exchange_coder=ReviewExchangeCoderPrincipal.ACTOR
        ).route is CompletionGateRoute.CANDIDATE_QUICK_GATE
        assert route_completion_gate(tmp_path).route is (
            CompletionGateRoute.CANDIDATE_QUICK_GATE
        )


class TestTheCompletionCommandReadsTheDeclaration:
    """``coding-done``'s own resolution of the declared principal."""

    def test_the_exchange_declaration_reaches_the_routing_owner(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            f"{ENV_PREFIX}{EXCHANGE_CODER_PRINCIPAL_ENV_SUFFIX}", "tech_lead"
        )

        assert resolve_exchange_coder_principal() is (
            ReviewExchangeCoderPrincipal.TECH_LEAD
        )
        assert not resolve_completion_gate_routing(None).runs_candidate_quick_gate

    def test_an_ordinary_session_declares_nothing_and_routes_ordinarily(
        self, monkeypatch
    ):
        monkeypatch.delenv(
            f"{ENV_PREFIX}{EXCHANGE_CODER_PRINCIPAL_ENV_SUFFIX}", raising=False
        )

        assert resolve_exchange_coder_principal() is (
            ReviewExchangeCoderPrincipal.ACTOR
        )
        assert resolve_completion_gate_routing(None).runs_candidate_quick_gate

    def test_unrecognised_text_fails_safe_to_the_ordinary_gate(self, monkeypatch):
        monkeypatch.setenv(
            f"{ENV_PREFIX}{EXCHANGE_CODER_PRINCIPAL_ENV_SUFFIX}", "wizard"
        )

        assert resolve_completion_gate_routing(None).runs_candidate_quick_gate
