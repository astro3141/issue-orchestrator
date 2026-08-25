"""What the installed Codex answers about the planning guard IO writes (#289).

The unit suite proves the installer writes the shared gate vocabulary and
refuses to report a guard it did not verify. What it cannot prove is that
*Codex* agrees: `prefix_rule` semantics, argv matching and the no-match shape
belong to the provider, and a policy that reads correctly to a Python
reimplementation is worth nothing if the CLI classifies it differently. That is
the same class of defect as #252, where the verifier's assumption about
``codex execpolicy check`` output was simply not what the CLI returns.

So this module puts the *actually generated* policy to the installed CLI and
measures four things #289 requires:

* the pinned gate commands — ``make validate-pr-raw`` and a pytest-shaped
  command — really are answered ``forbidden``, by decision and by a matched
  rule, not by an absence;
* reading the code — ``git log``, ``rg``, ``cat`` — really is answered with the
  no-match shape, so the guard is not a refuse-everything policy;
* the shipped safety rules the installer places beside it still deny
  ``git push --no-verify`` and still permit ``git push origin main``, so the
  scoped planning policy composes with them rather than replacing them;
* the mutation directions fail: a planning policy whose ``make`` rule is
  neutralized stops refusing the gate command, and a policy rendered from an
  emptied vocabulary stops refusing it too — which is what makes the shared
  classifier link load-bearing rather than decorative.

**What this module is not.** It is not the live-assurance proof that a *running*
Codex session loads a linked worktree's ``.codex/rules`` and refuses before
process creation. Per #21 §8.1, per-candidate validation must not depend on a
live model emitting a particular tool call, so that proof belongs to the
post-merge exact-artifact assurance run. An ``execpolicy check`` is local rule
evaluation: no model, no authentication, no network.

The provider behaviour this leaf rests on WAS measured, on codex-cli 0.147.0,
before the mechanism was chosen — a rules file in a linked worktree of a
trusted repository was loaded and the command was rejected at
``CreateProcess``, with both the safety file and the planning file enforcing in
the same session. See ``adapters/worktree/_planning_command_guard`` for the
record of that measurement.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    # Spawns the real Codex CLI: the marker, not a filename list, is what keeps
    # it out of blocking candidate validation (#194).
    pytest.mark.live_agent,
    pytest.mark.live_codex,
    pytest.mark.xdist_group("codex"),
]

from issue_orchestrator.adapters.hooks import (
    CodexAdapter,
    CodexCliExecPolicy,
    ExecPolicyOutcome,
)
from issue_orchestrator.adapters.hooks.codex_execpolicy import (
    DECISION_KEY,
    MATCHED_RULES_KEY,
)
from issue_orchestrator.adapters.worktree.api import (
    PLANNING_GUARD_RULES,
    render_planning_rules,
)

from tests.fixtures.live_agent_cli import is_codex_available
from tests.live_assurance import require_probe_ran

from .conftest import xdist_timeout

#: The acceptance direction #289 names, in the argv dialect Codex evaluates.
PINNED_GATE_COMMANDS = (
    ("make", "validate-pr-raw"),
    ("pytest", "-q", "tests/unit"),
    ("python", "-m", "pytest"),
    ("uv", "run", "pytest"),
)

#: Bounded read-only source inspection the planning principal must keep.
PINNED_INSPECTION_COMMANDS = (
    ("git", "log", "--oneline", "-20"),
    ("git", "status"),
    ("rg", "-n", "planning_investigation", "src"),
    ("cat", "AGENTS.md"),
    ("ls", "-la", "src"),
    # #289 acceptance 10: enforcement must not turn planning into a no-tools
    # role. The bounded create_issue flow #261 gave it runs through this.
    ("coding-done", "completed", "--implementation", "prepared the leaf"),
)

SAFETY_DENIED = ("git", "push", "--no-verify")
SAFETY_PERMITTED = ("git", "push", "origin", "main")


@pytest.fixture(autouse=True)
def require_codex_cli() -> None:
    """Report an absent CLI, rather than skipping past it.

    A missing install leaves the provider contract exactly as unmeasured as a
    check that never ran, which is what ``INCONCLUSIVE`` means in the assurance
    lane.
    """
    require_probe_ran(
        is_codex_available(),
        "Codex CLI not found, so the planning guard was never put to the "
        "provider: this proof measures the installed provider. Install Codex "
        "(brew install --cask codex) and re-run.",
    )


@pytest.fixture
def planning_rules(tmp_path: Path) -> Path:
    """The policy file exactly as a planning launch would write it."""
    rules_file = tmp_path / PLANNING_GUARD_RULES
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    rules_file.write_text(render_planning_rules(), encoding="utf-8")
    return rules_file


def raw_check(rules_file: Path, command: tuple[str, ...]) -> dict[str, object]:
    """The CLI's own JSON, so the classification can be read rather than inferred."""
    result = subprocess.run(
        [
            "codex",
            "execpolicy",
            "check",
            "--rules",
            str(rules_file),
            "--pretty",
            "--",
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=xdist_timeout(60),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestTheGeneratedPolicyRefusesTheGate:
    @pytest.mark.parametrize("command", PINNED_GATE_COMMANDS)
    def test_a_gate_command_is_refused_by_decision_not_by_absence(
        self, planning_rules: Path, command: tuple[str, ...]
    ) -> None:
        payload = raw_check(planning_rules, command)

        assert payload[DECISION_KEY] == "forbidden"
        assert payload[MATCHED_RULES_KEY], "a decision must name the rule that made it"
        assert (
            CodexCliExecPolicy().check(planning_rules, command)
            is ExecPolicyOutcome.FORBIDDEN
        )

    def test_the_refusal_tells_the_principal_what_it_is_for(
        self, planning_rules: Path
    ) -> None:
        payload = raw_check(planning_rules, ("make", "validate-pr-raw"))

        rendered = json.dumps(payload)
        assert "prepares a bounded issue" in rendered
        assert "create_issue" in rendered


class TestReadingTheCodeStaysPossible:
    @pytest.mark.parametrize("command", PINNED_INSPECTION_COMMANDS)
    def test_source_inspection_is_answered_with_the_no_match_shape(
        self, planning_rules: Path, command: tuple[str, ...]
    ) -> None:
        payload = raw_check(planning_rules, command)

        assert payload[MATCHED_RULES_KEY] == []
        assert DECISION_KEY not in payload
        assert (
            CodexCliExecPolicy().check(planning_rules, command)
            is ExecPolicyOutcome.NO_MATCH
        )


class TestTheScopedPolicyComposesWithShippedSafety:
    def test_the_safety_rules_beside_it_still_deny_the_bypass(
        self, tmp_path: Path
    ) -> None:
        """#289 acceptance 7: scoped enforcement composes, it does not replace."""
        CodexAdapter().install_hooks(tmp_path)
        safety = tmp_path / ".codex" / "rules" / "orchestrator.rules"

        assert raw_check(safety, SAFETY_DENIED)[DECISION_KEY] == "forbidden"
        assert raw_check(safety, SAFETY_PERMITTED)[MATCHED_RULES_KEY] == []

    def test_the_planning_policy_does_not_shadow_the_safety_policy(
        self, planning_rules: Path, tmp_path: Path
    ) -> None:
        """Each file answers for its own rules; neither silences the other."""
        CodexAdapter().install_hooks(tmp_path)
        safety = tmp_path / ".codex" / "rules" / "orchestrator.rules"
        assert safety.parent == planning_rules.parent, (
            "both policies must live in the one directory Codex loads"
        )

        # The planning file does not claim the safety rule's verdict...
        assert raw_check(planning_rules, SAFETY_DENIED)[MATCHED_RULES_KEY] == []
        # ...and the safety file does not claim the planning rule's.
        assert raw_check(safety, ("make", "validate-pr-raw"))[MATCHED_RULES_KEY] == []


class TestTheMutationDirectionsFail:
    def test_neutralizing_the_gate_rule_lets_the_gate_command_through(
        self, planning_rules: Path
    ) -> None:
        """Remove the scoped registration's teeth and the refusal disappears.

        Measured rather than asserted: only the ``make`` rule's decision is
        flipped, so the file still parses and still contains the planning
        prose. What changes is the live classification.
        """
        rendered = planning_rules.read_text(encoding="utf-8")
        neutralized = rendered.replace(
            'pattern = ["make"],\n    decision = "forbidden",',
            'pattern = ["make"],\n    decision = "allow",',
        )
        assert neutralized != rendered
        planning_rules.write_text(neutralized, encoding="utf-8")

        payload = raw_check(planning_rules, ("make", "validate-pr-raw"))

        assert payload[DECISION_KEY] != "forbidden"

    def test_breaking_the_shared_classifier_link_breaks_the_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Emptying the one vocabulary must stop the planning policy refusing.

        This is what makes "one classifier" a measured property rather than a
        claim: if the planning policy still refused ``make validate-pr-raw``
        with the vocabulary gone, it would be reading a second list.
        """
        monkeypatch.setattr(
            "issue_orchestrator.infra.hooks.gate_commands.GATE_COMMANDS", ()
        )
        rules_file = tmp_path / "empty-vocabulary.rules"
        rules_file.write_text(render_planning_rules(), encoding="utf-8")

        payload = raw_check(rules_file, ("make", "validate-pr-raw"))

        assert payload[MATCHED_RULES_KEY] == []
