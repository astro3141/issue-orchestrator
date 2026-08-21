"""An operator can ask for a ``planning_investigation`` run (#189).

Every stage downstream of the request already shipped — the flavor and its
admission branch (#136), canonical-context staging (#183), recovery-authority
completeness (#182), truncation legibility (#185) — and nothing in production
constructed a :class:`PlanningInvestigationScope`. This module pins the
PRODUCER leaf: the two operator surfaces (the dashboard's wire payload and the
one-shot CLI's flag) can name the focused role they want, and what they name
reaches the shipped planning admission unchanged.

Both sides of each command boundary are covered, per the repository's
command-surface rule:

* wire payload -> :func:`_domain_scope` -> the domain scope value, and that
  scope through the REAL :class:`TechLeadRunCoordinator` into the planning
  queue;
* CLI flag -> parser -> :func:`run_targeted_investigations` -> the same scope,
  handed to the same admission owner.

The failure directions the issue names are asserted in the direction they would
actually fail: naming nothing must still be a failure investigation, a blocked
subject must be refused with the PLANNING refusal, and no unattended path may
produce a planning run at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from issue_orchestrator.contracts.ui_openapi_models import TechLeadRunRequestPayload
from issue_orchestrator.control.tech_lead_run_admission import TechLeadRunCoordinator
from issue_orchestrator.control.tech_lead_trigger import focused_run_label
from issue_orchestrator.domain.models import OrchestratorState, PendingTechLeadReview
from issue_orchestrator.domain.tech_lead_run import (
    DEFAULT_FOCUSED_RUN_FLAVOR,
    FOCUSED_RUN_FLAVOR_NAMES,
    REASON_ISSUE_BLOCKED,
    REASON_NO_LONGER_BLOCKED,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    PlanningInvestigationScope,
    TechLeadRunOutcome,
    TechLeadRunRequest,
    TechLeadRunTrigger,
    focused_run_flavor,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.entrypoints.web_tech_lead_routes import _domain_scope

from .control.run_ledger_doubles import SharedRunLedger

TECH_LEAD_AGENT = "agent:tech-lead"
BLOCKING_LABEL = "blocked-failed"
SUBJECT = 109

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"

# The ONLY modules whose ``TechLeadRunRequest`` may name the planning role. Both
# are operator-initiated by construction: an HTTP command endpoint a human posts
# to, and the owner the one-shot CLI drives. #136 stopped short of a producer
# precisely so that no timer, label route or scheduler path could start a
# planning run before a deliberate pilot had happened.
OPERATOR_REQUEST_MODULES = frozenset(
    {
        "entrypoints/web_tech_lead_routes.py",
        "control/tech_lead_trigger.py",
    }
)

# Names by which a module could reach the planning role: the scope value, the
# session flavor, its wire/CLI spelling, and the two resolvers that turn a named
# flavor into a scope. A non-operator requester must carry none of them.
#
# ``scope_for_flavor`` is matched on a word boundary that excludes
# ``global_scope_for_flavor``: that sibling resolves WHOLE-REPOSITORY flavors
# only and raises on an issue-scoped one, so it cannot reach this role.
PLANNING_REACHING_NAMES = (
    r"PlanningInvestigationScope",
    r"PLANNING_INVESTIGATION",
    r"planning_investigation",
    r"(?<!global_)\bscope_for_flavor\b",
    r"\bfocused_run_flavor\b",
)

# Modules that decide work WITHOUT an operator: the reactive failure model, the
# periodic/storm health-review trigger, the stuck-sweep backstop, the planner,
# and the automatic-failure run wiring. None may reach the planning flavor.
UNATTENDED_MODULES = (
    "control/tech_lead_reaction.py",
    "control/tech_lead_run_wiring.py",
    "control/stuck_sweep.py",
    "control/planner.py",
)


# ---------------------------------------------------------------------------
# Deterministic doubles at the ports the coordinator actually depends on
# ---------------------------------------------------------------------------


@dataclass
class FakeIssue:
    number: int
    title: str = "Prepare the thing"
    labels: tuple[str, ...] = ()
    state: str = "open"
    body: str = ""
    milestone: Optional[str] = None


class FakeRepositoryHost:
    def __init__(self, issues: Optional[dict[int, FakeIssue]] = None) -> None:
        self.issues = dict(issues or {})

    def get_issue(self, number: int) -> Optional[FakeIssue]:
        return self.issues.get(number)


class FakeAnchorHost:
    """A global admission never happens in these tests; reaching it is a bug."""

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]:
        raise AssertionError("an issue-scoped request must not touch the anchor")


class RecordingEvents:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)


def _config() -> Any:
    from issue_orchestrator.infra.config import Config

    config = Config()
    config.tech_lead_review_agent = TECH_LEAD_AGENT
    return config


def _open_unblocked() -> FakeRepositoryHost:
    return FakeRepositoryHost({SUBJECT: FakeIssue(SUBJECT)})


def _blocked() -> FakeRepositoryHost:
    return FakeRepositoryHost(
        {SUBJECT: FakeIssue(SUBJECT, labels=(BLOCKING_LABEL,))}
    )


def _coordinator(
    state: OrchestratorState, repository_host: FakeRepositoryHost
) -> TechLeadRunCoordinator:
    return TechLeadRunCoordinator(
        state=state,
        config=_config(),
        repository_host=repository_host,  # type: ignore[arg-type]
        anchor_host=FakeAnchorHost(),
        ownership=SharedRunLedger().ownership("engine-1"),
        is_blocking_any=lambda labels: any(
            str(label).startswith("blocked") for label in labels
        ),
        events=RecordingEvents(),  # type: ignore[arg-type]
    )


def _dashboard_scope(scope_body: dict[str, Any]):
    """The scope a dashboard request body projects onto, contract included."""
    return _domain_scope(TechLeadRunRequestPayload.model_validate({"scope": scope_body}))


# ---------------------------------------------------------------------------
# Direction 2: an existing request names no flavor and is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope_body",
    [
        {"kind": "issue", "issue_number": SUBJECT},
        {"kind": "issue", "issue_number": SUBJECT, "flavor": "failure_investigation"},
    ],
    ids=["flavor_omitted", "flavor_named_explicitly"],
)
def test_an_issue_request_without_a_planning_flavor_is_a_failure_investigation(
    scope_body,
):
    """Every caller that existed before the discriminator is byte-identical."""
    assert _dashboard_scope(scope_body) == IssueInvestigationScope(SUBJECT)


def test_a_global_request_is_untouched_by_the_discriminator():
    assert _dashboard_scope({"kind": "global_health_review"}) == GlobalHealthReviewScope()


def test_naming_no_focused_flavor_resolves_to_the_failure_investigation():
    """The default lives in ONE place, shared by both operator surfaces."""
    assert focused_run_flavor(None) is DEFAULT_FOCUSED_RUN_FLAVOR
    assert focused_run_flavor("") is DEFAULT_FOCUSED_RUN_FLAVOR
    assert DEFAULT_FOCUSED_RUN_FLAVOR is TechLeadSessionFlavor.FAILURE_INVESTIGATION


@pytest.mark.parametrize(
    "named",
    ["health_review", "batch_review", "planning", "", " ", "PLANNING_INVESTIGATION"],
)
def test_a_flavor_that_is_not_focused_is_refused_rather_than_resolved(named):
    """A whole-board flavor here would admit an exclusive review as one issue's run.

    ``""`` and ``" "`` are the two shapes of "named nothing": the empty string
    is the default, and whitespace is a value that names no flavor at all.
    """
    if named == "":
        assert focused_run_flavor(named) is DEFAULT_FOCUSED_RUN_FLAVOR
        return
    with pytest.raises(ValueError, match="focused tech-lead run flavor"):
        focused_run_flavor(named)


def test_the_focused_flavor_vocabulary_is_derived_from_focused_ness():
    """One list, so the wire discriminator and the CLI choices cannot drift."""
    assert FOCUSED_RUN_FLAVOR_NAMES == (
        "failure_investigation",
        "planning_investigation",
    )
    assert all(
        focused_run_flavor(name).is_issue_focused for name in FOCUSED_RUN_FLAVOR_NAMES
    )


@pytest.mark.parametrize(
    "scope_body",
    [
        {"kind": "issue", "issue_number": SUBJECT, "flavor": "health_review"},
        {"kind": "issue", "issue_number": SUBJECT, "flavor": "planning"},
        {"kind": "global_health_review", "flavor": "planning_investigation"},
    ],
)
def test_the_wire_contract_refuses_a_flavor_it_does_not_declare(scope_body):
    """The generated model is the gate: an unknown role never reaches the owner.

    A ``flavor`` on the GLOBAL scope is refused too — that payload forbids extra
    keys, so a whole-board request cannot smuggle a focused role.
    """
    with pytest.raises(ValueError):
        TechLeadRunRequestPayload.model_validate({"scope": scope_body})


# ---------------------------------------------------------------------------
# Direction 1: a planning-flavored request reaches the planning admission
# ---------------------------------------------------------------------------


def test_a_planning_flavored_dashboard_request_projects_onto_the_planning_scope():
    scope = _dashboard_scope(
        {"kind": "issue", "issue_number": SUBJECT, "flavor": "planning_investigation"}
    )

    assert scope == PlanningInvestigationScope(SUBJECT)
    assert scope.flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION
    assert scope.run_key == f"planning:{SUBJECT}"


def test_a_planning_request_is_queued_through_the_planning_admission():
    """Direction 1: it must reach ``_admit_planning``, never ``_admit_issue``.

    Proven by what lands in the queue rather than by patching the branch: only
    ``queue_planning_investigation`` produces a PLANNING_INVESTIGATION entry
    with no failure context — ``_admit_issue`` manufactures a
    ``manual_focus_failure`` for every hand-aimed request it admits.
    """
    state = OrchestratorState()
    scope = _dashboard_scope(
        {"kind": "issue", "issue_number": SUBJECT, "flavor": "planning_investigation"}
    )

    admission = _coordinator(state, _open_unblocked()).admit(
        TechLeadRunRequest(scope=scope, trigger=TechLeadRunTrigger.DASHBOARD)
    )

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.run_key == f"planning:{SUBJECT}"
    assert [item.flavor for item in state.pending_tech_lead_reviews] == [
        TechLeadSessionFlavor.PLANNING_INVESTIGATION
    ]
    assert state.pending_tech_lead_reviews[0].failure is None


# ---------------------------------------------------------------------------
# Directions 3 + 4: the subject rule an operator request is judged by
# ---------------------------------------------------------------------------


def test_one_open_unblocked_subject_admits_planning_and_refuses_investigation():
    """Direction 4: the exact case ``subject_run_eligibility`` was written for.

    Asserted as a CONTRAST on one subject, because "admitted" alone would also
    pass if both roles accepted an open issue. The operator's choice of flavor
    is what decides the verdict — which is the whole point of the discriminator.
    """
    planning = _coordinator(OrchestratorState(), _open_unblocked()).admit(
        TechLeadRunRequest(
            scope=_dashboard_scope(
                {
                    "kind": "issue",
                    "issue_number": SUBJECT,
                    "flavor": "planning_investigation",
                }
            ),
            trigger=TechLeadRunTrigger.DASHBOARD,
        )
    )
    investigation = _coordinator(OrchestratorState(), _open_unblocked()).admit(
        TechLeadRunRequest(
            scope=_dashboard_scope({"kind": "issue", "issue_number": SUBJECT}),
            trigger=TechLeadRunTrigger.DASHBOARD,
        )
    )

    assert planning.outcome is TechLeadRunOutcome.QUEUED
    assert investigation.outcome is TechLeadRunOutcome.NOT_ELIGIBLE
    assert investigation.reason == REASON_NO_LONGER_BLOCKED


def test_a_blocked_subject_is_refused_with_the_planning_refusal():
    """Direction 3: ``issue_blocked``, never the investigation's ``no_longer_blocked``.

    The two focused roles refuse each other's subject state, so the reason code
    is what tells an operator WHICH role they aimed wrongly.
    """
    state = OrchestratorState()

    admission = _coordinator(state, _blocked()).admit(
        TechLeadRunRequest(
            scope=_dashboard_scope(
                {
                    "kind": "issue",
                    "issue_number": SUBJECT,
                    "flavor": "planning_investigation",
                }
            ),
            trigger=TechLeadRunTrigger.DASHBOARD,
        )
    )

    assert admission.outcome is TechLeadRunOutcome.NOT_ELIGIBLE
    assert admission.reason == REASON_ISSUE_BLOCKED
    assert admission.reason != REASON_NO_LONGER_BLOCKED
    assert state.pending_tech_lead_reviews == []


# ---------------------------------------------------------------------------
# Direction 8: two runs of one issue are two identities
# ---------------------------------------------------------------------------


def test_the_two_operator_requests_for_one_issue_are_distinct_run_keys():
    """A planning run and a failure investigation never coalesce (#136).

    Asserted at the OPERATOR surface, because that is where the two are now
    askable: the same issue, the same wire ``kind``, two different identities.
    """
    planning = _dashboard_scope(
        {"kind": "issue", "issue_number": SUBJECT, "flavor": "planning_investigation"}
    )
    investigation = _dashboard_scope({"kind": "issue", "issue_number": SUBJECT})

    assert planning.run_key == f"planning:{SUBJECT}"
    assert investigation.run_key == f"issue:{SUBJECT}"
    assert planning.run_key != investigation.run_key
    # Same SHAPE, so both are judged by the issue-scoped exclusivity rules.
    assert planning.kind is investigation.kind


# ---------------------------------------------------------------------------
# Direction 5: no unattended trigger exists
# ---------------------------------------------------------------------------


def _module_text(relative: str) -> str:
    return (SRC_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", UNATTENDED_MODULES)
def test_no_unattended_path_can_name_the_planning_flavor(relative: str):
    """Direction 5: a timer, label or scheduler path cannot start planning work.

    Read from the source rather than exercised, because the guarantee is an
    ABSENCE: there is no code path to call, and an assertion about behaviour
    could only ever prove that the paths tested today do not reach it.
    """
    text = _module_text(relative)

    assert "PlanningInvestigationScope" not in text
    assert "PLANNING_INVESTIGATION" not in text


def test_only_operator_surfaces_can_request_a_planning_run():
    """The set of planning-capable producers is closed, and both are hand-driven.

    Every module that CONSTRUCTS a run request is enumerated from source; the
    ones that are not operator surfaces must name nothing that could reach the
    planning role — not the scope, not the flavor, not its spelling, and not
    either resolver that turns a named flavor into a scope. A new unattended
    producer therefore fails here the moment it is written.
    """
    requesters = {
        str(path.relative_to(SRC_ROOT))
        for path in SRC_ROOT.rglob("*.py")
        if "TechLeadRunRequest(" in path.read_text(encoding="utf-8")
    }

    # Both operator surfaces really are producers (otherwise this test would
    # pass vacuously if the leaf were reverted).
    assert OPERATOR_REQUEST_MODULES <= requesters
    for relative in sorted(requesters - OPERATOR_REQUEST_MODULES):
        text = _module_text(relative)
        for pattern in PLANNING_REACHING_NAMES:
            assert re.search(pattern, text) is None, (
                f"{relative} constructs a tech-lead run request and can reach the"
                f" planning role via {pattern}"
            )


def test_the_automatic_failure_trigger_still_only_names_the_recovery_role():
    """The one automatic path that reaches issue-scoped admission is unchanged."""
    text = _module_text("control/tech_lead_run_wiring.py")

    assert "IssueInvestigationScope" in text
    assert TechLeadRunTrigger.AUTOMATIC_FAILURE.name in text


# ---------------------------------------------------------------------------
# The CLI half: one flag on an existing command, no new entrypoint
# ---------------------------------------------------------------------------


def _parse_tech_lead(argv: list[str]):
    from issue_orchestrator.entrypoints.cli_parser import build_parser
    from unittest.mock import MagicMock

    return build_parser(MagicMock()).parse_args(argv)


def test_the_cli_defaults_to_the_failure_investigation_it_always_dispatched():
    args = _parse_tech_lead(["tech_lead", str(SUBJECT)])

    assert args.flavor == DEFAULT_FOCUSED_RUN_FLAVOR.value
    assert focused_run_flavor(args.flavor) is TechLeadSessionFlavor.FAILURE_INVESTIGATION


def test_the_cli_flag_names_the_planning_role():
    args = _parse_tech_lead(
        ["tech_lead", str(SUBJECT), "--flavor", "planning_investigation"]
    )

    assert focused_run_flavor(args.flavor) is (
        TechLeadSessionFlavor.PLANNING_INVESTIGATION
    )


def test_the_cli_refuses_a_flavor_outside_the_focused_vocabulary():
    with pytest.raises(SystemExit):
        _parse_tech_lead(["tech_lead", str(SUBJECT), "--flavor", "health_review"])


def test_no_new_cli_entrypoint_was_added_for_planning():
    """Scope guard: the flag rides the existing ``tech_lead`` command.

    The declared command surface is what ``docs/user/stability.md`` publishes,
    so a new planning entrypoint would have to appear here — and would be a
    second hand-aimed surface for one role.
    """
    from issue_orchestrator.entrypoints.cli_parser import CLI_COMMANDS

    assert "tech_lead" in CLI_COMMANDS
    assert not any("plan" in name for name in CLI_COMMANDS)


@pytest.mark.parametrize(
    ("flavor", "expected_label"),
    [
        (TechLeadSessionFlavor.FAILURE_INVESTIGATION, "investigation"),
        (TechLeadSessionFlavor.PLANNING_INVESTIGATION, "planning investigation"),
    ],
)
def test_each_focused_role_has_one_operator_facing_name(flavor, expected_label):
    assert focused_run_label(flavor) == expected_label


@pytest.mark.parametrize(
    "flavor",
    [TechLeadSessionFlavor.HEALTH_REVIEW, TechLeadSessionFlavor.BATCH_REVIEW],
)
def test_a_whole_board_flavor_has_no_focused_run_name(flavor):
    with pytest.raises(ValueError, match="focused tech-lead run flavor"):
        focused_run_label(flavor)


# ---------------------------------------------------------------------------
# Direction 6: the declaration syntax is written down for its HUMAN author
# ---------------------------------------------------------------------------


DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"

# The documents where `Depends-on:` / `Stack-after:` are already explained. A
# human authoring a governing declaration reads these, not an agent prompt, and
# a typo costs a retry-queue burn — so the syntax belongs beside them.
GOVERNING_SYNTAX_DOCS = (
    DOCS_ROOT / "user" / "faq.md",
    DOCS_ROOT / "user" / "tutorial.md",
    DOCS_ROOT
    / "architecture"
    / "ADR"
    / "0029-stacked-work-via-typed-dependency-edges.md",
)


@pytest.mark.parametrize(
    "doc", GOVERNING_SYNTAX_DOCS, ids=lambda path: path.name
)
def test_both_governing_keywords_are_documented_for_humans(doc: Path):
    text = doc.read_text(encoding="utf-8")

    assert "Governed-by:" in text
    assert "Governed-by-optional:" in text
    # The reference form is the one the parser accepts, spelled out.
    assert "#" in text


@pytest.mark.parametrize(
    ("line", "accepted"),
    [
        ("Governed-by: #21", True),
        ("Governed-by-optional: #23", True),
        ("Governed-by: #21  # a trailing comment", True),
        ("  governed-by: #21", True),
        ("Governed-by: 21", False),
        ("Governed-by: M1-010", False),
        ("Governed-by: org/other-repo#5", False),
        (f"Governed-by: #{SUBJECT}", False),
        ("Governed-by: #21\nGoverned-by-optional: #21", False),
    ],
)
def test_the_documented_syntax_rules_are_the_parser_s_actual_rules(line, accepted):
    """The docs are checked against the parser, not merely for existence.

    Every row here is a row in the FAQ's syntax table. A documented rule that
    the parser does not enforce (or enforces the other way) would be worse than
    no documentation: it would teach an author a declaration that fails at
    launch, which is exactly the retry-queue burn this write-up exists to
    prevent.
    """
    from issue_orchestrator.domain.canonical_context import parse_governing_sources

    if accepted:
        assert parse_governing_sources(line, subject_issue_number=SUBJECT)
        return
    with pytest.raises(ValueError):
        parse_governing_sources(line, subject_issue_number=SUBJECT)


def test_a_governing_declaration_creates_no_dependency_edge():
    """The docs promise no scheduling effect; the dependency parser agrees."""
    from issue_orchestrator.domain.dependencies import parse_dependency_refs

    assert parse_dependency_refs("Governed-by: #21\nGoverned-by-optional: #23") == []
