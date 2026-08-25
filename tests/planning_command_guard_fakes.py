"""Test doubles for the launch-scoped planning command guard port (#289).

Two doubles, because the two facts a test can want are different:

* :class:`RecordingPlanningCommandGuard` establishes a guard and records what
  it was asked for. It is what a test uses when the subject is "was the guard
  bound to this launch, for this provider".
* :class:`FailingPlanningCommandGuard` refuses to establish one. It is what a
  test uses when the subject is "does the launch fail closed".

Neither writes a file or spawns a CLI. Tests that need the *real* policy to be
rendered and classified use ``CodexPlanningCommandGuardInstaller`` with a fake
``ExecPolicyChecker``, or the live Codex integration module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from issue_orchestrator.domain.artifact_contracts import AgentProvider
from issue_orchestrator.ports.planning_command_guard import (
    GuardProbe,
    PlanningCommandGuard,
    PlanningCommandGuardError,
)

__all__ = [
    "FailingPlanningCommandGuard",
    "RecordingPlanningCommandGuard",
]


@dataclass
class RecordingPlanningCommandGuard:
    """Establishes a verified-looking guard and remembers every request."""

    calls: list[tuple[Path, str]] = field(default_factory=list)
    enforce: bool = True

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> PlanningCommandGuard:
        self.calls.append((Path(worktree_path), provider.value))
        if not self.enforce:
            return PlanningCommandGuard(provider=provider)
        return PlanningCommandGuard(
            provider=provider,
            policy_file=Path(worktree_path) / ".codex/rules/planning-gate.rules",
            probes=(
                GuardProbe(command=("make", "validate-pr-raw"), refused=True),
                GuardProbe(command=("git", "log"), refused=False),
            ),
        )


@dataclass
class FailingPlanningCommandGuard:
    """Raises the way the real installer does when a guard does not take."""

    reason: str = "execpolicy did not refuse the pinned gate command"
    calls: list[tuple[Path, str]] = field(default_factory=list)

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> PlanningCommandGuard:
        self.calls.append((Path(worktree_path), provider.value))
        raise PlanningCommandGuardError(self.reason)
