"""Test doubles for the reviewer command guard port (#396).

The production installer verifies its policy by asking ``codex execpolicy
check``, so a suite that drives a whole review exchange cannot reach it unless
the provider CLI is installed — which it is not in CI, and need not be for the
facts those suites are about. These doubles keep the guard *bound* and
observable there:

* :class:`RecordingReviewCommandGuard` establishes a guard and records what it
  was asked for. It is what a test uses when the subject is "was the guard
  bound to this reviewer worktree, for the provider the exchange launches".
* :class:`FailingReviewCommandGuard` refuses to establish one. It is what a
  test uses when the subject is "does reviewer worktree creation fail closed".

Neither writes a file or spawns a CLI. Tests whose subject is what the policy
*actually refuses* use ``install_review_command_guard`` /
``CodexReviewCommandGuardInstaller`` with a fake ``ExecPolicyChecker``, or the
live Codex integration module.

The same split, and the same two shapes, as ``planning_command_guard_fakes``
keeps for the other guarded principal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from issue_orchestrator.adapters.worktree.api import REVIEW_GUARD_RULES
from issue_orchestrator.domain.artifact_contracts import AgentProvider
from issue_orchestrator.ports.review_command_guard import (
    GuardProbe,
    ReviewCommandGuardError,
    ReviewCommandGuardOutcome,
)

__all__ = [
    "FailingReviewCommandGuard",
    "RecordingReviewCommandGuard",
]


@dataclass
class RecordingReviewCommandGuard:
    """Establishes a verified-looking guard and remembers every request."""

    calls: list[tuple[Path, str]] = field(default_factory=list)
    guard: bool = True

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> ReviewCommandGuardOutcome:
        self.calls.append((Path(worktree_path), provider.value))
        if not self.guard:
            return ReviewCommandGuardOutcome(provider=provider, policy_file=None)
        return ReviewCommandGuardOutcome(
            provider=provider,
            policy_file=Path(worktree_path) / REVIEW_GUARD_RULES,
            probes=(
                GuardProbe(command=("make", "validate-pr-raw"), refused=True),
                GuardProbe(command=("git", "log"), refused=False),
            ),
        )

    def providers(self) -> tuple[str, ...]:
        """Every provider a guard was requested for, in request order."""
        return tuple(provider for _, provider in self.calls)


@dataclass
class FailingReviewCommandGuard:
    """Raises the way the real installer does when a guard does not take."""

    message: str = "policy did not verify as refusing"
    calls: list[tuple[Path, str]] = field(default_factory=list)

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> ReviewCommandGuardOutcome:
        self.calls.append((Path(worktree_path), provider.value))
        raise ReviewCommandGuardError(self.message)
