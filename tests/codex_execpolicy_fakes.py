"""Exec-policy checkers that stand in for the installed Codex CLI.

Two principals establish a worktree-local Codex gate policy through the same
mechanism — a ``planning_investigation`` Tech Lead (#289) and the
review-exchange reviewer (#396) — so the doubles their proofs measure against
live here rather than beside either one. A checker that behaved slightly
differently in the two suites would let one principal's guard drift while both
suites still passed.

:class:`StarlarkPrefixExecPolicy` is the important one: it reads the generated
``prefix_rule`` patterns back out of the file that was actually written and
applies Codex's documented prefix semantics, so a test cannot pass by agreeing
with itself about what the policy says — only about what Codex would do with
it, which the live integration modules re-measure against the real CLI. The
other three are the failure directions a guard must fail closed on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from issue_orchestrator.adapters.hooks import (
    ExecPolicyOutcome,
    ExecPolicyResultError,
)

__all__ = [
    "AlwaysAllowingExecPolicy",
    "SafetyBlindExecPolicy",
    "StarlarkPrefixExecPolicy",
    "UnanswerableExecPolicy",
]

_PATTERN_LINE = re.compile(r"^\s*pattern = (\[.*\]),\s*$")


class StarlarkPrefixExecPolicy:
    """Classifies a command against the rules file that was actually written.

    Reads the generated ``prefix_rule`` patterns back out of the file and
    applies Codex's documented prefix semantics (literal argv tokens, a list in
    one position meaning "any of these"). Nothing is stubbed per command, so
    tests using it cannot pass by agreeing with themselves about what the policy
    says.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []
        self.asked_files: list[Path] = []

    @staticmethod
    def _patterns(rules_file: Path) -> list[list[object]]:
        patterns: list[list[object]] = []
        for line in rules_file.read_text(encoding="utf-8").splitlines():
            match = _PATTERN_LINE.match(line)
            if match:
                patterns.append(eval(match.group(1)))  # noqa: S307 - generated literal
        return patterns

    @staticmethod
    def _matches(pattern: list[object], command: Sequence[str]) -> bool:
        if len(pattern) > len(command):
            return False
        for expected, actual in zip(pattern, command):
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        self.asked.append(tuple(command))
        self.asked_files.append(rules_file)
        for pattern in self._patterns(rules_file):
            if self._matches(pattern, command):
                return ExecPolicyOutcome.FORBIDDEN
        return ExecPolicyOutcome.NO_MATCH


class SafetyBlindExecPolicy(StarlarkPrefixExecPolicy):
    """Classifies the scoped policy normally; the safety policy refuses nothing.

    The shape of a shipped ``orchestrator.rules`` that arrived empty, truncated
    or superseded — the case a copy-and-return installer cannot distinguish
    from a working one.
    """

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        if rules_file.name == "orchestrator.rules":
            self.asked.append(tuple(command))
            self.asked_files.append(rules_file)
            return ExecPolicyOutcome.NO_MATCH
        return super().check(rules_file, command)


class AlwaysAllowingExecPolicy:
    """A mechanism that enforces nothing — the decorative-guard direction."""

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        return ExecPolicyOutcome.NO_MATCH


class UnanswerableExecPolicy:
    """A mechanism that cannot classify at all."""

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        raise ExecPolicyResultError("codex execpolicy check exited 1")
