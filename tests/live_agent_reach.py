"""What it means for a test to actually reach a live provider (#194).

``tests/integration/AGENTS.md`` states the rule in prose: *"Deterministic
assertions that do not depend on a model belong in a non-``live_agent`` module
so they stay in blocking validation."* Prose is where it stayed. The first
guardrail written for it named two modules and five test functions by hand,
which proves those five survived their extraction and is structurally
incapable of noticing the sixth — and a sixth is exactly what happened:
``TestShellEscaping`` and the ``agent-done`` cases were carried out of every
blocking gate by ``pytest.mark.live_agent`` in ``test_claude_execution.py``'s
``pytestmark``, because that marker is module scope and takes the whole file.

This module is that rule with an owner. The claim is one direction of the
biconditional, and deliberately the checkable one:

    every test in a ``live_agent`` module must reach a live provider

The converse — every test that reaches a provider must be in a ``live_agent``
module — is guarded elsewhere and by a different mechanism (a provider spawn
inside a blocking module fails that gate the first time a model declines,
which is the failure #194 exists to remove, and the marker scan in
``tests/unit/test_makefile_validation_phases.py`` pins module-scope
declaration).

**Reach is structural, not semantic.** Nothing here runs a test or reads a
model's mind. A test reaches a provider when its body — including any nested
function it defines, and any module-level helper it calls — mentions one of the
registered names in :data:`PROVIDER_REACH_NAMES`: a provider CLI it puts in an
argv or a command string, a production seam that builds such an invocation, a
registered live-provider probe, or one of the lane's own outcome helpers
(``assert_no_breach`` / ``require_probe_ran``), which exist nowhere else.

Docstrings are excluded. Prose about Claude is not evidence that the test calls
it, and the pre-#194 tree is full of deterministic cases whose docstrings
describe the provider they do not spawn.

**The rule fails closed.** A live-agent test that reaches its provider through
some route none of the registered names cover is reported as an offender, and
the fix is to register the route here rather than to loosen the check. A false
positive costs one edit in a named place; a false negative costs a silently
unrun test, which is the defect this module exists to prevent.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .fixtures.live_agent_cli import LIVE_PROVIDER_PROBES

LIVE_PROVIDER_COMMANDS = ("claude", "codex")
"""Provider CLI executables. A test naming one in an argv or command string is
spawning it — which is the ``live_agent`` marker's own criterion, per
``tests/integration/AGENTS.md``: *"A module that spawns a real provider CLI must
declare pytest.mark.live_agent at module scope."* Note this is about the
provider, not about the model: ``codex --version`` needs no model but does need
the operator's installed CLI, and a CLI upgrade that changed its ``--help``
wording must not be able to fail an unrelated candidate."""

LIVE_PROVIDER_SEAMS = (
    "CodexProvider",
    "ClaudeCodeAdapter",
    "build_claude_sandbox_argv",
)
"""Production objects that build or issue a provider invocation on the test's
behalf, so the CLI name never appears in the test body. Registered here rather
than matched by shape, because "this call ends in a provider spawn" is a fact
about production code, not something an AST can infer."""

LANE_OUTCOME_HELPERS = ("assert_no_breach", "require_probe_ran")
"""``tests/live_assurance``'s vocabulary. These say which of the assurance
lane's three outcomes an assertion means and have no purpose outside a
live-agent probe, so using one is itself a statement that the boundary in
question is exercised by a real provider."""

PROVIDER_REACH_NAMES = frozenset(
    LIVE_PROVIDER_COMMANDS
    + LIVE_PROVIDER_SEAMS
    + LANE_OUTCOME_HELPERS
    + LIVE_PROVIDER_PROBES
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _body_without_docstring(node: _FunctionNode) -> list[ast.stmt]:
    body = node.body
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _names_referenced(node: _FunctionNode) -> set[str]:
    """Every identifier the body mentions, including inside string literals.

    String literals count because that is how a provider invocation is
    written: ``["claude", "--print", prompt]`` and ``f"claude -p {prompt}"``
    both put the executable in a constant, and an f-string's literal parts are
    ordinary ``Constant`` nodes under the same subtree.
    """
    names: set[str] = set()
    for statement in _body_without_docstring(node):
        for child in ast.walk(statement):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                names.update(_IDENTIFIER.findall(child.value))
    return names


def _module_functions(tree: ast.Module) -> dict[str, _FunctionNode]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _reaches_provider(
    node: _FunctionNode,
    helpers: dict[str, _FunctionNode],
    seen: set[str],
) -> bool:
    names = _names_referenced(node)
    if names & PROVIDER_REACH_NAMES:
        return True
    for helper_name in sorted(names & helpers.keys()):
        if helper_name in seen:
            continue
        seen.add(helper_name)
        if _reaches_provider(helpers[helper_name], helpers, seen):
            return True
    return False


def collected_tests(tree: ast.Module) -> list[tuple[str, _FunctionNode]]:
    """``(nodeid suffix, function)`` for every test pytest would collect."""
    found: list[tuple[str, _FunctionNode]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test"):
                found.append((node.name, node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for member in node.body:
                if isinstance(
                    member, ast.FunctionDef | ast.AsyncFunctionDef
                ) and member.name.startswith("test"):
                    found.append((f"{node.name}::{member.name}", member))
    return found


def missing_provider_reach(source: str, *, filename: str) -> tuple[str, ...]:
    """The tests in ``source`` that show no evidence of reaching a provider.

    Takes source rather than a path so the rule can be proven non-vacuous
    against a synthetic module, without planting a deliberately-broken file in
    the tree for a guardrail to find.
    """
    tree = ast.parse(source, filename=filename)
    helpers = _module_functions(tree)
    return tuple(
        name
        for name, node in collected_tests(tree)
        if not _reaches_provider(node, helpers, set())
    )


def missing_provider_reach_in(path: Path) -> tuple[str, ...]:
    """:func:`missing_provider_reach` for a module on disk."""
    return missing_provider_reach(
        path.read_text(encoding="utf-8"), filename=str(path)
    )


__all__ = [
    "LANE_OUTCOME_HELPERS",
    "LIVE_PROVIDER_COMMANDS",
    "LIVE_PROVIDER_SEAMS",
    "PROVIDER_REACH_NAMES",
    "collected_tests",
    "missing_provider_reach",
    "missing_provider_reach_in",
]
