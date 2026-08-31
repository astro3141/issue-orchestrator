"""The launch-scoped command guard a planning principal launches behind (#289).

A ``planning_investigation`` Tech Lead prepares a bounded issue. It reads
source and governing evidence; it does not validate a code candidate, and the
repository's publication gate is not a planning verdict — R22 Pilot 4 spent
seventeen minutes inside one its sandbox could never satisfy and returned
BLOCKED without the bounded ``create_issue`` it was launched to produce. The
prompt already said not to. ``docs/architecture/hooks.md`` settles what to do
about an instruction that must hold: prompts are suggestions, barriers are
enforcement.

This port is the barrier's *decision surface*, not its mechanism. It exists so
the ADR-0031 launch owner — the only place that knows both the flavor and the
provider that will execute it — can ask for a guard and be told, as data,
whether one was actually established:

* :class:`PlanningCommandGuard` is the answer. ``enforced`` is a fact the
  caller must branch on, in the shape ``ReviewCommandGuardOutcome`` already
  established: a policy file having been written is not evidence that anything
  refuses a command, and a provider that never reads the file must not be
  reported as guarded.
* :class:`PlanningCommandGuardError` is raised when a guard the installer
  *can* establish could not be written or could not be verified. The caller's
  planning launch fails closed on it rather than spawning unguarded.

The policy itself is not declared here. The entry points a planning principal
is refused come from the one gate-command vocabulary in
:mod:`issue_orchestrator.infra.hooks.gate_commands`, which the reviewer guard
reads too, and the evidence shape both principals report — :class:`GuardProbe`,
re-exported here for the callers that already read it from this module — lives
in :mod:`.command_guard` for the same reason (#396).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.artifact_contracts import AgentProvider
from .command_guard import GuardProbe

__all__ = [
    "GUARDABLE_PLANNING_PROVIDERS",
    "GuardProbe",
    "PlanningCommandGuard",
    "PlanningCommandGuardError",
    "PlanningCommandGuardInstaller",
    "UNGUARDED_PLANNING_COMMAND_GUARD",
]

#: Providers a launch-scoped planning guard can actually be registered with.
#:
#: It lives in the port because two layers must agree on it and must not
#: disagree: the installer decides whether to write anything, and the ADR-0031
#: launch owner decides whether an unenforced guard is a failed launch or a
#: logged limitation. Two copies of that set is how one provider ends up
#: written-for and not required, or required and unwritable.
#:
#: Codex only. Widening it means implementing that provider's registration and
#: measuring that a running session loads it, not editing this line.
GUARDABLE_PLANNING_PROVIDERS: frozenset[str] = frozenset({"codex"})


class PlanningCommandGuardError(RuntimeError):
    """A guard that should have been established was not.

    Raised — rather than returned as an unguarded outcome — when the installer
    recognises the provider and still cannot produce a verified barrier. The
    two are different facts: "this provider has no guard mechanism" is a
    limitation the caller can log and decide about, while "this provider's
    guard did not take" is a launch that must not happen.
    """


@dataclass(frozen=True)
class PlanningCommandGuard:
    """What a planning launch actually got, for one worktree.

    ``policy_file`` is ``None`` when nothing was written. ``probes`` is empty
    in the same case; when a guard was established it holds the pinned
    refusals and allowances the installer verified against the enforcing
    mechanism before the session was allowed to start.
    """

    provider: AgentProvider
    policy_file: Path | None = None
    probes: tuple[GuardProbe, ...] = ()

    @property
    def enforced(self) -> bool:
        """True only when a policy file was written *and* verified refusing."""
        return self.policy_file is not None and any(
            probe.refused for probe in self.probes
        )

    def refusals(self) -> tuple[str, ...]:
        return tuple(probe.label for probe in self.probes if probe.refused)

    def allowances(self) -> tuple[str, ...]:
        return tuple(probe.label for probe in self.probes if not probe.refused)


class PlanningCommandGuardInstaller(Protocol):
    """Establishes, and verifies, a launch-scoped planning command guard."""

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> PlanningCommandGuard:
        """Register the gate-command refusal for one planning launch.

        ``provider`` is the provider that will actually execute the principal
        in ``worktree_path``, as the launch resolves it. It is required, not
        defaulted: a default is what lets a guard be claimed for a provider
        that never reads it.

        Returns an outcome whose ``enforced`` is ``False``, having written
        nothing, for a provider this installer cannot register with.

        Raises:
            PlanningCommandGuardError: the guard could be registered for this
                provider and was not — write failure, or a policy that did not
                verify as refusing.
        """
        ...


class _UnguardedInstaller:
    """Establishes nothing, and says so.

    The explicit "no guard mechanism is wired here" composition, for entry
    points that build a launcher without the adapter. It is not a fallback:
    the ADR-0031 owner fails a Codex planning launch closed on an unenforced
    guard, so wiring this one keeps that failure loud instead of silently
    launching an unguarded planning session.
    """

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> PlanningCommandGuard:
        return PlanningCommandGuard(provider=provider)


UNGUARDED_PLANNING_COMMAND_GUARD: PlanningCommandGuardInstaller = _UnguardedInstaller()
