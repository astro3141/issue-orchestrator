"""One owner decides whether a checkout is publishable (#385).

``prepush-check`` used to be the only caller, so the mode vocabulary and the
fail-closed direction lived inside the CLI. #385 gave it a second caller — the
trusted Tech Lead completion validator, which runs the same question in the
orchestrator's process — and two copies of "what counts as dirty" is exactly
the drift that lets two gates disagree about one checkout.

These tests hold the extracted owner to the CLI's pre-existing behaviour, and
to the direction it fails in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.infra.dirty_tree_guard import (
    DEFAULT_DIRTY_CHECK_MODE,
    DIRTY_CHECK_MODES,
    DirtyTreeVerdict,
    run_dirty_tree_guard,
)


class FakeWorkingCopy:
    def __init__(self, dirty: list[str] | None) -> None:
        self._dirty = dirty
        self.modes: list[str] = []

    def get_head_sha(self, worktree: Path) -> str | None:  # pragma: no cover
        return "a" * 40

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        self.modes.append(mode)
        return self._dirty


WORKTREE = Path("/repo")


class TestTheVocabularyHasOneDefinition:
    def test_the_documented_modes_are_the_accepted_modes(self) -> None:
        assert set(DIRTY_CHECK_MODES) == {"tracked", "unstaged", "all", "off"}

    def test_the_default_is_tracked(self) -> None:
        assert DEFAULT_DIRTY_CHECK_MODE == "tracked"


class TestPublishableIsAPropertyOfTheVerdict:
    @pytest.mark.parametrize(
        "verdict, publishable",
        [
            (DirtyTreeVerdict.CLEAN, True),
            (DirtyTreeVerdict.DISABLED, True),
            (DirtyTreeVerdict.DIRTY, False),
            (DirtyTreeVerdict.UNENUMERABLE, False),
            (DirtyTreeVerdict.INVALID_MODE, False),
        ],
    )
    def test_each_member_answers_it(
        self, verdict: DirtyTreeVerdict, publishable: bool
    ) -> None:
        assert verdict.publishable is publishable


class TestTheGuardsAnswers:
    def test_a_clean_checkout_is_publishable(self) -> None:
        result = run_dirty_tree_guard(
            WORKTREE, mode="tracked", working_copy=FakeWorkingCopy([])
        )

        assert result.verdict is DirtyTreeVerdict.CLEAN
        assert result.publishable

    def test_the_mode_is_the_one_that_reaches_the_working_copy(self) -> None:
        working_copy = FakeWorkingCopy([])

        run_dirty_tree_guard(WORKTREE, mode="all", working_copy=working_copy)

        assert working_copy.modes == ["all"]

    def test_dirty_content_is_reported_and_refused(self) -> None:
        result = run_dirty_tree_guard(
            WORKTREE,
            mode="tracked",
            working_copy=FakeWorkingCopy(["src/a.py", "src/b.py"]),
        )

        assert result.verdict is DirtyTreeVerdict.DIRTY
        assert result.dirty_files == ("src/a.py", "src/b.py")
        assert "src/a.py" in result.detail

    def test_runtime_managed_metadata_is_not_dirt(self) -> None:
        """The CLI's long-standing exclusion, kept in the shared owner."""
        result = run_dirty_tree_guard(
            WORKTREE,
            mode="tracked",
            working_copy=FakeWorkingCopy([".issue-orchestrator/state/x.json"]),
        )

        assert result.verdict is DirtyTreeVerdict.CLEAN

    def test_off_is_the_operators_explicit_opt_out(self) -> None:
        working_copy = FakeWorkingCopy(["src/a.py"])

        result = run_dirty_tree_guard(
            WORKTREE, mode="off", working_copy=working_copy
        )

        assert result.verdict is DirtyTreeVerdict.DISABLED
        assert result.publishable
        assert working_copy.modes == []

    def test_an_unknown_mode_is_refused_never_defaulted(self) -> None:
        working_copy = FakeWorkingCopy([])

        result = run_dirty_tree_guard(
            WORKTREE, mode="traked", working_copy=working_copy
        )

        assert result.verdict is DirtyTreeVerdict.INVALID_MODE
        assert not result.publishable
        assert working_copy.modes == []

    def test_a_failed_enumeration_fails_closed(self) -> None:
        result = run_dirty_tree_guard(
            WORKTREE, mode="tracked", working_copy=FakeWorkingCopy(None)
        )

        assert result.verdict is DirtyTreeVerdict.UNENUMERABLE
        assert not result.publishable
