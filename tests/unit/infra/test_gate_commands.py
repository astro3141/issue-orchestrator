"""The one gate-command vocabulary, in both enforcement dialects (#289).

Two principals are now refused build/test/validation commands through two
different mechanisms — the reviewer worktree through a Claude Code
``PreToolUse`` hook that sees command *text*, a planning Tech Lead through a
Codex exec policy that sees *argv tokens*. #289 requires them to consume one
classification, not two lists that happen to agree today.

These tests pin that: every entry renders into both dialects (or says why it
cannot), the two dialects agree about the commands #289 names, and the link is
load-bearing — dropping an entry from the vocabulary is measured to break the
planning refusal, which is the mutation direction the issue asks to be
observable.
"""

from __future__ import annotations

import re

import pytest

from issue_orchestrator.infra.hooks.gate_commands import (
    GATE_COMMANDS,
    GateCommand,
    codex_argv_patterns,
    shell_gate_patterns,
)
from issue_orchestrator.infra.hooks.review_command_guard import (
    evaluate_review_command,
)

#: Commands #289 pins as the acceptance direction, in both dialects.
PINNED_GATE_COMMANDS = [
    ("make validate-pr-raw", ("make", "validate-pr-raw")),
    ("pytest -q tests/unit", ("pytest", "-q", "tests/unit")),
    ("python -m pytest", ("python", "-m", "pytest")),
]

PINNED_INSPECTION_COMMANDS = [
    ("git log --oneline -20", ("git", "log", "--oneline", "-20")),
    ("rg -n planning_investigation src", ("rg", "-n", "planning_investigation", "src")),
    ("cat AGENTS.md", ("cat", "AGENTS.md")),
]


def _argv_matches(pattern, command: tuple[str, ...]) -> bool:
    """Reproduce Codex ``prefix_rule`` semantics: a literal argv prefix match."""
    if len(pattern) > len(command):
        return False
    for expected, actual in zip(pattern, command):
        if isinstance(expected, tuple):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _refused_by_argv(command: tuple[str, ...]) -> bool:
    return any(
        _argv_matches(pattern, command) for pattern in codex_argv_patterns()
    )


class TestOneVocabularyTwoDialects:
    def test_every_entry_renders_a_shell_pattern(self) -> None:
        assert len(shell_gate_patterns()) == len(GATE_COMMANDS)
        for fragment in shell_gate_patterns():
            re.compile(fragment)

    def test_an_entry_that_renders_no_argv_must_say_why(self) -> None:
        for entry in GATE_COMMANDS:
            assert entry.argv_patterns or entry.argv_gap, entry.shell_pattern

    def test_an_undocumented_argv_gap_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="gives no reason"):
            GateCommand(r"totally-new-gate\b")

    def test_argv_rendering_is_deterministic_and_deduplicated(self) -> None:
        first = codex_argv_patterns()
        assert first == codex_argv_patterns()
        assert len(first) == len(set(first))


class TestTheTwoDialectsAgreeOnWhatMatters:
    @pytest.mark.parametrize(("text", "argv"), PINNED_GATE_COMMANDS)
    def test_pinned_gate_commands_are_refused_in_both(
        self, text: str, argv: tuple[str, ...]
    ) -> None:
        assert evaluate_review_command(text).allowed is False
        assert _refused_by_argv(argv) is True

    @pytest.mark.parametrize(("text", "argv"), PINNED_INSPECTION_COMMANDS)
    def test_pinned_inspection_commands_are_allowed_in_both(
        self, text: str, argv: tuple[str, ...]
    ) -> None:
        assert evaluate_review_command(text).allowed is True
        assert _refused_by_argv(argv) is False


class TestTheSharedLinkIsLoadBearing:
    """The mutation direction #289 asks to be observable.

    Breaking the classifier link — removing ``make`` from the vocabulary — must
    make the planning dialect stop recognising the pinned gate command. If this
    passes while the vocabulary is empty, the planning guard is reading
    something else.
    """

    def test_dropping_an_entry_drops_the_planning_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _refused_by_argv(("make", "validate-pr-raw")) is True
        without_make = tuple(
            entry for entry in GATE_COMMANDS if entry.shell_pattern != r"make\b"
        )
        assert len(without_make) == len(GATE_COMMANDS) - 1
        monkeypatch.setattr(
            "issue_orchestrator.infra.hooks.gate_commands.GATE_COMMANDS",
            without_make,
        )
        assert _refused_by_argv(("make", "validate-pr-raw")) is False
