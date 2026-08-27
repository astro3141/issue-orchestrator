"""The completion gate routing owner (#293 / #319).

One question, one owner: given the owner-injected managed-run directory, does
``coding-done completed`` run the code-candidate quick gate? Everything the
routing owner cannot read as a planning run must come back as the ordinary
gate — the unsafe direction is skipping a real candidate's validation.
"""

import json

import pytest

from issue_orchestrator.control.completion_gate_routing import (
    CompletionGateRoute,
    CompletionGateRouting,
    route_completion_gate,
)
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

    @pytest.mark.parametrize(
        "flavor,focus",
        [
            (TechLeadSessionFlavor.BATCH_REVIEW, None),
            (TechLeadSessionFlavor.HEALTH_REVIEW, None),
            (TechLeadSessionFlavor.FAILURE_INVESTIGATION, 42),
        ],
    )
    def test_every_non_planning_flavor_keeps_the_candidate_gate(
        self, tmp_path, flavor, focus
    ):
        run_dir = _stage(
            tmp_path,
            TechLeadAssignment(flavor=flavor, focus_issue_number=focus),
        )

        routing = route_completion_gate(run_dir)

        assert routing.runs_candidate_quick_gate is True
        assert flavor.value in routing.reason


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
