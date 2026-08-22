"""Tests for Makefile validation phase orchestration."""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _gnu_make() -> str:
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
            "VALIDATE_WEB_JOBS": "1",
            "VALIDATE_LIVE_WEB_JOBS": "2",
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


_SUBMAKE = re.compile(r"gmake\s+((?:(?:-\S+|--\S+)\s+)*)([\w.\-]+(?:\s+[\w.\-]+)*)\s*;")


def _dry_run_closure(target: str, **overrides: str) -> list[str]:
    """Every command the phased target graph would run, phases expanded.

    ``make -n`` on a phase target prints the nested ``gmake`` invocations
    without expanding them, so asserting an absence against that output is
    vacuous — which is exactly how a live-agent lane could creep back into the
    publish gate unnoticed. This follows each sub-make and returns the union.
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
            for _flags, names in _SUBMAKE.findall(line):
                for nested in names.split():
                    walk(nested)

    walk(target)
    return collected


def _integration_modules_declaring(marker: str) -> list[Path]:
    return sorted(
        path
        for path in (REPO_ROOT / "tests" / "integration").rglob("test_*.py")
        if _has_marker(path, marker)
    )


def test_validate_impl_runs_core_phases_with_separate_job_caps():
    lines = _dry_run("_validate-impl")

    static_index = _find_line(lines, "validate-static-phase", "_validate-static-impl")
    core_tests_index = _find_line(
        lines,
        "validate-core-tests-phase",
        "_validate-core-tests-impl",
    )
    live_web_index = _find_line(
        lines,
        "validate-live-web-phase",
        "test-integration-core-live-codex",
        "test-web",
    )

    _assert_job_count(lines[static_index], 10)
    _assert_job_count(lines[core_tests_index], 1)
    _assert_job_count(lines[live_web_index], 2)

    assert static_index < core_tests_index < live_web_index


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

    core_index = _find_line(lines, 'target="test-integration-core"')
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

    def test_the_sandbox_probes_are_not_collected_by_the_publish_gate(self):
        lines = _dry_run_closure("_validate-pr-impl")

        assert all(
            "tests/integration/test_sandbox_os_boundary.py" not in line
            for line in lines
        )


class TestTheAssuranceLane:
    """Proofs 3 and 8, at the lane's command surface."""

    def test_it_selects_by_marker_and_files_an_exact_artifact_record(self):
        lines = _dry_run("test-live-assurance")
        pytest_line = lines[_find_line(lines, "--live-assurance-root")]

        assert '-m "live_agent"' in pytest_line
        assert "-p tests.live_assurance_lane" in pytest_line
        assert "--live-assurance-head-sha=$(git rev-parse HEAD)" in pytest_line

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


class TestDeterministicSandboxCoverageStaysInBlockingValidation:
    """Proof 7: the non-model-dependent assertions did not leave with the module."""

    DETERMINISTIC_CASES = (
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

    def test_the_unit_lane_that_runs_them_is_in_the_publish_gate(self):
        lines = _dry_run_closure("_validate-pr-impl")

        _find_line(lines, 'target="test-unit"')


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
