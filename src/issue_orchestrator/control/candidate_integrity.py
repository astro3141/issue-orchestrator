"""Proving one operation left a candidate's commit and tracked content alone.

Two reads of the same working copy — the commit ``HEAD`` stands at, and the
dirt a guard would block on — taken before an operation and again afterwards.
The rule they enforce is one sentence: an operation the orchestrator runs
*inside* a candidate's checkout may write untracked runtime state, and may not
move ``HEAD`` or leave the candidate's tracked content modified.

It lived inside :class:`~.worktree_runnability.WorktreeRunnability`, whose
module docstring still states the rule, because provisioning was the only
operation that needed it. A second operation now runs in the same checkout
under the same rule — the continuation's quick-validation preparation (#173) —
and a second private copy of "take a checkpoint, compare it afterwards" is how
two callers start disagreeing about what altering the candidate means. So the
two reads and the comparison have one owner, and each caller supplies only the
name of the operation whose doing a change would be.

**What counts as dirt is not decided here.** That question already has an owner
— ``list_dirty_files(..., "tracked")`` for which dirt is the candidate's own
content, and :func:`~..infra.runtime_artifacts.filter_runtime_managed_dirty_paths`
for which of those paths are runtime metadata every dirty surface ignores — and
this postflight asks it rather than answering it again. Reading raw
``git status --porcelain`` here instead would have been a second, stricter rule
under the same words: the operations run in these checkouts *emit files*
(a suite's JUnit XML, a coverage database, a setup step's caches), so an
untracked report a passing suite wrote would read as the candidate being
altered, and an operator who declared that report in ``runtime-ignore``
— documented as the way to stop repo-local runtime files blocking guards —
would be refused here anyway.

The check is asymmetric on purpose. Moving ``HEAD`` is always the operation's
doing. A path that was ALREADY dirty stays a question this cannot answer, so
only dirt that appeared between the two reads is attributed. Changes are
RETURNED rather than raised, because callers turn them into different things —
a worktree that is not runnable, a preparation that produced no usable
evidence — and this owner must not decide which.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..infra.runtime_artifacts import filter_runtime_managed_dirty_paths
from ..ports.working_copy import WorkingCopy

CANDIDATE_DIRT_MODE = "tracked"
"""The dirt an operation in a candidate's checkout may not leave behind.

Tracked content, staged or not. Untracked files are excluded at the source
rather than filtered afterwards, because "may write untracked runtime state" is
the rule itself and not a concession to a list of known filenames.
"""

_DIRT_PREVIEW = 5
"""How many altered paths a returned message names before summarising."""


@dataclass(frozen=True, slots=True)
class CandidateCheckpoint:
    """What the candidate looked like immediately before an operation.

    ``dirty_paths`` is ``None`` when the enumeration itself failed, which is
    not the same fact as "nothing was dirty" (``()``) and must not collapse
    into it: a checkout whose dirt could not be read is one whose integrity
    cannot be proved either way.
    """

    head_sha: str | None
    dirty_paths: tuple[str, ...] | None


class CandidateIntegrity:
    """Checkpoints a candidate's checkout and names what an operation changed."""

    def __init__(self, working_copy: WorkingCopy, *, operation: str) -> None:
        """Bind the reads to the operation whose doing a change would be.

        Args:
            working_copy: The working-copy port both reads go through.
            operation: How a change is attributed in the returned message —
                the caller's own name for what ran between the two
                checkpoints ("provisioning", "quick validation").
        """
        self._working_copy = working_copy
        self._operation = operation

    def checkpoint(self, worktree_path: Path) -> CandidateCheckpoint:
        """Read the candidate's commit and blocking dirt right now."""
        return CandidateCheckpoint(
            head_sha=self._working_copy.get_head_sha(worktree_path),
            dirty_paths=self._candidate_dirt(worktree_path),
        )

    def describe_change(
        self, worktree_path: Path, before: CandidateCheckpoint
    ) -> str | None:
        """Name what the operation changed about the candidate, or ``None``.

        Returns rather than raises so a caller can report it alongside an
        operation that failed *after* making the change: a failing command and
        an altered candidate are two separate facts, and the first must not
        suppress the second.
        """
        after = self.checkpoint(worktree_path)
        if after.head_sha != before.head_sha:
            return (
                f"{self._operation} moved HEAD in {worktree_path}: "
                f"{before.head_sha} -> {after.head_sha}"
            )
        if after.dirty_paths is None:
            # Fail closed on the read that decides the outcome: an operation
            # whose aftermath could not be read is indistinguishable from one
            # that altered the candidate, and only one of the two is safe.
            return (
                f"{self._operation} left {worktree_path} unprovable: its tracked "
                "changes could not be enumerated afterwards"
            )
        if before.dirty_paths is None:
            # The pre-state could not be read, so nothing found afterwards can
            # be attributed to this operation — the same "was it already like
            # this?" question the asymmetry above declines to answer.
            return None
        already_dirty = set(before.dirty_paths)
        appeared = tuple(
            path for path in after.dirty_paths if path not in already_dirty
        )
        if appeared:
            return (
                f"{self._operation} modified tracked content in {worktree_path}: "
                f"{_summarise(appeared)}"
            )
        return None

    def _candidate_dirt(self, worktree_path: Path) -> tuple[str, ...] | None:
        """Ask the existing owners which paths a guard would block on."""
        dirty = self._working_copy.list_dirty_files(worktree_path, CANDIDATE_DIRT_MODE)
        if dirty is None:
            return None
        return tuple(sorted(filter_runtime_managed_dirty_paths(dirty, worktree_path)))


def _summarise(paths: tuple[str, ...]) -> str:
    preview = ", ".join(paths[:_DIRT_PREVIEW])
    remaining = len(paths) - _DIRT_PREVIEW
    return f"{preview} (+{remaining} more)" if remaining > 0 else preview


__all__ = ["CANDIDATE_DIRT_MODE", "CandidateCheckpoint", "CandidateIntegrity"]
