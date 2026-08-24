"""Tests for Makefile validation phase orchestration."""

import ast
import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.live_agent_cli import LIVE_PROVIDER_PROBES
from tests.live_agent_reach import (
    PROVIDER_REACH_NAMES,
    collected_tests,
    missing_provider_reach,
    missing_provider_reach_in,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


@functools.cache
def _gnu_make() -> str:
    """The binary the Makefile's ``GMAKE :=`` would resolve to, here.

    Deliberately the same search the Makefile runs
    (``command -v gmake || command -v make``): a Homebrew macOS box answers
    ``gmake`` and a Linux CI runner answers ``make``, and every recipe below
    is printed with whichever one that is substituted in.
    """
    make_bin = shutil.which("gmake") or shutil.which("make")
    if make_bin is None:
        pytest.fail("GNU make is required to validate Makefile targets")
    result = subprocess.run(
        [make_bin, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or "GNU Make" not in result.stdout:
        pytest.fail("GNU make is required to validate Makefile targets")
    return make_bin


# These guardrails describe the *tracked* target graph — what this repository's
# Makefile selects — so the dry run must not inherit a caller's override of a
# variable the Makefile exposes with `?=`. GNU make exports a command-line
# override into every recipe's environment, so a gate invoked as
# `make validate-pr-raw PYTEST='... --deselect <nodeid>'` (which is exactly how
# #194's STEP A bootstrap pinned live-agent segregation from the engine, before
# the marker semantic landed here) hands that value to pytest, and pytest hands
# it on to the `make -n` below. The dry run would then describe the caller's
# selector rather than the Makefile's, and an assertion about what the publish
# gate names would be answering the wrong question. Dropping these lets each
# variable fall back to the Makefile's own default.
_AMBIENT_MAKE_VARIABLES = ("MAKEFLAGS", "MAKEOVERRIDES", "PYTEST")


def _dry_run(target: str, **overrides: str) -> list[str]:
    env = dict(os.environ)
    for name in _AMBIENT_MAKE_VARIABLES:
        env.pop(name, None)
    env.update(
        {
            "VALIDATE_JOBS": "10",
            "VALIDATE_TEST_JOBS": "1",
            # Deliberately not 1: the web phase used to be scheduled by a
            # second knob (VALIDATE_LIVE_WEB_JOBS) that existed only because it
            # shared the phase with the real-Codex check. A distinct value here
            # is what proves the phase now reads the knob TROUBLESHOOTING.md
            # documents rather than a neighbour's default.
            "VALIDATE_WEB_JOBS": "2",
            "VALIDATE_AGENT_JOBS": "1",
            "VALIDATE_E2E_JOBS": "1",
            **overrides,
        }
    )
    result = subprocess.run(
        [_gnu_make(), "-n", "--always-make", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _matching_indexes(lines: list[str], *fragments: str) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if all(fragment in line for fragment in fragments)
    ]


def _find_line(lines: list[str], *fragments: str) -> int:
    matches = _matching_indexes(lines, *fragments)
    if not matches:
        raise AssertionError(
            f"Missing line containing {fragments!r}. Output:\n" + "\n".join(lines)
        )
    if len(matches) > 1:
        raise AssertionError(
            f"Expected one line containing {fragments!r}, got {len(matches)}"
        )
    return matches[0]


def _assert_job_count(line: str, jobs: int) -> None:
    assert re.search(rf"(?:^|\s)-j\s*{jobs}(?:\s|$)", line), line


def _assert_no_job_count(line: str) -> None:
    assert not re.search(r"(?:^|\s)-j\s*\d+(?:\s|$)", line), line


def _makefile_text() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _makefile_variable_words(name: str) -> list[str]:
    match = re.search(
        rf"^{re.escape(name)}\s*:?=\s*(.+)$",
        _makefile_text(),
        re.MULTILINE,
    )
    assert match is not None, f"Makefile variable {name} not found"
    return match.group(1).split()


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _has_marker(path: Path, marker: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        _dotted_name(node) == f"pytest.mark.{marker}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    )


def _module_scope_markers(tree: ast.Module) -> set[str]:
    """Marker names a ``pytestmark`` assignment applies to the whole file."""
    markers: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        for element in ast.walk(node.value):
            dotted = _dotted_name(element) if isinstance(element, ast.Attribute) else None
            if dotted is not None and dotted.startswith("pytest.mark."):
                markers.add(dotted.removeprefix("pytest.mark."))
    return markers


def _markers_on(node: ast.AST, module_markers: set[str]) -> set[str]:
    """Every marker pytest would apply to one collected test."""
    markers = set(module_markers)
    decorators = getattr(node, "decorator_list", [])
    for decorator in decorators:
        for element in ast.walk(decorator):
            dotted = _dotted_name(element) if isinstance(element, ast.Attribute) else None
            if dotted is not None and dotted.startswith("pytest.mark."):
                markers.add(dotted.removeprefix("pytest.mark."))
    return markers


def _tests_declaring(path: Path, marker: str) -> tuple[str, ...]:
    """Nodeid suffixes in ``path`` that carry ``marker``, module scope included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_markers = _module_scope_markers(tree)
    return tuple(
        name
        for name, node in collected_tests(tree)
        if marker in _markers_on(node, module_markers)
    )


def _marker_expressions(lines: list[str]) -> list[str]:
    """Every ``-m "<expr>"`` selector in a dry run, as its expression text."""
    return [
        match.group(1)
        for line in lines
        for match in re.finditer(r'-m\s+"([^"]*)"', line)
    ]


def _positively_selects(expression: str, marker: str) -> bool:
    """Whether ``expression`` selects ``marker`` rather than deselecting it."""
    return (
        re.search(rf"(?<!not )\b{re.escape(marker)}\b", expression) is not None
    )


@functools.cache
def _submake() -> re.Pattern[str]:
    """Nested sub-make invocations, named the way *this machine* prints them.

    The Makefile substitutes ``$(GMAKE)`` — the resolved binary, as an absolute
    path — into every phase recipe, so the dry run says ``.../gmake`` where one
    is installed and ``.../make`` where one is not. Matching the literal
    ``gmake`` therefore expanded nothing on a Linux CI runner while expanding
    everything on a Homebrew macOS box: the same assertions read a different
    graph on each. Anchor the pattern to the binary `_gnu_make` resolved, which
    is the same search ``GMAKE :=`` runs, and both describe the same graph.
    """
    return re.compile(
        re.escape(_gnu_make())
        + r"\s+((?:(?:-\S+|--\S+)\s+)*)([\w.\-]+(?:\s+[\w.\-]+)*)\s*;"
    )


def _dry_run_closure(target: str, **overrides: str) -> list[str]:
    """Every command the phased target graph would run, phases expanded.

    ``make -n`` on a phase target prints the nested sub-make invocations
    without expanding them, so asserting an absence against that output is
    vacuous — which is exactly how a live-agent lane could creep back into the
    publish gate unnoticed. This follows each sub-make and returns the union.

    Expanding nothing is the same vacuum by another route, so it is refused
    here rather than left to surface as whichever assertion happened to be
    reading the un-expanded output.
    """
    seen: set[str] = set()
    collected: list[str] = []

    def walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        lines = _dry_run(name, **overrides)
        collected.extend(lines)
        for line in lines:
            for _flags, names in _submake().findall(line):
                for nested in names.split():
                    walk(nested)

    walk(target)
    assert len(seen) > 1, (
        f"{target} expanded to no sub-make: the closure is just its own dry "
        f"run, and every absence asserted against it would be vacuous "
        f"(looked for {_gnu_make()!r} invocations)"
    )
    return collected


def _integration_modules_declaring(marker: str) -> list[Path]:
    return sorted(
        path
        for path in (REPO_ROOT / "tests" / "integration").rglob("test_*.py")
        if _has_marker(path, marker)
    )


def _integration_modules() -> list[Path]:
    return sorted((REPO_ROOT / "tests" / "integration").rglob("test_*.py"))


class _ImportTimeCalls(ast.NodeVisitor):
    """The functions a module calls while it is being *imported*.

    A function body does not run at import; everything at module and class
    scope does, and so do decorator expressions and default arguments — which
    is where a ``@pytest.mark.skipif(not probe(), ...)`` would hide.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - pytest/ast API
        dotted = _dotted_name(node.func)
        if dotted is not None:
            self.names.add(dotted.rsplit(".", 1)[-1])
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_signature_only(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_signature_only(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        self._visit_defaults(node.args)

    def _visit_signature_only(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_defaults(node.args)

    def _visit_defaults(self, args: ast.arguments) -> None:
        for default in (*args.defaults, *args.kw_defaults):
            if default is not None:
                self.visit(default)


def _import_time_calls(path: Path) -> set[str]:
    visitor = _ImportTimeCalls()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.names


def _calls_in(node: ast.AST) -> set[str]:
    """The functions called anywhere inside one AST node."""
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        dotted = _dotted_name(child.func)
        if dotted is not None:
            names.add(dotted.rsplit(".", 1)[-1])
    return names


def _any_scope_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _calls_in(tree)


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function is decorated ``@pytest.fixture`` (bare or called)."""
    for decorator in node.decorator_list:
        for element in ast.walk(decorator):
            dotted = _dotted_name(element)
            if dotted is not None and dotted.startswith("pytest.fixture"):
                return True
    return False


def _fixture_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Module-level ``@pytest.fixture`` definitions, by fixture name."""
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_fixture(node)
    }


def test_validate_impl_runs_core_phases_with_separate_job_caps():
    lines = _dry_run("_validate-impl")

    static_index = _find_line(lines, "validate-static-phase", "_validate-static-impl")
    core_tests_index = _find_line(
        lines,
        "validate-core-tests-phase",
        "_validate-core-tests-impl",
    )
    web_index = _find_line(lines, "validate-web-phase", "test-web")

    _assert_job_count(lines[static_index], 10)
    _assert_job_count(lines[core_tests_index], 1)
    _assert_job_count(lines[web_index], 2)

    assert static_index < core_tests_index < web_index


def test_validate_pr_impl_runs_agent_phase_after_validate_phase():
    lines = _dry_run("_validate-pr-impl")

    validate_index = _find_line(lines, "validate-main-phase", "_validate-impl")
    agent_index = _find_line(lines, "validate-agent-phase", "_validate-agent-impl")

    _assert_job_count(lines[agent_index], 1)

    assert validate_index < agent_index


def test_validate_full_impl_runs_e2e_after_pr_phase():
    lines = _dry_run("_validate-full-impl")

    pr_index = _find_line(lines, "_validate-pr-impl")
    e2e_index = _find_line(lines, "test-e2e")

    _assert_job_count(lines[e2e_index], 1)

    assert pr_index < e2e_index


def test_validate_pr_raw_does_not_schedule_entire_graph_at_validate_jobs():
    lines = _dry_run("validate-pr-raw")
    raw_pr_index = _find_line(lines, "_validate-pr-impl")

    _assert_no_job_count(lines[raw_pr_index])


def test_validate_pr_raw_does_not_reenter_cache_aware_verify_script():
    # validation.publish.cmd points at `make validate-pr-raw`, which is what the
    # cache-aware wrapper (scripts/verify-pr.sh) ultimately runs. If the raw
    # target invoked verify-pr.sh again the pre-push gate would recurse.
    lines = _dry_run("validate-pr-raw")

    assert all("verify-pr.sh" not in line for line in lines)


def test_validate_pr_uses_cache_aware_verify_script():
    lines = _dry_run("validate-pr")

    verify_index = _find_line(lines, "./scripts/verify-pr.sh")

    assert all("validate_runner" not in line for line in lines[: verify_index + 1])


def test_agent_validation_targets_emit_timing_markers():
    simulated_lines = _dry_run("test-simulated-agent", SIMULATED_PARALLEL="0")
    assurance_lines = _dry_run("test-live-assurance")

    _find_line(simulated_lines, "[validate-timing] START target=$target")
    _find_line(simulated_lines, "[validate-timing] END target=$target")
    _find_line(simulated_lines, 'target="test-simulated-agent"')

    starts = _matching_indexes(assurance_lines, "[validate-timing] START target=$target")
    ends = _matching_indexes(assurance_lines, "[validate-timing] END target=$target")
    assert len(starts) == 1
    assert len(ends) == 1

    lane_index = _find_line(assurance_lines, 'target="test-live-assurance"')
    assert starts == [lane_index]
    assert all("live_codex" not in line for line in assurance_lines)


def test_core_validation_runs_live_codex_marker_serially():
    lines = _dry_run("test-integration-core", INTEGRATION_PARALLEL="0")

    starts = _matching_indexes(lines, "[validate-timing] START target=$target")
    ends = _matching_indexes(lines, "[validate-timing] END target=$target")
    assert len(starts) == 2
    assert len(ends) == 2

    core_index = _find_line(lines, 'target="test-integration-core-local"')
    live_codex_index = _find_line(lines, 'target="test-integration-core-live-codex"')
    non_live_marker_index = _find_line(
        lines,
        '-m "not requires_infra and not live_codex and not live_agent"',
    )
    live_marker_index = _find_line(
        lines, '-m "live_codex and not requires_infra and not live_agent"'
    )

    assert core_index < live_codex_index
    assert non_live_marker_index == core_index
    assert live_marker_index == live_codex_index
    assert all(
        "::test_real_interactive_codex_reviewer_round_trips_through_exchange" not in line
        for line in lines
    )


# ---------------------------------------------------------------------------
# #194 — publish validation vs live-agent assurance
# ---------------------------------------------------------------------------


class TestTheGuardrailsReadTheTrackedGraph:
    """What follows is only a proof if the dry run describes this repository.

    Every assertion below is of the form "the publish gate does/does not name
    X". The gate is invoked as ``make validate-pr-raw``, and a caller may pass
    ``PYTEST=...`` on that command line — #194's STEP A bootstrap did exactly
    that, from the engine, to segregate the live-agent probes before the marker
    semantic existed here. Make exports such an override into the environment
    of every recipe, so without isolation these tests would be reading the
    caller's selector out of their own ambient environment.
    """

    OVERRIDE = (
        ".venv/bin/pytest --deselect "
        "tests/integration/test_sandbox_os_boundary.py::test_a_live_probe"
    )

    def test_an_ambient_override_does_not_reach_the_dry_run(self, monkeypatch):
        monkeypatch.setenv("PYTEST", self.OVERRIDE)

        lines = _dry_run_closure("_validate-pr-impl")

        assert all("--deselect" not in line for line in lines)
        assert any(".venv/bin/pytest" in line for line in lines), (
            "the Makefile's own default did not take over; the dry run is "
            "describing something other than the tracked target graph"
        )

    def test_the_same_graph_is_read_where_there_is_no_gmake(self, tmp_path, monkeypatch):
        """A Linux CI runner has no ``gmake``, and must read the same graph.

        ``GMAKE := $(shell command -v gmake || command -v make)`` falls back to
        ``make`` there, and every phase recipe is then printed under that name
        instead. A sub-make pattern written against the Homebrew name expands
        the whole graph on the machine the tests were written on and nothing at
        all on the runner, so the closure silently degrades to a single dry run
        and each absence asserted against it stops proving anything.

        This stands the fallback up for real: GNU make, reachable only as
        ``make``, with every PATH entry that carries a ``gmake`` removed.
        """
        as_make = tmp_path / "make"
        as_make.symlink_to(_gnu_make())
        without_gmake = [
            entry
            for entry in os.environ["PATH"].split(os.pathsep)
            if entry and not (Path(entry) / "gmake").exists()
        ]
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), *without_gmake]))
        _gnu_make.cache_clear()
        _submake.cache_clear()
        try:
            assert shutil.which("gmake") is None
            assert _gnu_make() == str(as_make)

            lines = _dry_run_closure("_validate-pr-impl")

            _find_line(lines, 'target="test-unit"')
        finally:
            _gnu_make.cache_clear()
            _submake.cache_clear()


class TestMarkerBeatsFilename:
    """Proof 1: the ``live_agent`` marker is the only segregation mechanism."""

    def test_the_marker_is_what_blocking_integration_deselects(self):
        lines = _dry_run("test-integration-core", INTEGRATION_PARALLEL="0")

        for line in lines:
            if " -m " not in f" {line} ":
                continue
            assert "not live_agent" in line, (
                "a blocking integration selector that does not deselect "
                f"live_agent puts a model's choices in front of publication: {line}"
            )

    def test_blocking_validation_names_no_live_agent_file(self):
        """A fourth live-agent file must require no second edit anywhere."""
        lines = _dry_run_closure("_validate-pr-impl")
        live_agent_modules = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in _integration_modules_declaring("live_agent")
        ]

        assert live_agent_modules, "the marker scan found nothing; proof vacuous"
        for path in live_agent_modules:
            assert all(path not in line for line in lines), (
                f"{path} is named by the publish gate. Live-agent membership is "
                "the marker's to decide; a filename list is a second place to "
                "keep in sync, which is what #194 removed."
            )

    def test_there_is_no_live_agent_filename_variable_left(self):
        assert not re.search(
            r"^INTEGRATION_AGENT_FILES\s*:?=", _makefile_text(), re.MULTILINE
        )

    def test_no_blocking_target_ignores_a_test_file(self):
        lines = _dry_run_closure("_validate-pr-impl")

        assert all("--ignore=tests/integration" not in line for line in lines)

    def test_every_live_agent_integration_module_declares_it_at_module_scope(self):
        """A per-test marker would leave the rest of the file in the blocking lane."""
        for path in _integration_modules_declaring("live_agent"):
            source = path.read_text(encoding="utf-8")
            assert "pytestmark" in source, (
                f"{path} declares live_agent but not at module scope; part of it "
                "would still gate publication"
            )


class TestPublicationIsNotModelGated:
    """Proof 2: an unrelated candidate publishes whatever the live probe did."""

    def test_the_publish_gate_never_selects_the_live_agent_marker(self):
        lines = _dry_run_closure("_validate-pr-impl")

        for line in lines:
            assert '-m "live_agent"' not in line, line
            assert "test-live-assurance" not in line, (
                "the assurance lane is inside the publish gate again; an "
                f"INCONCLUSIVE probe would block an unrelated candidate: {line}"
            )

    def test_the_agent_phase_no_longer_runs_the_live_agent_lane(self):
        lines = _dry_run("_validate-agent-impl", VALIDATE_AGENT_JOBS="1")

        _find_line(lines, 'target="test-simulated-agent"')
        assert all("live_assurance" not in line for line in lines)

    def test_the_publish_gate_never_names_the_sandbox_probe_module(self):
        """Named, not collected — deliberately the weaker of the two claims.

        ``-m "... and not live_agent"`` is a *deselect applied after
        collection*, so the publish gate does import every module under
        ``tests/integration``, this one included. What this proves is that no
        blocking recipe reaches for the probe module by path, which is the
        filename coupling #194 removed. What runs at import time is the
        separate, stronger claim below.
        """
        lines = _dry_run_closure("_validate-pr-impl")

        assert all(
            "tests/integration/test_sandbox_os_boundary.py" not in line
            for line in lines
        )

    def test_no_integration_module_calls_a_live_provider_at_import(self):
        """The cost of the publish gate must not depend on a provider answering.

        Because the marker deselects rather than prevents collection, a
        module-scope ``is_claude_authenticated()`` runs a real ``claude -p``
        call — with a 30-second ceiling, once per xdist worker, twice over for
        the second integration invocation — every time anybody publishes, for
        tests that are then deselected. `--ignore=` used to prevent that
        import; the marker does not, so the probe has to be deferred to call
        time instead.
        """
        offenders = {
            path.relative_to(REPO_ROOT).as_posix(): sorted(called)
            for path in _integration_modules()
            if (called := _import_time_calls(path) & set(LIVE_PROVIDER_PROBES))
        }

        assert offenders == {}, (
            "these modules make a real provider round trip while being "
            f"imported, which blocking validation does on every run: {offenders}"
        )

    def test_that_rule_has_a_live_subject(self):
        """Otherwise the check above passes by naming something nobody calls."""
        assert LIVE_PROVIDER_PROBES
        callers = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in _integration_modules()
            if _any_scope_calls(path) & set(LIVE_PROVIDER_PROBES)
        ]

        assert callers, (
            "no integration module calls a registered live-provider probe at "
            "all; the import-time guardrail is checking for a name that has "
            "left the tree"
        )


# ---------------------------------------------------------------------------
# #227 — the real-Codex provider smoke is not blocking candidate publication
# ---------------------------------------------------------------------------


LIVE_CODEX_SMOKE_MODULE = (
    "tests/integration/test_persistent_review_exchange_integration.py"
)
LIVE_CODEX_SMOKE_TEST = "test_real_interactive_codex_reviewer_round_trips_through_exchange"
LIVE_CODEX_TARGET = "test-integration-core-live-codex"
RETRY_FLAGS = ("--reruns", "--only-rerun", "--last-failed", "--lf")


class TestTheRealCodexSmokeIsOutOfBlockingPublication:
    """#194 segregated the marker; the graph put it back (#227).

    ``live_codex`` was deselected by every blocking *selector* and then run
    anyway, because ``_validate-impl``'s live-web phase named
    ``test-integration-core-live-codex`` — whose selector is ``-m
    "live_codex"`` — as a target. A candidate with nothing of its own at fault
    failed publication on ``prompt_not_accepted`` after 120 s idle.

    Marker selection and target membership are two independent facts, so both
    are asserted here. Either alone is satisfiable while the real Codex CLI
    still runs in front of every candidate.
    """

    def test_no_blocking_phase_names_the_live_codex_target(self):
        """Proof 1: target membership."""
        lines = _dry_run_closure("_validate-pr-impl")

        assert all(LIVE_CODEX_TARGET not in line for line in lines), (
            "the real-Codex smoke lane is a blocking target again; a provider "
            "that never accepts the prompt would fail an unrelated candidate"
        )

    def test_no_blocking_selector_selects_the_live_codex_marker(self):
        """Proof 1: marker selection, over every pytest invocation in the gate."""
        expressions = _marker_expressions(_dry_run_closure("_validate-pr-impl"))

        assert expressions, "no marker selector in the gate; the proof is vacuous"
        for expression in expressions:
            assert not _positively_selects(expression, "live_codex"), expression
            assert not _positively_selects(expression, "live_agent"), expression

    def test_the_blocking_integration_selector_still_deselects_it(self):
        """Proof 4, and proof 2's other half: deselection is stated, not implied."""
        expressions = _marker_expressions(
            _dry_run("test-integration-core-local", INTEGRATION_PARALLEL="0")
        )

        assert expressions
        for expression in expressions:
            assert "not live_codex" in expression, expression
            assert "not live_agent" in expression, expression

    def test_the_named_smoke_is_the_marked_test(self):
        """Otherwise the deselection above is about nothing in particular."""
        marked = _tests_declaring(REPO_ROOT / LIVE_CODEX_SMOKE_MODULE, "live_codex")

        assert LIVE_CODEX_SMOKE_TEST in marked, (
            f"{LIVE_CODEX_SMOKE_TEST} no longer declares live_codex, so nothing "
            "connects the blocking selectors' deselection to the test #227 is "
            "about"
        )

    def test_the_readiness_probe_is_registered_and_deferred_to_call_time(self):
        """Proof 5: deselection is not enough — the gate still imports this.

        ``live_codex`` deselects after collection, exactly like ``live_agent``,
        and the module holding the smoke also holds deterministic tests the
        gate does run. So every blocking validation imports it, once per xdist
        worker. A module-scope readiness probe — a ``skipif`` condition is one,
        the decorator expression runs on import — would therefore be a real
        ``codex login status`` spawn inside the publish gate, for the one test
        that gate is about to throw away.

        Import-time probing is already forbidden by
        ``test_no_integration_module_calls_a_live_provider_at_import``, but
        that rule can only see probes named in ``LIVE_PROVIDER_PROBES``. A
        module-local helper is invisible to it and reads green. What this pins
        is the registration, which is what gives the general rule its subject.
        """
        module = REPO_ROOT / LIVE_CODEX_SMOKE_MODULE
        probes = set(LIVE_PROVIDER_PROBES)

        assert _any_scope_calls(module) & probes, (
            "the real-Codex smoke's readiness check is not a registered "
            "live-provider probe, so the import-time guardrail reads past it; "
            f"register it in tests/fixtures/live_agent_cli.py ({probes})"
        )
        assert not (_import_time_calls(module) & probes), (
            "the readiness probe runs while this module is imported, which "
            "blocking validation does on every run — and at collection time, "
            "before tests/codex_home.py's isolation fixtures, so against the "
            "operator's own ~/.codex"
        )

    def test_the_lane_cannot_report_success_without_running_the_smoke(self):
        """Proof 5's other half: an absent CLI fails, it does not skip.

        This lane is the only place the coverage lives now, and the Makefile
        documents it as provider-compliance evidence someone reads. A
        ``skipif`` would let ``make test-integration-core-live-codex`` exit 0
        having run nothing, so the evidence could not tell *ran and passed*
        from *never ran*. Readiness reports through ``require_probe_ran``
        instead, from the fixture that also defers the probe — one edit site
        for both halves.
        """
        module = REPO_ROOT / LIVE_CODEX_SMOKE_MODULE
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        module_markers = _module_scope_markers(tree)
        smoke = next(
            node
            for name, node in collected_tests(tree)
            if name == LIVE_CODEX_SMOKE_TEST
        )

        assert not ({"skip", "skipif"} & _markers_on(smoke, module_markers)), (
            "the real-Codex smoke skips instead of failing when codex is "
            "unavailable, so its lane exits 0 having proven nothing"
        )

        fixtures = _fixture_functions(tree)
        requested = {argument.arg for argument in smoke.args.args}
        gates = [
            fixtures[name]
            for name in sorted(requested & fixtures.keys())
            if _calls_in(fixtures[name]) & set(LIVE_PROVIDER_PROBES)
        ]

        assert gates, (
            "the smoke requests no fixture that probes codex readiness, so "
            "nothing checks the prerequisite at call time"
        )
        assert any("require_probe_ran" in _calls_in(gate) for gate in gates), (
            "codex readiness is enforced by something other than "
            "require_probe_ran; an unusable provider must surface as a loud "
            "failure naming the missing prerequisite"
        )

    def test_the_publish_gate_names_neither_the_module_nor_the_nodeid(self):
        """Proof 2: no blocking recipe reaches for it by path either."""
        lines = _dry_run_closure("_validate-pr-impl")

        assert all(LIVE_CODEX_SMOKE_MODULE not in line for line in lines)
        assert all(LIVE_CODEX_SMOKE_TEST not in line for line in lines)

    def test_the_explicit_lane_still_collects_it(self):
        """Proof 3: retained as its own runnable regression lane."""
        lines = _dry_run(LIVE_CODEX_TARGET)
        pytest_line = lines[_find_line(lines, "tests/integration")]

        assert '-m "live_codex and not requires_infra and not live_agent"' in pytest_line
        assert "--ignore" not in pytest_line
        assert "--deselect" not in pytest_line

        module = REPO_ROOT / LIVE_CODEX_SMOKE_MODULE
        excluded = {"requires_infra", "live_agent"}
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        module_markers = _module_scope_markers(tree)
        smoke = next(
            node
            for name, node in collected_tests(tree)
            if name == LIVE_CODEX_SMOKE_TEST
        )

        assert not (_markers_on(smoke, module_markers) & excluded), (
            "the smoke test acquired a marker the lane's own selector "
            "deselects, so the lane it was retained for no longer runs it"
        )

    def test_the_lane_declares_no_retry_semantics(self):
        """Proof 7: availability handling is not smuggled in as re-runs."""
        lane_lines = _dry_run(LIVE_CODEX_TARGET)
        gate_lines = _dry_run_closure("_validate-pr-impl")

        for flag in RETRY_FLAGS:
            assert all(flag not in line for line in lane_lines), flag
            assert all(flag not in line for line in gate_lines), flag

    def test_the_lane_files_nothing_a_gate_could_read(self):
        """Proof 8: no evidence identity to cross over in the first place.

        The assurance lane files a record and is refused a `publish_gate`
        suite by `LiveAssuranceRecord`. This lane has no record at all — it
        loads no lane plugin, resolves no artifact root, and writes into
        neither evidence directory — so there is nothing for a reader to
        mistake for publication authority.
        """
        lines = _dry_run(LIVE_CODEX_TARGET)

        for forbidden in (
            "--live-assurance-root",
            "tests.live_assurance_lane",
            ".issue-orchestrator/live-assurance",
            ".issue-orchestrator/validation",
        ):
            assert all(forbidden not in line for line in lines), forbidden

    def test_the_deterministic_phases_are_all_still_required(self):
        """Proof 6: nothing else left the gate along with the smoke lane."""
        lines = _dry_run_closure("_validate-pr-impl")

        for target in (
            "test-unit",
            "test-simulated-core",
            "test-integration-core-local",
            "test-web",
            "test-simulated-agent",
        ):
            _find_line(lines, f'target="{target}"')
        for phase in ("validate-static-phase", "validate-core-tests-phase"):
            _find_line(lines, phase)

        # test-vscode hangs off validate-pr-raw itself rather than the phased
        # graph, and that recipe line carries no `;` for the closure to follow.
        _find_line(_dry_run("validate-pr-raw"), "test-vscode")


class TestTheAssuranceLane:
    """Proofs 3 and 8, at the lane's command surface."""

    def test_it_selects_by_marker_and_files_an_exact_artifact_record(self):
        lines = _dry_run("test-live-assurance")
        pytest_line = lines[_find_line(lines, "--live-assurance-root")]

        assert '-m "live_agent"' in pytest_line
        assert "-p tests.live_assurance_lane" in pytest_line

    def test_the_artifact_identity_is_not_resolved_in_the_recipe(self):
        """One root, one artifact — a second source could name another tree.

        A SHA computed by the recipe comes from ``make``'s cwd while the record
        is written under ``LIVE_ASSURANCE_ROOT``, so the two are free to
        describe different checkouts and nothing downstream can tell. The lane
        resolves both from the root instead.
        """
        lines = _dry_run("test-live-assurance")
        pytest_line = lines[_find_line(lines, "--live-assurance-root")]

        assert "--live-assurance-head-sha" not in pytest_line
        assert "rev-parse" not in pytest_line

    def test_it_runs_serially_and_does_not_stop_at_the_first_probe(self):
        """``-x`` would let one breach hide behind an earlier probe's failure."""
        lines = _dry_run("test-live-assurance")
        pytest_line = lines[_find_line(lines, "--live-assurance-root")]

        assert " -x " not in f" {pytest_line} "
        assert " -n " not in f" {pytest_line} "

    def test_it_declares_no_retry_semantics(self):
        """Proof 8: re-run policy for a candidate's gate is not reintroduced."""
        lines = _dry_run("test-live-assurance")
        pytest_line = lines[_find_line(lines, "--live-assurance-root")]

        for flag in ("--reruns", "--only-rerun", "--last-failed", "--lf"):
            assert flag not in pytest_line, (
                f"{flag} turns the assurance lane into a retry policy: {pytest_line}"
            )


class TestEveryLiveAgentTestReachesAProvider:
    """Proof 7: the marker takes nothing out of blocking validation for free.

    ``pytest.mark.live_agent`` in a ``pytestmark`` list is module scope, so
    marking a module removes *every* test in it from every blocking gate — and
    the assurance lane that collects them files a record rather than failing a
    candidate, so what leaves blocking validation lands in no gate at all.
    Whether that is correct depends on each test, one at a time: the marker's
    own criterion is spawning a real provider CLI.

    Stated as a rule rather than as a list of the cases someone thought of.
    ``TestDeterministicSandboxCoverageStaysInBlockingValidation`` below is the
    list, and it is kept — as the witness that the two named extractions are
    still where they were put — but it could only ever prove that about the
    modules it names. ``TestShellEscaping`` and the ``agent-done`` cases left
    blocking validation past it without a word.
    """

    def test_no_live_agent_module_hides_a_deterministic_case(self):
        offenders = {
            path.relative_to(REPO_ROOT).as_posix(): tests
            for path in _integration_modules_declaring("live_agent")
            if (tests := missing_provider_reach_in(path))
        }

        assert offenders == {}, (
            "these tests are in a live_agent module but show no sign of "
            "reaching a provider, so the marker has taken them out of every "
            "blocking gate without putting them in one that can fail: "
            f"{offenders}. Move them to a non-live_agent module (see "
            "tests/integration/test_agent_invocation_surface.py), or — if they "
            "do reach a provider by a route nothing registers — add that route "
            "to tests/live_agent_reach.py."
        )

    def test_that_rule_can_actually_fail(self):
        """Otherwise it is the vacuous check it was written to replace.

        A synthetic module rather than a planted broken file: the rule has to
        be provably able to report an offender without the tree having to
        contain one.
        """
        deterministic = '''
import subprocess

class TestQuoting:
    def test_escaping_round_trips(self):
        """Claude and Codex invocations are built with this."""
        wrapped = "bash -c " + "echo hi".replace("'", "'\\\\''")
        assert subprocess.run(["bash", "-c", wrapped]).returncode == 0
'''
        assert missing_provider_reach(deterministic, filename="<synthetic>") == (
            "TestQuoting::test_escaping_round_trips",
        )

    def test_a_live_probe_is_recognised_through_a_module_helper(self):
        """Reach may be one call away; the rule must not demand it be inline."""
        indirect = '''
def _spawn(prompt):
    return run(["claude", "--print", prompt])

def test_the_model_answers():
    assert "OK" in _spawn("Reply OK").stdout
'''
        assert missing_provider_reach(indirect, filename="<synthetic>") == ()

    def test_the_registry_and_the_collector_have_live_subjects(self):
        """Both halves of the rule must be about something that exists."""
        assert PROVIDER_REACH_NAMES

        modules = _integration_modules_declaring("live_agent")
        assert modules, "no live_agent module; the rule has no subject"

        collected = [
            name
            for path in modules
            for name, _node in collected_tests(
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            )
        ]
        assert collected, (
            "the live_agent modules collect no tests, so the rule above passes "
            "by having nothing to check"
        )


class TestDeterministicCoverageStaysInBlockingValidation:
    """Proof 7, continued: the extractions are still where they were put.

    A case list, and kept only as one: it witnesses that these specific
    deterministic cases survived their split, which the rule above cannot —
    a case deleted outright reaches no provider and is in no live-agent
    module, so nothing structural misses it. The rule above is what notices a
    module nobody named here.
    """

    DETERMINISTIC_CASES = (
        (
            "tests/integration/test_agent_invocation_surface.py",
            (
                "test_single_quote_escaping",
                "test_complex_quoting_pattern",
                "test_nested_quotes_in_prompt",
                "test_agent_done_wrapper_resolves_correctly",
                "test_completion_json_written_to_worktree_not_main_repo",
                "test_completion_json_written_to_wrong_place_without_cd",
            ),
        ),
        (
            "tests/unit/test_sandbox_stream_events.py",
            (
                "test_tool_events_supports_system_permission_denial_message",
                "test_native_writes_to_selects_only_write_tools_naming_the_target",
                "test_is_permission_denial_distinguishes_denial_from_arbitrary_error",
                "test_codex_command_events_extracts_completed_command_executions",
            ),
        ),
        (
            "tests/unit/test_sandbox_provider_adapter.py",
            ("test_generated_deny_rule_count_is_bounded",),
        ),
    )

    @pytest.mark.parametrize("module,cases", DETERMINISTIC_CASES)
    def test_the_case_is_still_there(self, module: str, cases: tuple[str, ...]):
        source = (REPO_ROOT / module).read_text(encoding="utf-8")

        for case in cases:
            assert f"def {case}" in source, (
                f"{module}::{case} was removed. It does not depend on a model "
                "choosing anything and must stay in blocking validation."
            )

    @pytest.mark.parametrize("module,cases", DETERMINISTIC_CASES)
    def test_the_module_holding_it_is_not_live_agent(
        self, module: str, cases: tuple[str, ...]
    ):
        assert not _has_marker(REPO_ROOT / module, "live_agent"), (
            f"{module} became live-agent, which would deselect "
            f"{cases} from blocking validation"
        )

    def test_the_lanes_that_run_them_are_in_the_publish_gate(self):
        """Both of them: the extracted cases are split across two suites.

        Naming a module ``tests/unit/...`` or ``tests/integration/...`` does
        nothing on its own — what puts it in front of publication is a target
        that collects it, inside the expanded ``_validate-pr-impl`` graph.
        """
        lines = _dry_run_closure("_validate-pr-impl")

        _find_line(lines, 'target="test-unit"')
        _find_line(lines, 'target="test-integration-core-local"')


def test_live_agent_transport_is_scheduled_by_e2e_not_integration_assurance():
    assurance_lines = _dry_run("test-live-assurance")
    e2e_lines = _dry_run("test-e2e")

    assert all(
        "tests/e2e" not in line
        for line in assurance_lines
        if "live-assurance" in line or "pytest" in line
    )
    # The e2e lane must actually collect the transport test: pin that the
    # pytest invocation targets the whole tests/e2e dir with no --ignore and
    # no -m deselection.
    e2e_pytest_line = e2e_lines[_find_line(e2e_lines, "tests/e2e")]
    assert "--ignore" not in e2e_pytest_line
    assert " -m " not in f" {e2e_pytest_line} "
