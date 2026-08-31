"""The Tech Lead completion protocol asks for nothing it may not do (#385).

A bounded Tech Lead is handed ``coding-done`` like a coder, and until #385 it
was handed the coder's DOCUMENT too — which makes ``prepush-check --dirty-only
-v`` mandatory. That command records timings under the repository's shared git
common dir, outside the session's sandbox write roots, so a live bounded run
(#383) died at exactly that step.

These tests pin the two halves of the repair at the source: the Tech Lead
document never issues the command, and the Actor/rework document still does.
They also pin the owner that decides which of the two a coding-lane launch is
handed — ``control/tech_lead_session_policy.coding_lane_task_kind`` — since the
three launch sites are only as correct as the answer they ask it for. The
end-to-end direction, that each launcher actually hands a tech-lead session this
document, lives in ``tests/unit/test_session_launcher.py``.

#388 closes the one lane #385 left open, on the same shape: the review
exchange's coder SIDE is a position in the protocol, so
``review_exchange_coder_principal`` names the authority sitting in it, and that
one value selects the document, the sandbox role, and whether ``coding-done``
runs the host-mutating quick gate. Who files the round's validation evidence —
the other half, which a document swap alone would not have moved — is pinned in
``tests/unit/execution/test_review_exchange_turn_validation.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_gate_routing import (
    CompletionGateRoute,
    route_completion_gate,
)
from issue_orchestrator.control.tech_lead_session_policy import (
    coding_lane_task_kind,
    review_exchange_coder_principal,
)
from issue_orchestrator.domain.review_exchange_coder_principal import (
    ReviewExchangeCoderPrincipal,
)
from issue_orchestrator.domain.sandbox_scope import (
    REVIEW_EXCHANGE_CODER_TASK_KIND,
    REVIEW_EXCHANGE_TECH_LEAD_TASK_KIND,
    SandboxRole,
    SandboxScopeContext,
    compute_session_scope,
)
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.resources import (
    get_coding_done_instructions,
    get_completion_instructions,
    get_review_exchange_coder_instructions,
    get_review_exchange_tech_lead_instructions,
    get_reviewer_done_instructions,
    get_tech_lead_done_instructions,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The imperative the model must never be given. Matched as the exact command
#: line rather than the bare tool name, so a document may still NAME the command
#: in order to forbid it.
PREPUSH_IMPERATIVE = "prepush-check --dirty-only -v"


def _flowed(text: str) -> str:
    """Collapse Markdown line wrapping so assertions read as sentences."""
    return " ".join(text.split())


class TestTheTechLeadProtocolIsItsOwn:
    def test_the_tech_lead_task_kind_selects_it(self) -> None:
        assert (
            get_completion_instructions(TaskKind.TECH_LEAD.value)
            == get_tech_lead_done_instructions()
        )

    def test_it_is_neither_the_coder_nor_the_reviewer_document(self) -> None:
        tech_lead = get_tech_lead_done_instructions()

        assert tech_lead != get_coding_done_instructions()
        assert tech_lead != get_reviewer_done_instructions()

    def test_it_still_completes_with_coding_done(self) -> None:
        """The Tech Lead's completion command is unchanged; its gate moved."""
        tech_lead = get_tech_lead_done_instructions()

        assert "coding-done completed" in tech_lead
        assert "coding-done blocked" in tech_lead
        assert "coding-done needs_human" in tech_lead


class TestTheHostMutatingStepIsGone:
    def test_the_tech_lead_document_never_issues_the_command(self) -> None:
        assert PREPUSH_IMPERATIVE not in get_tech_lead_done_instructions()

    def test_it_says_who_owns_the_validation_instead(self) -> None:
        """Not skipped, reassigned — the document has to say so."""
        tech_lead = _flowed(get_tech_lead_done_instructions())

        assert "Do not run `prepush-check`" in tech_lead
        assert "the orchestrator executes the mandatory completion validation" in (
            tech_lead
        )

    def test_it_still_demands_a_clean_committed_checkout(self) -> None:
        """What the model owes the gate is a publishable tree, not a gate run."""
        tech_lead = _flowed(get_tech_lead_done_instructions())

        assert "git status --short" in tech_lead
        assert "reject a dirty working tree" in tech_lead

    def test_it_overrides_any_prompt_that_asks_for_the_command(self) -> None:
        """Insurance for repository-supplied text, not for our own prompts.

        A target repository can supply a task prompt or a
        ``retry_prompt_template`` that names the command; those the orchestrator
        does not control, so the document has to win against them. What it must
        NOT be is the resolution for a contradiction the orchestrator itself
        ships — #385 round 2 F3/A2 removed that contradiction at its source, and
        the document says so, so this sentence stays insurance rather than
        becoming load-bearing.
        """
        tech_lead = _flowed(get_tech_lead_done_instructions())

        assert (
            "If ANY other prompt tells you to run `prepush-check`"
        ) in tech_lead
        assert "repository-supplied retry template" in tech_lead
        assert "this instruction wins" in tech_lead
        assert "The orchestrator's own prompts will not ask you to" in tech_lead


class TestTheActorProtocolIsUnchanged:
    def test_the_coder_document_still_issues_the_command(self) -> None:
        """R4: no Actor/Reviewer behaviour moves with this repair."""
        assert PREPUSH_IMPERATIVE in get_coding_done_instructions()

    @pytest.mark.parametrize("task_kind", ["code", "rework"])
    def test_coder_task_kinds_still_get_the_coder_document(
        self, task_kind: str
    ) -> None:
        assert get_completion_instructions(task_kind) == (
            get_coding_done_instructions()
        )

    @pytest.mark.parametrize(
        "task_kind",
        [TaskKind.REVIEW.value, TaskKind.RETROSPECTIVE_REVIEW.value],
    )
    def test_review_task_kinds_still_get_the_reviewer_document(
        self, task_kind: str
    ) -> None:
        assert get_completion_instructions(task_kind) == (
            get_reviewer_done_instructions()
        )


class TestTheCodingLaneRoleOwner:
    """`coding_lane_task_kind` is the ONE place the lane's role is decided.

    Three sites launch on the coding lane — the first launch, the
    validation-retry relaunch, and the rework relaunch — and each must ask this
    owner rather than answer with a literal (#385 round 1 F2, round 2 F1). Its
    contract has two halves: a tech-lead agent always resolves to the Tech Lead
    role, and every other agent keeps the lane's OWN coder kind, so routing a
    site through the owner cannot restate an Actor as something its lane is not.
    """

    @pytest.mark.parametrize(
        "lane_task_kind", [TaskKind.CODE, TaskKind.REWORK]
    )
    def test_a_tech_lead_agent_resolves_to_the_tech_lead_role_on_every_lane(
        self, lane_task_kind: TaskKind
    ) -> None:
        assert (
            coding_lane_task_kind(
                "agent:tech-lead",
                "agent:tech-lead",
                lane_task_kind=lane_task_kind,
            )
            == TaskKind.TECH_LEAD.value
        )

    @pytest.mark.parametrize(
        "lane_task_kind", [TaskKind.CODE, TaskKind.REWORK]
    )
    def test_every_other_agent_keeps_its_own_lane_kind(
        self, lane_task_kind: TaskKind
    ) -> None:
        assert (
            coding_lane_task_kind(
                "agent:tech-lead", "agent:web", lane_task_kind=lane_task_kind
            )
            == lane_task_kind.value
        )

    def test_the_default_lane_is_code(self) -> None:
        """The two pre-existing sites call it without naming a lane."""
        assert coding_lane_task_kind("agent:tech-lead", "agent:web") == (
            TaskKind.CODE.value
        )

    def test_an_unconfigured_tech_lead_agent_never_claims_the_role(self) -> None:
        """No tech lead configured means no agent can resolve to it."""
        assert (
            coding_lane_task_kind(
                None, "agent:tech-lead", lane_task_kind=TaskKind.REWORK
            )
            == TaskKind.REWORK.value
        )

    @pytest.mark.parametrize(
        "lane_task_kind", [TaskKind.CODE, TaskKind.REWORK]
    )
    def test_the_role_it_returns_selects_the_document_the_session_is_handed(
        self, lane_task_kind: TaskKind
    ) -> None:
        """The owner's answer and the protocol document are one decision.

        This is the join the three launch sites rely on: whatever role the owner
        names is the document ``get_completion_instructions`` hands the session,
        so a lane that asks the owner cannot hand a Tech Lead the coder protocol.
        """
        tech_lead_kind = coding_lane_task_kind(
            "agent:tech-lead", "agent:tech-lead", lane_task_kind=lane_task_kind
        )
        coder_kind = coding_lane_task_kind(
            "agent:tech-lead", "agent:web", lane_task_kind=lane_task_kind
        )

        assert get_completion_instructions(tech_lead_kind) == (
            get_tech_lead_done_instructions()
        )
        assert get_completion_instructions(coder_kind) == (
            get_coding_done_instructions()
        )


def _tech_lead_prompt_variants() -> dict[str, str]:
    """Every Tech Lead task prompt this repository ships or generates."""
    from issue_orchestrator.entrypoints.setup_wizard_prompts import (
        build_tech_lead_review_prompt_text,
    )

    return {
        "setup_wizard": build_tech_lead_review_prompt_text(
            "tech-lead-review", "tech-lead-reviewed"
        ),
        "examples": (
            REPO_ROOT / "examples" / "prompts" / "tech-lead-review.md"
        ).read_text(),
        "repo_specific": (
            REPO_ROOT / "repo-specific" / "prompts" / "tech-lead.md"
        ).read_text(),
    }


class TestNoTechLeadPromptReintroducesTheCommand:
    @pytest.mark.parametrize("variant", sorted(_tech_lead_prompt_variants()))
    def test_shipped_tech_lead_prompts_do_not_issue_it(self, variant: str) -> None:
        """A task prompt is the other place the obligation could creep back."""
        assert PREPUSH_IMPERATIVE not in _tech_lead_prompt_variants()[variant]


class TestTheReviewExchangeCoderLaneOwner:
    """The exchange's coder SIDE is a position; the principal is the authority.

    #385 left this lane deliberately unrepaired and wrote the reachability down
    as a known gap: a completion that offers a change for review starts an
    exchange with ``coder_label = agent_label``, and a tech-lead agent resolves
    a reviewer like any other, so a code-bearing Tech Lead really does sit on
    the coder side. #388 makes the lane ask WHO is sitting there.
    """

    def test_the_tech_lead_agent_is_the_tech_lead_principal_on_that_side(
        self,
    ) -> None:
        assert review_exchange_coder_principal("agent:tech-lead", "agent:tech-lead") is (
            ReviewExchangeCoderPrincipal.TECH_LEAD
        )

    def test_every_other_agent_is_an_actor_there(self) -> None:
        assert review_exchange_coder_principal("agent:tech-lead", "agent:web") is (
            ReviewExchangeCoderPrincipal.ACTOR
        )

    def test_an_unconfigured_tech_lead_agent_never_claims_the_side(self) -> None:
        assert review_exchange_coder_principal(None, "agent:tech-lead") is (
            ReviewExchangeCoderPrincipal.ACTOR
        )

    def test_the_principal_selects_the_document_that_side_is_handed(self) -> None:
        """One value, both consequences — they cannot be taken separately."""
        tech_lead = ReviewExchangeCoderPrincipal.TECH_LEAD
        actor = ReviewExchangeCoderPrincipal.ACTOR

        assert get_completion_instructions(tech_lead.task_kind) == (
            get_review_exchange_tech_lead_instructions()
        )
        assert get_completion_instructions(actor.task_kind) == (
            get_review_exchange_coder_instructions()
        )

    def test_the_actor_side_keeps_its_own_task_kind(self) -> None:
        """F5: nothing about the ordinary exchange coder lane moves."""
        assert ReviewExchangeCoderPrincipal.ACTOR.task_kind == (
            REVIEW_EXCHANGE_CODER_TASK_KIND
        )
        assert ReviewExchangeCoderPrincipal.TECH_LEAD.task_kind == (
            REVIEW_EXCHANGE_TECH_LEAD_TASK_KIND
        )

    def test_only_the_actor_files_its_own_turn_validation(self) -> None:
        assert ReviewExchangeCoderPrincipal.ACTOR.files_its_own_turn_validation
        assert not (
            ReviewExchangeCoderPrincipal.TECH_LEAD.files_its_own_turn_validation
        )

    def test_an_undeclared_or_unknown_principal_reads_back_as_the_actor(
        self,
    ) -> None:
        """Fail-safe: only the exact recorded value moves a lane's ownership."""
        for raw in (None, "", "   ", "tech lead", "TECH_LEAD", "nonsense"):
            assert ReviewExchangeCoderPrincipal.declared(raw) is (
                ReviewExchangeCoderPrincipal.ACTOR
            )
        assert ReviewExchangeCoderPrincipal.declared(" tech_lead ") is (
            ReviewExchangeCoderPrincipal.TECH_LEAD
        )


class TestTheExchangeTechLeadDocument:
    def test_it_never_issues_the_host_mutating_command(self) -> None:
        assert PREPUSH_IMPERATIVE not in get_review_exchange_tech_lead_instructions()

    def test_it_says_who_owns_the_validation_instead(self) -> None:
        exchange = _flowed(get_review_exchange_tech_lead_instructions())

        assert "Do not run `prepush-check`" in exchange
        assert "the orchestrator executes the mandatory completion validation" in (
            exchange
        )

    def test_it_is_still_the_exchange_protocol(self) -> None:
        """Same two steps; only the validation owner differs."""
        exchange = get_review_exchange_tech_lead_instructions()

        assert "coding-done completed" in exchange
        assert "exchange-respond ok" in exchange
        assert "coding-done needs_human" in exchange
        assert "coder_escalated_to_human" in exchange

    def test_it_is_none_of_the_other_completion_documents(self) -> None:
        exchange = get_review_exchange_tech_lead_instructions()

        assert exchange != get_review_exchange_coder_instructions()
        assert exchange != get_tech_lead_done_instructions()
        assert exchange != get_coding_done_instructions()
        assert exchange != get_reviewer_done_instructions()

    def test_the_actor_exchange_document_still_issues_the_command(self) -> None:
        """F5: the Actor lane's contract is untouched."""
        assert PREPUSH_IMPERATIVE in get_review_exchange_coder_instructions()


class TestTheExchangeTechLeadSandboxRole:
    def test_the_side_resolves_to_the_tech_lead_sandbox_role(self) -> None:
        scope = compute_session_scope(
            AgentConfig(prompt_path=Path("prompt.md"), sandbox=True),
            SandboxScopeContext(
                task_kind=REVIEW_EXCHANGE_TECH_LEAD_TASK_KIND,
                worktree=Path("/tmp/wt"),
            ),
        )
        actor_scope = compute_session_scope(
            AgentConfig(prompt_path=Path("prompt.md"), sandbox=True),
            SandboxScopeContext(
                task_kind=REVIEW_EXCHANGE_CODER_TASK_KIND,
                worktree=Path("/tmp/wt"),
            ),
        )

        # No authority widening: the computed scope is the coder's, byte for
        # byte. What the role buys is the completion protocol, not reach.
        assert scope == actor_scope
        assert SandboxRole.TECH_LEAD.value == "tech-lead"


class TestTheExchangeLaneRoutesItsOwnCompletionGate:
    """F2: the model runs no host-mutating gate merely by being on this lane."""

    def test_a_declared_tech_lead_principal_routes_validation_to_the_orchestrator(
        self, tmp_path: Path
    ) -> None:
        routing = route_completion_gate(
            tmp_path,
            exchange_coder=ReviewExchangeCoderPrincipal.TECH_LEAD,
        )

        assert routing.route is (
            CompletionGateRoute.TECH_LEAD_ORCHESTRATOR_OWNED_VALIDATION
        )
        assert not routing.runs_candidate_quick_gate

    def test_it_is_answered_before_the_run_directory_is_read(self) -> None:
        """The exchange run stages no tech-lead assignment to be found."""
        routing = route_completion_gate(
            None, exchange_coder=ReviewExchangeCoderPrincipal.TECH_LEAD
        )

        assert not routing.runs_candidate_quick_gate

    def test_an_actor_on_the_same_lane_still_runs_its_own_gate(
        self, tmp_path: Path
    ) -> None:
        routing = route_completion_gate(
            tmp_path, exchange_coder=ReviewExchangeCoderPrincipal.ACTOR
        )

        assert routing.route is CompletionGateRoute.CANDIDATE_QUICK_GATE

    def test_the_undeclared_default_is_the_ordinary_gate(self, tmp_path: Path) -> None:
        assert route_completion_gate(tmp_path).runs_candidate_quick_gate
