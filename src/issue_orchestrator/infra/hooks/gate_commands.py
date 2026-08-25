"""The one vocabulary of build/test/validation entry points (#289).

``review_command_guard`` owned this list first, as a tuple of regex fragments
matched against the text of a Bash command. That was the only enforcement
dialect there was: the reviewer worktree is guarded through Claude Code's
``PreToolUse`` hook, which is handed a shell string.

A second principal now needs the same classification through a different
mechanism. A ``planning_investigation`` Tech Lead running under Codex is
refused gate commands by a launch-scoped Codex exec policy
(``adapters/worktree/_planning_command_guard``), and Codex's ``prefix_rule``
matches *argv tokens*, not command text — it has no regex at all.

Two enforcement substrates, one classification. This module is where "which
entry points start a build, test or validation run" is written down, once, in
a form both can render:

* :func:`shell_gate_patterns` renders the regex dialect the reviewer's hook
  has always used, and is the only source ``review_command_guard`` reads;
* :func:`codex_argv_patterns` renders the argv-prefix dialect the Codex
  installer turns into ``prefix_rule`` entries.

Adding a gate entry point means adding one :class:`GateCommand` here, and both
principals learn it. Removing one is how the mutation proof in #289 breaks the
planning refusal — which is the point of there being a single list.

**Threat model: forgetfulness, not evasion.** Inherited unchanged from the
reviewer guard. Both principals are cooperating agents that have been told in
their prompt why gates cannot run; the barrier exists so the instruction cannot
be quietly ignored, not so a determined process cannot get around it. Read this
list as "the ways a gate command gets run by accident", and do not build
anything on it that would need it to be exhaustive.

**The two dialects do not have equal reach, and that is recorded rather than
smoothed over.** The regex dialect can match a program invoked by path
(``.venv/bin/pytest``) or under a name prefix (``validate-pr-raw``); Codex's
``prefix_rule`` matches whole argv tokens and, measured on codex-cli 0.147.0,
does not match a path-prefixed program unless the caller passes
``--resolve-host-executables``. Every entry whose argv rendering is narrower
than its shell rendering says so in :attr:`GateCommand.argv_gap`, so the
narrowing is a documented property of this table instead of a surprise found
later at a refusal that did not happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ArgvPattern",
    "GATE_COMMANDS",
    "GateCommand",
    "codex_argv_patterns",
    "shell_gate_patterns",
]

#: One Codex ``prefix_rule`` pattern: the leading argv tokens that identify a
#: gate entry point. A tuple in a position means "any one of these", which is
#: the alternation ``prefix_rule`` accepts natively.
ArgvPattern = tuple["str | tuple[str, ...]", ...]


@dataclass(frozen=True)
class GateCommand:
    """One entry point that starts a build, test or validation run.

    ``shell_pattern`` is a regex fragment; the reviewer guard anchors it at a
    command position and allows a leading path, so it must not carry anchors of
    its own.

    ``argv_patterns`` is the same entry point as leading argv tokens. It is
    empty when the entry point cannot be expressed in that dialect at all, in
    which case ``argv_gap`` must say why — an entry that quietly renders to
    nothing is how a shared vocabulary stops being shared.
    """

    shell_pattern: str
    argv_patterns: tuple[ArgvPattern, ...] = ()
    argv_gap: str = ""

    def __post_init__(self) -> None:
        if not self.argv_patterns and not self.argv_gap:
            raise ValueError(
                f"GateCommand({self.shell_pattern!r}) renders to no Codex argv "
                "pattern and gives no reason; state the gap in argv_gap"
            )


# Node package managers: only the verbs that install or run something.
_NODE_VERBS = ("ci", "install", "i", "test", "run", "exec", "build", "start", "dlx")
_NODE_SHELL_VERBS = "|".join(_NODE_VERBS)

# Python gate runners, and the launchers that reach them.
_PYTHON_RUNNERS = ("pytest", "tox", "unittest", "nox")
_PYTHON_LAUNCHER_ARGV: tuple[tuple[str, ...], ...] = (
    ("python",),
    ("python3",),
    ("uvx",),
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("hatch", "run"),
)


def _node_gate(manager: str) -> GateCommand:
    return GateCommand(
        rf"{manager}\s+(?:{_NODE_SHELL_VERBS})\b",
        ((manager, _NODE_VERBS),),
    )


def _bare_gate(name: str, *, argv_gap: str = "") -> GateCommand:
    """An entry point that is a gate run whatever arguments follow it."""
    return GateCommand(rf"{re.escape(name)}\b", ((name,),), argv_gap=argv_gap)


def _python_launcher_argv() -> tuple[ArgvPattern, ...]:
    """``python -m pytest``/``uv run pytest``-shaped prefixes, both spellings."""
    patterns: list[ArgvPattern] = []
    for launcher in _PYTHON_LAUNCHER_ARGV:
        patterns.append((*launcher, "-m", _PYTHON_RUNNERS))
        patterns.append((*launcher, _PYTHON_RUNNERS))
    return tuple(patterns)


GATE_COMMANDS: tuple[GateCommand, ...] = (
    # Build-system entry points.
    GateCommand(r"gradlew(?:\.bat)?\b", (("gradlew",), ("gradlew.bat",))),
    _bare_gate("gradle"),
    _bare_gate("make"),
    _bare_gate("ninja"),
    _bare_gate("cmake"),
    GateCommand(
        r"bazel\s+(?:test|build|run|coverage)\b",
        (("bazel", ("test", "build", "run", "coverage")),),
    ),
    _bare_gate("mvn"),
    _bare_gate("sbt"),
    _bare_gate("rake"),
    GateCommand(
        r"cargo\s+(?:test|build|check|bench|run|clippy)\b",
        (("cargo", ("test", "build", "check", "bench", "run", "clippy")),),
    ),
    GateCommand(
        r"go\s+(?:test|build|vet|run)\b",
        (("go", ("test", "build", "vet", "run")),),
    ),
    GateCommand(
        r"dotnet\s+(?:test|build|run)\b",
        (("dotnet", ("test", "build", "run")),),
    ),
    _bare_gate("tox"),
    _node_gate("npm"),
    _node_gate("pnpm"),
    _node_gate("yarn"),
    _node_gate("bun"),
    # Ad-hoc package runners exist to execute a tool; nothing they can run in a
    # guarded worktree is a read of the candidate's source.
    _bare_gate("npx"),
    _bare_gate("bunx"),
    # A nested shell would otherwise carry a gate command past the regex
    # dialect, because the inner command is an argument rather than a command
    # position. It gets no argv rendering on purpose: Codex executes every
    # command it runs through a login shell of its own and classifies the
    # *parsed inner* command, so a rule naming a shell would either be inert or
    # refuse every command the principal runs, including the reads this guard
    # exists to keep available.
    GateCommand(
        r"(?:ba|z|k|da)?sh\s+(?:-\S+\s+)*-c\b",
        argv_gap=(
            "Codex classifies the parsed inner command of its own shell "
            "wrapper, so the inner entry point is matched on its own merits "
            "and a shell rule would refuse everything"
        ),
    ),
    # Python test/gate runners, direct and via a launcher.
    _bare_gate("pytest"),
    GateCommand(
        r"(?:python[\d.]*|uv\s+run|uvx|poetry\s+run|pipenv\s+run|hatch\s+run)"
        r"\s+(?:-\S+\s+)*(?:-m\s+)?(?:pytest|tox|unittest|nox)\b",
        _python_launcher_argv(),
        argv_gap=(
            "argv covers the `python`/`python3` spellings; a versioned "
            "interpreter name such as `python3.12` is matched by the regex "
            "dialect only"
        ),
    ),
    _bare_gate("nox"),
    GateCommand(r"bundle\s+exec\b", (("bundle", "exec"),)),
    # Static-analysis and browser-test tooling the gates drive.
    _bare_gate("ruff"),
    _bare_gate("pyright"),
    _bare_gate("mypy"),
    _bare_gate("semgrep"),
    _bare_gate("eslint"),
    _bare_gate("tsc"),
    _bare_gate("vitest"),
    _bare_gate("jest"),
    _bare_gate("playwright"),
    _bare_gate("lint-imports"),
    # This repository's own gate entry points.
    GateCommand(
        r"validate(?:-\S+)?\b",
        (("validate",),),
        argv_gap=(
            "prefix_rule matches whole argv tokens, so a `validate-<suffix>` "
            "script name is matched by the regex dialect only; the same "
            "targets are reached through `make`, which is covered"
        ),
    ),
    _bare_gate("prepush-check"),
    GateCommand(r"verify-pr(?:\.sh)?\b", (("verify-pr",), ("verify-pr.sh",))),
    _bare_gate(
        "quality_guardrails.py",
        argv_gap=(
            "usually invoked as `python tools/quality_guardrails.py`, whose "
            "argv carries a path Codex does not resolve to a basename rule; "
            "the regex dialect matches it through its path prefix"
        ),
    ),
)


def shell_gate_patterns() -> tuple[str, ...]:
    """The regex fragments, in declaration order.

    Returned as fragments rather than compiled patterns because the caller
    supplies the command-position and path-prefix anchoring that makes them
    mean "run this entry point" rather than "mention this word".
    """
    return tuple(entry.shell_pattern for entry in GATE_COMMANDS)


def codex_argv_patterns() -> tuple[ArgvPattern, ...]:
    """Every argv prefix a Codex ``prefix_rule`` should refuse, deduplicated.

    Order follows :data:`GATE_COMMANDS` so the generated policy file is
    deterministic: the same vocabulary renders byte-identical rules on every
    launch, which is what lets the written file be compared rather than
    guessed at.
    """
    seen: set[ArgvPattern] = set()
    patterns: list[ArgvPattern] = []
    for entry in GATE_COMMANDS:
        for pattern in entry.argv_patterns:
            if pattern in seen:
                continue
            seen.add(pattern)
            patterns.append(pattern)
    return tuple(patterns)
