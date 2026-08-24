"""The Codex hook gate must tell "no rule matched" from "a rule said no" (#252).

The shipped rules deliberately list ``git push origin main`` as ``not_match``
for the forbidden ``git push --no-verify`` rule, and Codex answers a command no
rule matches with ``{"matchedRules": []}`` and no ``decision`` at all. The
verifier read that absence as "not allowed", so the safe negative sample came
back as ``execpolicy_wrongly_blocks`` and a Pilot launch failed its hook gate
before any agent session existed.

The property proven here is ``NO_MATCH != FORBIDDEN`` — and only that. The
converse, "anything other than forbidden is allowed", is what would make the
gate useless, so every direction that could smuggle it in is pinned too:
``prompt``, malformed output, a decision with no matched rule, matched rules
with no decision, and a nonzero CLI exit are all verification failures, and the
dangerous sample must still come back explicitly forbidden.

The payloads come from ``tests/codex_execpolicy_samples.py``, recorded from
``codex execpolicy check`` 0.147.0 against the shipped ``orchestrator.rules``;
``tests/integration/test_codex_execpolicy_live.py`` asks the installed CLI the
same questions, so this file cannot quietly drift from the provider it claims
to describe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from issue_orchestrator.adapters.hooks import (
    CodexAdapter,
    CodexCliExecPolicy,
    ExecPolicyOutcome,
    ExecPolicyResultError,
    classify_execpolicy_result,
)

from tests.codex_execpolicy_samples import (
    ALLOW_PAYLOAD,
    DANGEROUS_COMMAND,
    FORBIDDEN_PAYLOAD,
    NO_MATCH_PAYLOAD,
    PROMPT_PAYLOAD,
    SAFE_COMMAND,
)


class RecordedExecPolicy:
    """A policy that replays recorded CLI payloads through the real classifier.

    The fake stops at the process boundary: what a command's stdout means is
    still decided by production code, so these tests cannot pass by agreeing
    with themselves about interpretation.
    """

    def __init__(self, payloads: dict[tuple[str, ...], str]) -> None:
        self._payloads = payloads
        self.asked: list[tuple[str, ...]] = []

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        key = tuple(command)
        self.asked.append(key)
        if key not in self._payloads:
            raise AssertionError(f"verifier asked an unexpected command: {key}")
        return classify_execpolicy_result(self._payloads[key])


class RefusingExecPolicy:
    """A policy that cannot answer — a broken CLI, a timeout, an exit code."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        raise self._error


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with the shipped rules installed, and Codex on PATH."""
    return tmp_path


@pytest.fixture(autouse=True)
def codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI-availability check is a separate concern from classification."""
    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/local/bin/codex")


def verify_with(payloads: dict[tuple[str, ...], str], project: Path):
    adapter = CodexAdapter(execpolicy=RecordedExecPolicy(payloads))
    adapter.install_hooks(project)
    return adapter.verify_hooks(project)


class TestClassification:
    """What a single execpolicy result means."""

    def test_no_matching_rule_is_not_a_verdict(self) -> None:
        """The documented no-match shape: no rule matched, so nothing denied."""
        assert classify_execpolicy_result(NO_MATCH_PAYLOAD) is (
            ExecPolicyOutcome.NO_MATCH
        )

    def test_explicit_forbidden_is_forbidden(self) -> None:
        assert classify_execpolicy_result(FORBIDDEN_PAYLOAD) is (
            ExecPolicyOutcome.FORBIDDEN
        )

    def test_explicit_allow_is_allowed(self) -> None:
        assert classify_execpolicy_result(ALLOW_PAYLOAD) is ExecPolicyOutcome.ALLOWED

    def test_prompt_is_not_classifiable(self) -> None:
        """``prompt`` is a question, and the CLI has flags that skip questions."""
        with pytest.raises(ExecPolicyResultError, match="prompt"):
            classify_execpolicy_result(PROMPT_PAYLOAD)

    def test_unrecognized_decision_is_not_classifiable(self) -> None:
        payload = json.dumps({"matchedRules": [{"x": 1}], "decision": "escalate"})
        with pytest.raises(ExecPolicyResultError, match="escalate"):
            classify_execpolicy_result(payload)

    def test_matched_rules_without_a_decision_is_not_no_match(self) -> None:
        """A rule matched, so the absent decision is missing data, not silence."""
        payload = json.dumps({"matchedRules": [{"prefixRuleMatch": {}}]})
        with pytest.raises(ExecPolicyResultError, match="without a decision"):
            classify_execpolicy_result(payload)

    def test_decision_without_a_matched_rule_is_inconsistent(self) -> None:
        payload = json.dumps({"matchedRules": [], "decision": "allow"})
        with pytest.raises(ExecPolicyResultError, match="no matched rule"):
            classify_execpolicy_result(payload)

    def test_missing_matched_rules_key_is_not_no_match(self) -> None:
        """An empty object is not the documented no-match shape."""
        with pytest.raises(ExecPolicyResultError, match="matchedRules"):
            classify_execpolicy_result(json.dumps({}))

    def test_matched_rules_of_the_wrong_type_is_rejected(self) -> None:
        payload = json.dumps({"matchedRules": None, "decision": "forbidden"})
        with pytest.raises(ExecPolicyResultError, match="matchedRules"):
            classify_execpolicy_result(payload)

    def test_non_string_decision_is_rejected(self) -> None:
        payload = json.dumps({"matchedRules": [{"x": 1}], "decision": False})
        with pytest.raises(ExecPolicyResultError, match="not a string"):
            classify_execpolicy_result(payload)

    def test_non_object_result_is_rejected(self) -> None:
        with pytest.raises(ExecPolicyResultError, match="not a JSON object"):
            classify_execpolicy_result(json.dumps(["forbidden"]))

    def test_unparseable_output_is_rejected(self) -> None:
        with pytest.raises(ExecPolicyResultError, match="unparseable"):
            classify_execpolicy_result("not json at all")

    def test_empty_output_is_rejected(self) -> None:
        with pytest.raises(ExecPolicyResultError, match="unparseable"):
            classify_execpolicy_result("")


class TestHookVerification:
    """What the hook gate concludes from those results."""

    def test_no_match_on_the_safe_command_passes_verification(
        self, project: Path
    ) -> None:
        """Acceptance 1: the shape that used to fail every Codex launch."""
        result = verify_with(
            {
                DANGEROUS_COMMAND: FORBIDDEN_PAYLOAD,
                SAFE_COMMAND: NO_MATCH_PAYLOAD,
            },
            project,
        )

        assert result.success, result.checks_failed
        assert "execpolicy_allows:git push origin main" in result.checks_passed
        assert "execpolicy_blocks:git push --no-verify" in result.checks_passed

    def test_explicit_allow_on_the_safe_command_also_passes(
        self, project: Path
    ) -> None:
        """A future rule that deliberately permits the push is equally fine."""
        result = verify_with(
            {DANGEROUS_COMMAND: FORBIDDEN_PAYLOAD, SAFE_COMMAND: ALLOW_PAYLOAD},
            project,
        )

        assert result.success, result.checks_failed

    def test_forbidding_the_safe_command_still_fails_verification(
        self, project: Path
    ) -> None:
        """Acceptance 2: a real block is still reported, under its own name."""
        result = verify_with(
            {DANGEROUS_COMMAND: FORBIDDEN_PAYLOAD, SAFE_COMMAND: FORBIDDEN_PAYLOAD},
            project,
        )

        assert not result.success
        assert "execpolicy_wrongly_blocks:git push origin main" in result.checks_failed

    def test_prompt_on_the_safe_command_does_not_pass(self, project: Path) -> None:
        """Acceptance 3: ``prompt`` is neither no-match nor allow."""
        result = verify_with(
            {DANGEROUS_COMMAND: FORBIDDEN_PAYLOAD, SAFE_COMMAND: PROMPT_PAYLOAD},
            project,
        )

        assert not result.success
        assert not any(
            check.startswith("execpolicy_allows") for check in result.checks_passed
        )
        assert any(
            check.startswith("execpolicy_check_failed:git push origin main")
            for check in result.checks_failed
        )

    def test_malformed_output_fails_closed(self, project: Path) -> None:
        """Acceptance 4: unreadable is not permissive."""
        result = verify_with(
            {DANGEROUS_COMMAND: FORBIDDEN_PAYLOAD, SAFE_COMMAND: "{ oops"},
            project,
        )

        assert not result.success
        assert not any(
            check.startswith("execpolicy_allows") for check in result.checks_passed
        )

    def test_a_policy_that_cannot_answer_fails_closed(self, project: Path) -> None:
        """A nonzero exit, a timeout, a missing binary: all the same verdict."""
        adapter = CodexAdapter(
            execpolicy=RefusingExecPolicy(
                subprocess.TimeoutExpired(cmd="codex", timeout=120)
            )
        )
        adapter.install_hooks(project)

        result = adapter.verify_hooks(project)

        assert not result.success
        assert not any(
            check.startswith("execpolicy_") for check in result.checks_passed
        )
        assert (
            len(
                [
                    check
                    for check in result.checks_failed
                    if check.startswith("execpolicy_check_failed:")
                ]
            )
            == 2
        )

    def test_one_unanswerable_check_does_not_hide_the_other(
        self, project: Path
    ) -> None:
        """The dangerous sample is judged even when the safe sample errors."""
        result = verify_with(
            {DANGEROUS_COMMAND: "{ oops", SAFE_COMMAND: NO_MATCH_PAYLOAD},
            project,
        )

        assert not result.success
        assert "execpolicy_allows:git push origin main" in result.checks_passed

    def test_neutralizing_the_dangerous_rule_fails_verification(
        self, project: Path
    ) -> None:
        """Acceptance 5: no-match for the dangerous command is a gate failure.

        This is the direction the fix must not weaken — the same shape that now
        passes for the safe command must still fail here.
        """
        result = verify_with(
            {DANGEROUS_COMMAND: NO_MATCH_PAYLOAD, SAFE_COMMAND: NO_MATCH_PAYLOAD},
            project,
        )

        assert not result.success
        assert "execpolicy_should_block:git push --no-verify" in result.checks_failed

    def test_allowing_the_dangerous_command_fails_verification(
        self, project: Path
    ) -> None:
        result = verify_with(
            {DANGEROUS_COMMAND: ALLOW_PAYLOAD, SAFE_COMMAND: NO_MATCH_PAYLOAD},
            project,
        )

        assert not result.success
        assert "execpolicy_should_block:git push --no-verify" in result.checks_failed

    def test_both_samples_are_always_asked(self, project: Path) -> None:
        """Neither sample may be short-circuited by the other's answer."""
        policy = RecordedExecPolicy(
            {DANGEROUS_COMMAND: "{ oops", SAFE_COMMAND: NO_MATCH_PAYLOAD}
        )
        adapter = CodexAdapter(execpolicy=policy)
        adapter.install_hooks(project)

        adapter.verify_hooks(project)

        assert policy.asked == [DANGEROUS_COMMAND, SAFE_COMMAND]


class TestCodexCliExecPolicy:
    """The process boundary: what the CLI's exit code means."""

    def test_nonzero_exit_raises_rather_than_classifying(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Acceptance 4: a failed check is never a permissive answer."""

        def _failed_run(*args: object, **kwargs: object):
            return subprocess.CompletedProcess(
                args=["codex"], returncode=1, stdout="", stderr="failed to read policy"
            )

        monkeypatch.setattr(subprocess, "run", _failed_run)

        with pytest.raises(ExecPolicyResultError, match="failed to read policy"):
            CodexCliExecPolicy().check(tmp_path / "orchestrator.rules", SAFE_COMMAND)

    def test_nonzero_exit_without_stderr_still_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _silent_failure(*args: object, **kwargs: object):
            return subprocess.CompletedProcess(
                args=["codex"], returncode=2, stdout="", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", _silent_failure)

        with pytest.raises(ExecPolicyResultError, match="exited 2"):
            CodexCliExecPolicy().check(tmp_path / "orchestrator.rules", SAFE_COMMAND)

    def test_successful_exit_is_classified_from_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        recorded: dict[str, object] = {}

        def _ok_run(argv: list[str], **kwargs: object):
            recorded["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=NO_MATCH_PAYLOAD, stderr=""
            )

        monkeypatch.setattr(subprocess, "run", _ok_run)
        rules_file = tmp_path / "orchestrator.rules"

        outcome = CodexCliExecPolicy().check(rules_file, SAFE_COMMAND)

        assert outcome is ExecPolicyOutcome.NO_MATCH
        assert recorded["argv"] == [
            "codex",
            "execpolicy",
            "check",
            "--rules",
            str(rules_file),
            "--pretty",
            "--",
            *SAFE_COMMAND,
        ]
