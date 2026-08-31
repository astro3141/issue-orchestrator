"""The exchange-scoped command guard a review-exchange reviewer sits behind (#396).

The reviewer worktree is deliberately created without the repository's runtime
prerequisites (``execution/reviewer_worktree``,
``docs/architecture/validation.md``). A gate command run there measures the
missing prerequisite rather than the candidate, so the reviewer is refused one
— and ``docs/architecture/hooks.md`` rules that the refusal has to be a
barrier, not a line in a prompt.

This port is that barrier's *decision surface*, not its mechanism, in the shape
:mod:`.planning_command_guard` already established for the other guarded
principal (#289):

* :class:`ReviewCommandGuardOutcome` is the answer. ``guarded`` is a fact the
  caller must branch on: a policy file having been written is not evidence that
  anything refuses a command, and a provider that never reads the file must not
  be reported as guarded.
* :class:`ReviewCommandGuardError` is raised when a guard the installer *can*
  establish could not be written or could not be verified. The reviewer
  worktree is rolled back on it rather than handed over as guarded.

Separating the decision surface from the mechanism is what lets the exchange be
exercised without the provider CLI the mechanism consults. The Codex
registration verifies its policy by asking ``codex execpolicy check``, which is
the only authority on its own rules and is therefore the right thing for
production to ask — but it also makes "was a guard bound to this reviewer
worktree, for the provider that will really sit in it" a question no hermetic
test could put without installing a provider CLI. Injecting the installer is
how that question gets asked at the port boundary instead.

Which providers can actually be registered with is deliberately *not* declared
here. It is derived from the installer's own registration table
(``adapters/worktree/_review_command_guard.GUARDABLE_PROVIDERS``), so a
provider cannot be called guardable without a registration behind it — the
repair that adds a name to a set and refuses nothing is one this codebase
cannot express. That differs from :data:`~.planning_command_guard.
GUARDABLE_PLANNING_PROVIDERS`, which lives in its port because two layers must
agree on it; here only the installer reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.artifact_contracts import AgentProvider
from .command_guard import GuardProbe

__all__ = [
    "GuardProbe",
    "ReviewCommandGuardError",
    "ReviewCommandGuardInstaller",
    "ReviewCommandGuardOutcome",
]


class ReviewCommandGuardError(RuntimeError):
    """A guard the installer can register was not established.

    Raised for a write failure and for a policy that did not verify as
    refusing, because the caller's decision is the same either way: a reviewer
    worktree whose barrier did not take must not be handed to a reviewer.
    """


@dataclass(frozen=True)
class ReviewCommandGuardOutcome:
    """What the installer did for one reviewer worktree.

    ``guarded`` is the fact callers have to branch on. It exists as a returned
    value rather than an assumed post-condition because the alternative — a
    ``Path`` for every provider — is what let a Claude-shaped settings file
    stand in for enforcement on providers that never read it.

    ``probes`` is empty for a mechanism whose enforcement is not established by
    classifying samples (the Claude hook runs the orchestrator's own pinned
    policy module), and carries the measured classifications for one that is
    (the Codex exec policy, whose file is data a checker must be asked about).
    """

    provider: AgentProvider
    policy_file: Path | None
    probes: tuple[GuardProbe, ...] = ()

    @property
    def guarded(self) -> bool:
        return self.policy_file is not None

    def refusals(self) -> tuple[str, ...]:
        return tuple(probe.label for probe in self.probes if probe.refused)

    def allowances(self) -> tuple[str, ...]:
        return tuple(probe.label for probe in self.probes if not probe.refused)


class ReviewCommandGuardInstaller(Protocol):
    """Establishes, and verifies, one reviewer worktree's command guard."""

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> ReviewCommandGuardOutcome:
        """Register the gate-command refusal in ``worktree_path`` if it can be.

        ``provider`` is the provider that will actually run in this worktree,
        as the exchange resolves it for launch. It is required, not defaulted:
        a default is what lets a guard be claimed for a provider that never
        reads it.

        Returns an outcome whose ``guarded`` is ``False``, having written
        nothing, for a provider this installer cannot register with.

        Raises:
            ReviewCommandGuardError: the guard could be registered for this
                provider and was not.
        """
        ...
