"""The live-assurance lane reduces a real pytest run to one of three outcomes.

Covers required failure-direction proofs 3 (a genuine breach is
``SECURITY_FAIL``, not ``INCONCLUSIVE``), 4 (a probe that never ran is
``INCONCLUSIVE``; neither can be recorded as ``PASS``) and 8 (nothing here
re-runs anything).

The end-to-end cases drive a **real** pytest session over generated probe
modules rather than hand-built report objects, because the thing under test is
precisely the wiring between "a probe raised through this channel" and "the
record says this". Each generated directory also contains an *unmarked*
module, so the same run proves the lane's marker selection: a file with no
``live_agent`` marker is never collected, and a file that has it is, with no
filename list anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.json_live_assurance_store import LIVE_ASSURANCE_DIR
from issue_orchestrator.domain.live_assurance import (
    LiveAssuranceOutcome,
    LiveAssuranceRecord,
)
from tests.live_assurance_lane import LiveAssuranceLane

REPO_ROOT = Path(__file__).resolve().parents[2]

_UNMARKED_MODULE = '''
def test_not_a_live_agent_test() -> None:
    raise AssertionError("this module must never be collected by the lane")
'''

_PROBE_HEADER = '''
import pytest

from tests.live_assurance import assert_no_breach, require_probe_ran

pytestmark = [pytest.mark.integration, pytest.mark.live_agent]
'''

_PASSING_PROBE = '''
def test_boundary_holds() -> None:
    require_probe_ran(True, "probe ran")
    assert_no_breach(True, "SANDBOX BREACH: nothing escaped")
'''

_BREACHING_PROBE = '''
def test_deny_rule_was_reverted() -> None:
    require_probe_ran(True, "probe ran")
    assert_no_breach(False, "SANDBOX BREACH: a denied secret was read by Bash.")
'''

_MULTILINE_BREACHING_PROBE = '''
def test_a_denied_secret_was_read() -> None:
    require_probe_ran(True, "probe ran")
    assert_no_breach(
        False,
        "SANDBOX BREACH: a denied secret was read by Bash.\\n"
        "denied path: /etc/secret-token\\n"
        "captured provider output: SECRET_VALUE=hunter2",
    )
'''

_NEVER_RAN_PROBE = '''
def test_model_never_issued_the_operation() -> None:
    require_probe_ran(False, "secret-read probe did not run")
'''

_SKIPPED_PROBE = '''
import pytest


def test_provider_unavailable() -> None:
    pytest.skip("claude CLI not installed")
'''

_UNREADY_PROVIDER_PROBE = '''
@pytest.fixture(autouse=True)
def _require_authenticated_provider() -> None:
    require_probe_ran(False, "the provider CLI is not authenticated on this host")


def test_the_chain_runs() -> None:
    assert_no_breach(True, "SANDBOX BREACH: nothing escaped")
'''
"""The readiness-gate shape a live-agent module actually lands.

``tests/integration/test_live_agent_chain.py`` reports an unusable provider
from an autouse fixture, because the probe is a real ``claude -p`` round trip
and must not run while blocking validation imports the module. That puts the
failure in the **setup** phase, which is a different path through the collector
than the call-phase failures every other probe here exercises.
"""


def _subprocess_env() -> dict[str, str]:
    """Environment in which this checkout's source is the one that runs.

    The nested run's rootdir is the generated probe directory, so this
    repository's ``pythonpath`` ini setting does not apply and a bare
    ``issue_orchestrator`` import would resolve against whatever editable
    install the environment happens to carry — another checkout entirely.
    Naming ``src`` explicitly is what makes the record under test the one this
    candidate's code wrote.
    """
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_checkout(tmp_path: Path) -> tuple[Path, str]:
    """A real one-commit checkout, and the SHA the lane must resolve from it.

    The lane reads its artifact identity out of ``--live-assurance-root``
    rather than being handed a SHA, so the harness has to supply a genuine
    checkout. A synthetic constant here would be testing an option that no
    longer exists.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--initial-branch=main")
    _git(checkout, "config", "user.email", "lane@example.invalid")
    _git(checkout, "config", "user.name", "Lane Harness")
    _git(checkout, "commit", "--quiet", "--allow-empty", "-m", "artifact")
    return checkout, _git(checkout, "rev-parse", "HEAD")


def _run_lane_in(
    checkout: Path, tmp_path: Path, *probe_sources: str
) -> subprocess.CompletedProcess[str]:
    """Run the lane over generated probes, filing evidence about ``checkout``.

    The probes live outside the checkout so that generating them does not make
    the tree dirty — that state is a subject of these tests, not a side effect
    of the harness.
    """
    probes = tmp_path / "probes"
    probes.mkdir()
    (probes / "test_not_live_agent.py").write_text(_UNMARKED_MODULE, encoding="utf-8")
    for index, source in enumerate(probe_sources):
        (probes / f"test_probe_{index}.py").write_text(
            _PROBE_HEADER + source, encoding="utf-8"
        )

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probes),
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.live_assurance_lane",
            "-m",
            "live_agent",
            f"--live-assurance-root={checkout}",
        ],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def _filed_record(checkout: Path, head_sha: str) -> LiveAssuranceRecord | None:
    record_path = checkout / LIVE_ASSURANCE_DIR / f"{head_sha}.json"
    if not record_path.exists():
        return None
    return LiveAssuranceRecord.from_payload(
        json.loads(record_path.read_text(encoding="utf-8"))
    )


def _run_lane(tmp_path: Path, *probe_sources: str) -> LiveAssuranceRecord | None:
    """Run the lane over generated probes and return the record it filed."""
    checkout, head_sha = _make_checkout(tmp_path)
    _run_lane_in(checkout, tmp_path, *probe_sources)
    return _filed_record(checkout, head_sha)


class TestTheLaneRecordsWhatItObserved:
    def test_a_run_whose_probes_all_held_is_a_pass(self, tmp_path: Path) -> None:
        record = _run_lane(tmp_path, _PASSING_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.PASS

    def test_a_reverted_deny_rule_is_security_fail_not_inconclusive(
        self, tmp_path: Path
    ) -> None:
        """Proof 3: a boundary that was exercised and did not hold."""
        record = _run_lane(tmp_path, _BREACHING_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.SECURITY_FAIL
        assert "SANDBOX BREACH" in record.detail

    def test_a_breachs_evidence_survives_into_the_record(
        self, tmp_path: Path
    ) -> None:
        """The refused promotion is read long after the probe output is gone.

        A breach assertion embeds the denied path and the provider output it
        captured, across several lines. Reducing that to a first line would
        leave an operator with the headline and none of the proof.
        """
        record = _run_lane(tmp_path, _MULTILINE_BREACHING_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.SECURITY_FAIL
        assert "/etc/secret-token" in record.detail
        assert "SECRET_VALUE=hunter2" in record.detail

    def test_an_operation_the_model_never_issued_is_inconclusive(
        self, tmp_path: Path
    ) -> None:
        """Proof 4: #109's shape is neither a failure nor a pass."""
        record = _run_lane(tmp_path, _NEVER_RAN_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.INCONCLUSIVE
        assert "probe did not run" in record.detail

    def test_an_unavailable_provider_is_inconclusive(self, tmp_path: Path) -> None:
        record = _run_lane(tmp_path, _SKIPPED_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.INCONCLUSIVE

    def test_a_readiness_gate_refused_in_setup_is_inconclusive(
        self, tmp_path: Path
    ) -> None:
        """The live-agent modules' own shape: the gate is an autouse fixture.

        Its ``require_probe_ran`` fails during **setup**, so the call phase this
        collector counts executions in never happens. A lane that only watched
        call-phase reports would see a run with nothing wrong in it and file a
        ``PASS`` for a boundary no provider was available to exercise.
        """
        record = _run_lane(tmp_path, _UNREADY_PROVIDER_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.INCONCLUSIVE
        assert "not authenticated" in record.detail

    def test_a_breach_beside_a_probe_that_never_ran_is_security_fail(
        self, tmp_path: Path
    ) -> None:
        record = _run_lane(tmp_path, _BREACHING_PROBE, _NEVER_RAN_PROBE)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.SECURITY_FAIL

    def test_an_empty_selection_is_inconclusive_never_a_vacuous_pass(
        self, tmp_path: Path
    ) -> None:
        record = _run_lane(tmp_path)

        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.INCONCLUSIVE
        assert "no live-agent probe was selected" in record.detail


class TestTheLaneAccumulator:
    """The reduction itself, without a pytest session in the way."""

    def test_an_empty_lane_is_inconclusive(self) -> None:
        assert LiveAssuranceLane().outcome is LiveAssuranceOutcome.INCONCLUSIVE

    def test_executed_probes_with_nothing_wrong_are_a_pass(self) -> None:
        lane = LiveAssuranceLane()
        lane.record_executed()

        assert lane.outcome is LiveAssuranceOutcome.PASS

    def test_an_incomplete_observation_outranks_a_pass(self) -> None:
        lane = LiveAssuranceLane()
        lane.record_executed()
        lane.record_incomplete("probe::b", "did not run")

        assert lane.outcome is LiveAssuranceOutcome.INCONCLUSIVE

    def test_a_breach_outranks_an_incomplete_observation(self) -> None:
        lane = LiveAssuranceLane()
        lane.record_incomplete("probe::b", "did not run")
        lane.record_breach("probe::a", "SANDBOX BREACH")

        assert lane.outcome is LiveAssuranceOutcome.SECURITY_FAIL

    def test_every_observation_is_preserved_in_the_detail(self) -> None:
        """An INCONCLUSIVE whose reason was dropped reinterprets the failure."""
        lane = LiveAssuranceLane()
        lane.record_breach("probe::a", "secret read")
        lane.record_incomplete("probe::b", "did not run")

        assert "SECURITY_FAIL: probe::a: secret read" in lane.detail
        assert "INCONCLUSIVE: probe::b: did not run" in lane.detail

    def test_the_lane_offers_no_retry_surface(self) -> None:
        """Proof 8: nothing here re-runs a probe or a candidate's validation."""
        surface = {name for name in dir(LiveAssuranceLane) if not name.startswith("_")}

        assert not {
            name
            for name in surface
            if "retry" in name or "attempt" in name or "rerun" in name
        }


class TestTheProbeModuleUsesTheClassificationChannels:
    """Proof 3's structural half: a breach cannot come out of a bare assert.

    ``assert cond, "SANDBOX BREACH: ..."`` raises a plain ``AssertionError``,
    which the lane classifies ``INCONCLUSIVE`` — a proven boundary violation
    filed as a provider hiccup, and a promotion refusal that reads as "re-run
    the lane". The condition and the message are unchanged; only the door is.
    """

    PROBE_MODULE = REPO_ROOT / "tests" / "integration" / "test_sandbox_os_boundary.py"

    def _bare_assert_messages(self) -> list[str]:
        import ast

        tree = ast.parse(self.PROBE_MODULE.read_text(encoding="utf-8"))
        messages: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert) or node.msg is None:
                continue
            messages.append(ast.dump(node.msg))
        return messages

    def test_no_bare_assert_carries_a_breach_message(self) -> None:
        offenders = [
            message
            for message in self._bare_assert_messages()
            if "SANDBOX BREACH" in message
        ]

        assert offenders == [], (
            "a SANDBOX BREACH assertion bypasses assert_no_breach and would be "
            f"recorded as INCONCLUSIVE: {offenders}"
        )

    def test_the_module_actually_uses_both_channels(self) -> None:
        """Otherwise the check above passes because nothing asserts at all."""
        source = self.PROBE_MODULE.read_text(encoding="utf-8")

        assert source.count("assert_no_breach(") >= 10
        assert source.count("require_probe_ran(") >= 5


class TestTheRecordIsAboutTheCheckoutItIsFiledUnder:
    """The writer half of exact-artifact: one root decides both.

    The lane used to be handed ``--live-assurance-head-sha`` beside a
    separately overridable root, so the SHA and the tree the record described
    could be two different checkouts with nothing able to notice. Identity now
    comes from the root itself.
    """

    def test_the_record_names_the_checkouts_own_head(self, tmp_path: Path) -> None:
        checkout, head_sha = _make_checkout(tmp_path)

        _run_lane_in(checkout, tmp_path, _PASSING_PROBE)

        record = _filed_record(checkout, head_sha)
        assert record is not None
        assert record.head_sha == head_sha
        assert record.working_tree_dirty is False
        assert record.assures(head_sha) is True

    def test_a_run_over_uncommitted_changes_assures_nothing(
        self, tmp_path: Path
    ) -> None:
        """The probes exercised a tree this commit does not name."""
        checkout, head_sha = _make_checkout(tmp_path)
        (checkout / "sandbox_edit.py").write_text("# in progress\n", encoding="utf-8")

        _run_lane_in(checkout, tmp_path, _PASSING_PROBE)

        record = _filed_record(checkout, head_sha)
        assert record is not None
        assert record.outcome is LiveAssuranceOutcome.PASS
        assert record.working_tree_dirty is True
        assert record.assures(head_sha) is False

    def test_a_root_that_is_not_a_checkout_is_a_usage_error(
        self, tmp_path: Path
    ) -> None:
        """A record with no artifact identity would assure an unknown build."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path),
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "tests.live_assurance_lane",
                f"--live-assurance-root={tmp_path}",
            ],
            cwd=REPO_ROOT,
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "not a checkout at a commit" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "outcome", [LiveAssuranceOutcome.SECURITY_FAIL, LiveAssuranceOutcome.INCONCLUSIVE]
)
def test_a_non_pass_lane_run_never_files_a_pass(
    tmp_path: Path, outcome: LiveAssuranceOutcome
) -> None:
    """Proof 4's closing half, stated against the record the gate reads."""
    source = (
        _BREACHING_PROBE
        if outcome is LiveAssuranceOutcome.SECURITY_FAIL
        else _NEVER_RAN_PROBE
    )
    record = _run_lane(tmp_path, source)

    assert record is not None
    assert record.assures(record.head_sha) is False
