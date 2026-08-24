"""What the installed Codex actually answers about the shipped rules (#252).

The D16 defect was never a logic error the unit suite could have caught on its
own: the verifier's assumption about ``codex execpolicy check`` output was
simply not what the CLI returns. A command that matches no rule is answered
``{"matchedRules": []}`` with no ``decision`` at all, and the verifier read
that absence as a block — so ``git push origin main``, which the shipped rules
deliberately list as ``not_match``, failed the hook gate and stopped a Pilot
launch before any agent session existed.

So this module measures the provider rather than a description of it:

* the safe negative sample really does come back in the no-match shape, and the
  gate really does pass against the installed CLI;
* the dangerous sample really does come back explicitly ``forbidden``, and
  neutralizing that one rule really does fail the gate;
* a nonzero exit and a ``prompt`` decision really do reach the caller as
  unclassifiable, from the real CLI rather than from a fake.

``live_codex`` keeps it out of every blocking gate — it needs the operator's
Codex install, and a CLI upgrade must not fail an unrelated candidate — while
``live_agent`` puts it in ``make test-live-assurance``, which is where the
exact-artifact promotion proof this fix is a prerequisite for goes looking for
live Codex hook verification. It needs no model and no authentication: an
execpolicy check is local rule evaluation.
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
    ExecPolicyResultError,
)
from issue_orchestrator.adapters.hooks.codex_execpolicy import (
    DECISION_KEY,
    MATCHED_RULES_KEY,
)

from tests.codex_execpolicy_samples import (
    DANGEROUS_COMMAND,
    NO_MATCH_PAYLOAD,
    PROMPT_RULES,
    SAFE_COMMAND,
)
from tests.fixtures.live_agent_cli import is_codex_available
from tests.live_assurance import require_probe_ran

from .conftest import xdist_timeout

RULES_FILENAME = "orchestrator.rules"


@pytest.fixture(autouse=True)
def require_codex_cli() -> None:
    """Report an absent CLI, rather than skipping past it.

    A missing install leaves the provider contract exactly as unmeasured as a
    check that never ran, which is what ``INCONCLUSIVE`` means in the assurance
    lane. ``is_codex_available`` is a PATH lookup and contacts nothing, and it
    is called here rather than at module scope because blocking validation
    imports this file even though it deselects it.
    """
    require_probe_ran(
        is_codex_available(),
        "Codex CLI not found, so the execpolicy verification never ran: this "
        "proof measures the installed provider. Install Codex "
        "(brew install --cask codex) and re-run.",
    )


@pytest.fixture
def installed_project(tmp_path: Path) -> Path:
    """A project carrying the rules the orchestrator actually ships."""
    CodexAdapter().install_hooks(tmp_path)
    return tmp_path


def rules_of(project: Path) -> Path:
    return project / ".codex" / "rules" / RULES_FILENAME


def raw_check(rules_file: Path, command: tuple[str, ...]) -> dict[str, object]:
    """The CLI's own JSON, so the recorded shapes can be compared to reality."""
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


class TestShippedRulesAgainstTheInstalledCli:
    """The measurement the fix rests on."""

    def test_safe_push_is_answered_with_the_no_match_shape(
        self, installed_project: Path
    ) -> None:
        """The recorded no-match payload is still what Codex returns."""
        payload = raw_check(rules_of(installed_project), SAFE_COMMAND)

        assert payload == json.loads(NO_MATCH_PAYLOAD)
        assert DECISION_KEY not in payload
        assert payload[MATCHED_RULES_KEY] == []

    def test_safe_push_classifies_as_no_match_not_forbidden(
        self, installed_project: Path
    ) -> None:
        outcome = CodexCliExecPolicy().check(rules_of(installed_project), SAFE_COMMAND)

        assert outcome is ExecPolicyOutcome.NO_MATCH

    def test_dangerous_push_is_explicitly_forbidden(
        self, installed_project: Path
    ) -> None:
        """Acceptance 5: the rule that matters still denies, by decision."""
        payload = raw_check(rules_of(installed_project), DANGEROUS_COMMAND)

        assert payload[DECISION_KEY] == "forbidden"
        assert payload[MATCHED_RULES_KEY]
        outcome = CodexCliExecPolicy().check(
            rules_of(installed_project), DANGEROUS_COMMAND
        )
        assert outcome is ExecPolicyOutcome.FORBIDDEN

    def test_hook_gate_passes_against_the_installed_cli(
        self, installed_project: Path
    ) -> None:
        """The end of the live defect: verification of a real install succeeds."""
        result = CodexAdapter().verify_hooks(installed_project)

        assert result.success, result.checks_failed
        assert "execpolicy_allows:git push origin main" in result.checks_passed
        assert "execpolicy_blocks:git push --no-verify" in result.checks_passed

    def test_neutralizing_the_forbidden_rule_fails_the_gate(
        self, installed_project: Path
    ) -> None:
        """Acceptance 5's failure direction, measured rather than asserted.

        Only the dangerous rule's decision is flipped, so the rules file still
        satisfies every textual check — what fails is the live classification.
        """
        rules_file = rules_of(installed_project)
        shipped = rules_file.read_text(encoding="utf-8")
        neutralized = shipped.replace(
            'pattern = ["git", "push", "--no-verify"],\n    decision = "forbidden"',
            'pattern = ["git", "push", "--no-verify"],\n    decision = "allow"',
            1,
        )
        assert neutralized != shipped, (
            "the shipped rules no longer spell the forbidden push rule the way "
            "this proof neutralizes it; re-target the edit rather than deleting it"
        )
        rules_file.write_text(neutralized, encoding="utf-8")

        result = CodexAdapter().verify_hooks(installed_project)

        assert not result.success
        assert "execpolicy_should_block:git push --no-verify" in result.checks_failed, (
            result.checks_failed
        )


class TestUnclassifiableAnswersFromTheRealCli:
    """Fail-closed directions, proven against the provider rather than a fake."""

    def test_prompt_decision_is_not_treated_as_no_match(self, tmp_path: Path) -> None:
        """Acceptance 3: Codex can emit ``prompt``; this repository refuses it."""
        rules_file = tmp_path / RULES_FILENAME
        rules_file.write_text(PROMPT_RULES, encoding="utf-8")

        payload = raw_check(rules_file, SAFE_COMMAND)
        assert payload[DECISION_KEY] == "prompt"

        with pytest.raises(ExecPolicyResultError, match="prompt"):
            CodexCliExecPolicy().check(rules_file, SAFE_COMMAND)

    def test_nonzero_cli_exit_is_not_a_no_match(self, tmp_path: Path) -> None:
        """Acceptance 4: an unreadable policy is not an absent policy."""
        missing = tmp_path / "does-not-exist.rules"

        with pytest.raises(ExecPolicyResultError):
            CodexCliExecPolicy().check(missing, SAFE_COMMAND)
