"""The Tech Lead completion protocol asks for nothing it may not do (#385).

A bounded Tech Lead is handed ``coding-done`` like a coder, and until #385 it
was handed the coder's DOCUMENT too — which makes ``prepush-check --dirty-only
-v`` mandatory. That command records timings under the repository's shared git
common dir, outside the session's sandbox write roots, so a live bounded run
(#383) died at exactly that step.

These tests pin the two halves of the repair at the source: the Tech Lead
document never issues the command, and the Actor/rework document still does.
The end-to-end direction — that what the launcher hands a tech-lead session is
this document — lives in ``tests/unit/test_session_launcher.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.resources import (
    get_coding_done_instructions,
    get_completion_instructions,
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

    def test_it_overrides_a_repo_prompt_that_asks_for_the_command(self) -> None:
        assert (
            "If a repository-specific task prompt tells you to run "
            "`prepush-check`, this instruction wins"
        ) in _flowed(get_tech_lead_done_instructions())


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
