"""The review-exchange worktree's gate-command refusal (#48).

The reviewer worktree is created without the repository's runtime
prerequisites, so a gate command run there reports on the environment while the
record says it reports on the candidate. `docs/architecture/hooks.md` says a
prompt cannot be what prevents that — a hook must. These tests pin the policy
that hook applies: what it refuses, what it must keep allowing, and that an
envelope it cannot read is a refusal rather than a way past it.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from issue_orchestrator.infra.hooks.review_command_guard import (
    GUARD_MODULE,
    REFUSAL_REASON,
    evaluate_raw_input,
    evaluate_review_command,
    main,
    orchestrator_source_root,
)

GATE_COMMANDS = [
    "make validate-pr-raw",
    "make -C /repo worktree-setup",
    "./gradlew test",
    "gradle build",
    "npm ci",
    "npm test",
    "pnpm install",
    "yarn build",
    "npx playwright test",
    "pytest tests/unit -q",
    ".venv/bin/python -m pytest tests/unit",
    "uv run pytest",
    "tox",
    "cargo test",
    "mvn -q verify",
    "bazel test //...",
    "go test ./...",
    "dotnet test",
    "ruff check src",
    "pyright",
    "semgrep --config auto",
    "./scripts/gates/verify-pr.sh",
    "prepush-check --dirty-only -v",
    "validate",
    # Composed and prefixed forms reach the same entry points.
    "cd packages/vscode && npm run build",
    "FOO=1 make typecheck",
    "sudo make install",
    'bash -c "make test"',
    "git diff | head -5; pytest",
]

READING_COMMANDS = [
    "git log --oneline -20",
    "git diff main...HEAD",
    "git show HEAD:Makefile",
    "rg -n 'def provision' src",
    "grep -rn make src",
    "cat package.json",
    "sed -n 1,40p src/x.py",
    "ls -la src",
    "find . -name '*.py'",
    "echo make it clearer",
    "reviewer-done changes_requested --issues 'x' --risk low",
]


class TestGateCommandsAreRefused:
    """The reviewer cannot start a build, test or validation run here."""

    @pytest.mark.parametrize("command", GATE_COMMANDS)
    def test_gate_entry_points_are_refused(self, command: str) -> None:
        decision = evaluate_review_command(command)

        assert decision.allowed is False
        assert decision.exit_code == 2

    def test_refusal_says_why_and_what_to_do_instead(self) -> None:
        decision = evaluate_review_command("make validate-pr-raw")

        assert decision.reason == REFUSAL_REASON
        assert "not provisioned" not in decision.reason  # states the fact plainly
        assert "runtime prerequisites" in decision.reason
        assert "Review by reading the code" in decision.reason


class TestReadingTheCodeStaysPossible:
    """A refusal that also blocks reviewing would be worse than the problem."""

    @pytest.mark.parametrize("command", READING_COMMANDS)
    def test_reading_and_completion_commands_are_allowed(self, command: str) -> None:
        assert evaluate_review_command(command).allowed is True

    def test_empty_command_is_allowed(self) -> None:
        assert evaluate_review_command("").allowed is True


class TestHookEnvelope:
    def test_command_is_read_from_the_claude_tool_input(self) -> None:
        raw = json.dumps({"tool_input": {"command": "pytest"}})

        assert evaluate_raw_input(raw).allowed is False

    def test_unreadable_envelope_is_refused_rather_than_waved_through(self) -> None:
        decision = evaluate_raw_input("not json at all")

        assert decision.allowed is False
        assert "unable to extract command" in decision.reason

    def test_no_input_at_all_allows(self) -> None:
        assert evaluate_raw_input("").allowed is True

    def test_claude_mode_exits_two_and_explains_on_stderr(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(
            "sys.stdin",
            _StubStdin(json.dumps({"tool_input": {"command": "make test"}})),
        )

        exit_code = main(["--mode", "claude"])

        assert exit_code == 2
        assert REFUSAL_REASON in capsys.readouterr().err

    def test_claude_mode_allows_a_reading_command(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sys.stdin",
            _StubStdin(json.dumps({"tool_input": {"command": "git log"}})),
        )

        assert main(["--mode", "claude"]) == 0


class TestRunnableAsAHook:
    """The installed hook invokes this module as a script; prove that works."""

    def test_module_entry_point_blocks_a_gate_command(self) -> None:
        result = _run_guard_module("make validate-pr-raw")

        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_module_entry_point_allows_reading(self) -> None:
        assert _run_guard_module("git log --oneline").returncode == 0


class _StubStdin:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> str:
        return self._payload


def _run_guard_module(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", GUARD_MODULE, "--mode", "claude"],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(orchestrator_source_root()), "PATH": "/usr/bin:/bin"},
    )
