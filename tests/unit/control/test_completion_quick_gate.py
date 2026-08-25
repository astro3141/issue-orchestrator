"""Which completions keep their local quick-validation gate (#293).

The owner grants the drop for exactly one shape — a cleanly parsed
``planning_investigation`` assignment — so every test here fixes one *other*
shape and asserts the gate survives it. The value of the routing is entirely in
its narrowness: it must not be possible to lose a code candidate's agent-side
feedback by deleting, truncating, or mistyping a file in the run directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_quick_gate import (
    route_completion_quick_gate,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadSessionFlavor,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

PLANNING = TechLeadSessionFlavor.PLANNING_INVESTIGATION

# Every flavor that is NOT planning. Enumerated from the enum itself rather than
# listed by hand, so a Tech Lead flavor added later is held to the existing
# behaviour until someone deliberately changes this test — "Tech Lead" must
# never quietly generalise into "skip validation".
OTHER_FLAVORS = tuple(
    flavor for flavor in TechLeadSessionFlavor if flavor is not PLANNING
)


@pytest.fixture()
def assets(tmp_path: Path) -> SessionRunAssets:
    """The run contract the orchestrator injects into a managed session."""
    return FileSystemSessionOutput().start_run(tmp_path, "issue-293")


def assignment_path(assets: SessionRunAssets) -> Path:
    return assets.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME


def write_assignment(assets: SessionRunAssets, flavor: TechLeadSessionFlavor) -> None:
    TechLeadAssignment(
        flavor=flavor,
        focus_issue_number=293 if flavor.is_issue_focused else None,
    ).write(assignment_path(assets))


class TestThePlanningRunThatEarnsTheDrop:
    def test_a_planning_assignment_drops_the_gate(
        self, assets: SessionRunAssets
    ) -> None:
        write_assignment(assets, PLANNING)

        routing = route_completion_quick_gate(assets)

        assert routing.runs_quick_gate is False

    def test_the_reason_names_the_planning_contract(
        self, assets: SessionRunAssets
    ) -> None:
        """An operator reading the log learns why, not just that."""
        write_assignment(assets, PLANNING)

        detail = route_completion_quick_gate(assets).detail

        assert "planning_investigation" in detail
        assert "no verdict to contribute" in detail


class TestEveryOtherSessionKeepsIt:
    def test_a_run_with_no_tech_lead_assignment_keeps_the_gate(
        self, assets: SessionRunAssets
    ) -> None:
        """The ordinary Actor: nothing wrote an assignment for it."""
        assert not assignment_path(assets).exists()

        routing = route_completion_quick_gate(assets)

        assert routing.runs_quick_gate is True
        assert "no tech_lead assignment" in routing.detail

    @pytest.mark.parametrize("flavor", OTHER_FLAVORS, ids=lambda f: f.value)
    def test_other_tech_lead_flavors_keep_the_gate(
        self, assets: SessionRunAssets, flavor: TechLeadSessionFlavor
    ) -> None:
        write_assignment(assets, flavor)

        routing = route_completion_quick_gate(assets)

        assert routing.runs_quick_gate is True
        assert flavor.value in routing.detail

    def test_the_enumeration_really_covers_the_non_planning_flavors(self) -> None:
        """Guards the parametrization above against an empty sweep."""
        assert set(OTHER_FLAVORS) == set(TechLeadSessionFlavor) - {PLANNING}
        assert TechLeadSessionFlavor.FAILURE_INVESTIGATION in OTHER_FLAVORS


class TestUnreadableIsNeverPlanning:
    """Missing/malformed/ambiguous routing evidence must not become a drop."""

    def test_malformed_json_keeps_the_gate(self, assets: SessionRunAssets) -> None:
        path = assignment_path(assets)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        routing = route_completion_quick_gate(assets)

        assert routing.runs_quick_gate is True
        assert "could not be read" in routing.detail

    def test_an_unknown_flavor_keeps_the_gate(self, assets: SessionRunAssets) -> None:
        path = assignment_path(assets)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version": 1, "flavor": "planning", "focus_issue_number": 293}',
            encoding="utf-8",
        )

        routing = route_completion_quick_gate(assets)

        assert routing.runs_quick_gate is True

    def test_an_assignment_that_is_a_directory_keeps_the_gate(
        self, assets: SessionRunAssets
    ) -> None:
        """The read fails with OSError rather than ValueError; same answer."""
        assignment_path(assets).mkdir(parents=True)

        routing = route_completion_quick_gate(assets)

        assert routing.runs_quick_gate is True
        assert "could not be read" in routing.detail
