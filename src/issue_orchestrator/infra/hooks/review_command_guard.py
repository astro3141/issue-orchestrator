"""Technical refusal of gate commands in the review-exchange worktree.

The persistent review-exchange reviewer worktree is deliberately created
without the repository's runtime prerequisites (``execution/reviewer_worktree``,
``docs/architecture/validation.md``). A build, test or validation command run
there fails on the missing prerequisite rather than on the change under review,
so the reviewer must not run one.

``docs/architecture/hooks.md`` settles how that "must not" is expressed:
prompts and checklists are suggestions, hooks are enforcement. This module is
the enforcement half — a ``PreToolUse`` policy installed into that worktree by
:func:`issue_orchestrator.adapters.worktree.api.install_review_command_guard`,
which refuses the command before it executes. The reviewer prompt keeps saying
*why*; it is no longer what the invariant rests on.

The policy is deliberately a command-name allowlist inversion: it matches the
entry points that start a build/test/validation run at a command position and
leaves reading the code — ``git``, ``rg``, ``cat``, ``ls``, ``find`` — and the
reviewer's own completion command untouched.

**Threat model: forgetfulness, not evasion.** The reviewer is a cooperating
agent that has been told in its prompt why gates cannot run here; the guard
exists so the instruction cannot be quietly ignored, not so a determined
process cannot get around it. It matches at command position and unwraps the
prefixes a reviewer would plausibly type by habit (env assignments,
``sudo``/``exec``, a path prefix, a pipeline, a nested ``sh -c``). Indirection
that only an agent trying to evade would reach for — ``env make test``,
``xargs make``, ``find . -exec make {} \\;`` — is out of scope by design. Read
this list as "the ways a gate command gets run by accident", and do not build
anything on it that would need it to be exhaustive.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .block_no_verify import (
    HookDecision,
    extract_command_from_input,
    format_copilot_response,
    format_cursor_response,
)

__all__ = [
    "GUARD_MODULE",
    "REFUSAL_REASON",
    "evaluate_raw_input",
    "evaluate_review_command",
    "main",
]

#: Dotted module path the installed hook invokes. Kept next to the policy so
#: the installer cannot name a module that no longer exists.
GUARD_MODULE = "issue_orchestrator.infra.hooks.review_command_guard"

REFUSAL_REASON = (
    "BLOCKED: this is the review-exchange reviewer worktree. It is created "
    "without the repository's runtime prerequisites (no virtualenv, no node "
    "modules, no browser binaries), so build, test and validation commands "
    "cannot produce a verdict about the change here — they fail on the missing "
    "prerequisite, waste the round's budget, and can hang on restricted "
    "networks. Review by reading the code; treat the coder's validation record "
    "as the authority on whether the gate passed."
)

# A command position: the start of the input, or immediately after a shell
# separator. Env-var assignments and a `command`/`exec`/`time` prefix are
# skipped so `FOO=1 make test` is still recognised as running `make`.
_COMMAND_START = r"(?:^|[\n;&|(`]|\$\()\s*(?:\w+=\S*\s+)*(?:(?:command|exec|time|nice|sudo)\s+)*"

# Optional leading path on an entry point invoked by path (`./gradlew`,
# `packages/vscode/node_modules/.bin/vitest`).
_PATH_PREFIX = r"(?:[\w.~/-]*/)?"

_GATE_COMMANDS: tuple[str, ...] = (
    # Build-system entry points.
    r"gradlew(?:\.bat)?\b",
    r"gradle\b",
    r"make\b",
    r"ninja\b",
    r"cmake\b",
    r"bazel\s+(?:test|build|run|coverage)\b",
    r"mvn\b",
    r"sbt\b",
    r"rake\b",
    r"cargo\s+(?:test|build|check|bench|run|clippy)\b",
    r"go\s+(?:test|build|vet|run)\b",
    r"dotnet\s+(?:test|build|run)\b",
    r"tox\b",
    # Node package managers: only the verbs that install or run something.
    r"(?:npm|pnpm|yarn|bun)\s+(?:ci|install|i|test|run|exec|build|start|dlx)\b",
    # Ad-hoc package runners exist to execute a tool; nothing they can run here
    # is a read of the candidate's source.
    r"(?:npx|bunx)\b",
    # A nested shell would otherwise carry a gate command past this policy,
    # because the inner command is an argument rather than a command position.
    r"(?:ba|z|k|da)?sh\s+(?:-\S+\s+)*-c\b",
    # Python test/gate runners, direct and via a launcher.
    r"pytest\b",
    r"(?:python[\d.]*|uv\s+run|uvx|poetry\s+run|pipenv\s+run|hatch\s+run)"
    r"\s+(?:-\S+\s+)*(?:-m\s+)?(?:pytest|tox|unittest|nox)\b",
    r"nox\b",
    r"bundle\s+exec\b",
    # Static-analysis and browser-test tooling the gates drive.
    r"(?:ruff|pyright|mypy|semgrep|eslint|tsc|vitest|jest|playwright)\b",
    r"lint-imports\b",
    # This repository's own gate entry points.
    r"validate(?:-\S+)?\b",
    r"prepush-check\b",
    r"verify-pr(?:\.sh)?\b",
    r"quality_guardrails\.py\b",
)

_GATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(_COMMAND_START + _PATH_PREFIX + entry) for entry in _GATE_COMMANDS
)


def evaluate_review_command(command: str) -> HookDecision:
    """Allow or refuse one Bash command run inside the reviewer worktree."""
    if not command:
        return HookDecision(True, "")
    for pattern in _GATE_PATTERNS:
        if pattern.search(command):
            return HookDecision(False, REFUSAL_REASON)
    return HookDecision(True, "")


def evaluate_raw_input(raw: str) -> HookDecision:
    """Evaluate raw hook JSON input and return an allow/deny decision.

    Input that carries no extractable command is refused: this guard is only
    ever installed in a worktree where a gate command is unrunnable, so an
    unreadable envelope must not become a way past it.
    """
    command = extract_command_from_input(raw)
    if raw and not command:
        return HookDecision(
            False,
            "BLOCKED: unable to extract command from hook input. Input may be malformed.",
        )
    return evaluate_review_command(command)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("claude", "cursor", "gemini", "copilot"), required=True
    )
    args = parser.parse_args(argv)

    decision = evaluate_raw_input(sys.stdin.read())

    if args.mode in ("claude", "gemini"):
        if not decision.allowed:
            print(decision.reason, file=sys.stderr)
        return decision.exit_code

    if args.mode == "cursor":
        print(format_cursor_response(decision))
        return 0

    print(format_copilot_response(decision))
    return 0


def orchestrator_source_root() -> Path:
    """The ``src`` directory this module is importable from.

    The installed hook pins ``PYTHONPATH`` to this path so the policy that runs
    is the orchestrator's own, never a copy carried by the worktree it guards.
    """
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    raise SystemExit(main())
