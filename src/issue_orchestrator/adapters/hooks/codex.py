"""Codex hook adapter."""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ...infra.hooks._types import (
    AiAgentAdapter,
    AiAgentType,
    HookInstallationLayout,
    ManagedHookArtifact,
    TEMPLATES_DIR,
    VerificationResult,
)
from .codex_execpolicy import (
    CodexCliExecPolicy,
    ExecPolicyChecker,
    ExecPolicyOutcome,
    ExecPolicyResultError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExecPolicySample:
    """A command whose classification by the installed policy is pinned.

    ``expect_forbidden`` is the whole assertion: the dangerous sample must come
    back explicitly forbidden, and the safe sample must come back *not*
    forbidden -- which the shipped rules satisfy by matching no rule at all
    (#252). Anything the policy cannot classify raises before it reaches here,
    so an unreadable answer never satisfies either expectation.
    """

    command: tuple[str, ...]
    expect_forbidden: bool

    @property
    def label(self) -> str:
        return " ".join(self.command)

    @property
    def passed_check(self) -> str:
        """What was measured, not what it was mistaken for.

        The safe sample passes by coming back *not blocked* -- which the
        shipped rules satisfy by matching no rule at all. Reporting that as
        ``execpolicy_allows`` would say "allow" about a no-match, which is the
        very conflation #252 removed, so the passing check is named
        ``execpolicy_not_blocked`` and reads against ``execpolicy_wrongly_blocks``.
        """
        verb = "blocks" if self.expect_forbidden else "not_blocked"
        return f"execpolicy_{verb}:{self.label}"

    @property
    def failed_check(self) -> str:
        verb = "should_block" if self.expect_forbidden else "wrongly_blocks"
        return f"execpolicy_{verb}:{self.label}"


_EXECPOLICY_SAMPLES = (
    _ExecPolicySample(command=("git", "push", "--no-verify"), expect_forbidden=True),
    _ExecPolicySample(
        command=("git", "push", "origin", "main"), expect_forbidden=False
    ),
)
"""The dangerous positive and the safe negative the hook gate verifies."""


class CodexAdapter(AiAgentAdapter):
    """Adapter for OpenAI Codex CLI.

    Codex CLI uses Starlark rules files in .codex/rules/ within the project.
    Project-scoped rules override user-global defaults.
    Rules use prefix_rule() with decision="forbidden" to block commands.
    """

    def __init__(self, execpolicy: ExecPolicyChecker | None = None) -> None:
        """Take the policy that answers ``execpolicy`` questions.

        Defaults to the installed Codex CLI, which is the only authority on
        its own rules; tests inject a checker rather than reaching into the
        adapter.
        """
        self._execpolicy: ExecPolicyChecker = (
            CodexCliExecPolicy() if execpolicy is None else execpolicy
        )

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CODEX

    def _get_rules_dir(self, project_root: Path) -> Path:
        """Get the Codex rules directory for a project."""
        return project_root / ".codex" / "rules"

    def _copy_rules_file(
        self, src: Path, target: Path, files_created: list[Path]
    ) -> None:
        """Copy a rules file."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def installation_layout(self, project_root: Path) -> HookInstallationLayout:
        return HookInstallationLayout(
            managed_files=(
                ManagedHookArtifact(
                    path=self._get_rules_dir(project_root) / "orchestrator.rules",
                    template_path=TEMPLATES_DIR / "codex" / "orchestrator.rules",
                ),
            )
        )

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Codex CLI rules.

        Installs rules into the project's .codex/rules/ directory.
        """
        files_created: list[Path] = []
        for artifact in self._managed_files(project_root):
            if artifact.template_path is None:
                continue
            artifact.path.parent.mkdir(parents=True, exist_ok=True)
            self._copy_rules_file(artifact.template_path, artifact.path, files_created)

        return files_created

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Codex CLI rules are installed.

        Checks project-scoped rules file and, if Codex is available,
        runs execpolicy checks to validate enforcement.
        """
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"

        if not rules_file.exists():
            checks_failed.append("rules_file_exists: orchestrator.rules not found")
            return VerificationResult(
                False, self.agent_type, checks_passed, checks_failed
            )
        checks_passed.append("rules_file_exists")

        # Verify rules file contains our blocking rules
        content = rules_file.read_text()
        required_patterns = [
            'pattern = ["git", "push", "--no-verify"]',
            'decision = "forbidden"',
            'pattern = ["gh", "pr", "merge"]',
        ]

        for pattern in required_patterns:
            if pattern in content:
                checks_passed.append(f"rule_contains:{pattern[:30]}")
            else:
                checks_failed.append(f"rule_missing:{pattern[:30]}")

        codex_bin = shutil.which("codex")
        if not codex_bin:
            checks_failed.append("execpolicy_cli_available: codex not available")
            return VerificationResult(
                False, self.agent_type, checks_passed, checks_failed
            )

        sample_passed, sample_failed = self._check_execpolicy_samples(rules_file)
        checks_passed.extend(sample_passed)
        checks_failed.extend(sample_failed)

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check if Codex CLI rules are installed."""
        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"
        return rules_file.exists()

    def _check_execpolicy_samples(
        self, rules_file: Path
    ) -> tuple[list[str], list[str]]:
        """Classify every sample, and report the checks that passed and failed.

        Each sample is asked and judged independently, so one unanswerable
        check cannot hide the verdict on the other. Any failure to classify --
        an unrecognized decision such as ``prompt``, malformed output, a CLI
        that exited nonzero, a timeout, an absent binary -- is a verification
        failure, never a silent pass.

        Only ``ExecPolicyResultError`` is caught, because that is the failure
        channel :class:`ExecPolicyChecker` declares and the whole channel: a
        checker that raises anything else has a bug, and a bug reported as a
        truncated policy-failure string is the same guess-at-an-unreadable-answer
        this module exists to remove. It is left to crash where it happened.
        """
        passed: list[str] = []
        failed: list[str] = []
        for sample in _EXECPOLICY_SAMPLES:
            try:
                outcome = self._execpolicy.check(rules_file, sample.command)
            except ExecPolicyResultError as exc:
                failed.append(f"execpolicy_check_failed:{sample.label}:{str(exc)[:40]}")
                continue
            is_forbidden = outcome is ExecPolicyOutcome.FORBIDDEN
            if is_forbidden == sample.expect_forbidden:
                passed.append(sample.passed_check)
            else:
                failed.append(sample.failed_check)
        return passed, failed


__all__ = ["CodexAdapter"]
