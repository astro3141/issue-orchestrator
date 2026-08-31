"""What a command guard measured before it called itself a barrier.

Two principals are refused the repository's build/test/validation entry points
by a policy the orchestrator installs into the worktree they run in: a
``planning_investigation`` Tech Lead (:mod:`.planning_command_guard`, #289) and
the review-exchange reviewer
(``adapters/worktree/_review_command_guard``, #396). They are refused for
different reasons and their policies say different things, but both are held to
the same standard — ``docs/architecture/hooks.md`` rules that a written file is
not enforcement, so "guarded" has to be something that was *asked of the
enforcing mechanism*, not inferred from a path existing.

:class:`GuardProbe` is that answer, for one command. It lives here rather than
in either principal's own module because a guard report is evidence, and
evidence that came out of two independently-shaped records would let one
principal's proof drift away from the other's while both still looked like
proof.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GuardProbe"]


@dataclass(frozen=True)
class GuardProbe:
    """One command whose classification by the established guard was measured.

    Recorded so the guard's report is evidence rather than assertion: the
    caller can see *which* commands were put to the enforcing mechanism and
    what it answered.

    What each caller then does with that is its own decision, and they differ
    today: the planning principal's launch owner writes the classifications
    into the run manifest, so they survive the launch; the reviewer's installer
    measures them and returns them, and the review exchange has no manifest
    seam to keep them in. Read this as "measured", not as "recorded somewhere
    after the fact", unless the principal you are reading says otherwise.
    """

    command: tuple[str, ...]
    refused: bool

    @property
    def label(self) -> str:
        return " ".join(self.command)
