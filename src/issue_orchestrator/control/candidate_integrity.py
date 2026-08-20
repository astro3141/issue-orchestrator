"""Proving one operation left a candidate's commit and tracked content alone.

Two reads of the same working copy — ``get_head_sha`` and
``has_uncommitted_changes`` — taken before an operation and again afterwards.
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

The check is asymmetric on purpose. Moving ``HEAD`` is always the operation's
doing. A worktree that was ALREADY dirty stays a question this cannot answer,
so only a clean-to-dirty transition is attributed. Changes are RETURNED rather
than raised, because callers turn them into different things — a worktree that
is not runnable, a preparation that produced no usable evidence — and this
owner must not decide which.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ports.working_copy import WorkingCopy


@dataclass(frozen=True, slots=True)
class CandidateCheckpoint:
    """What the candidate looked like immediately before an operation."""

    head_sha: str | None
    dirty: bool


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
        """Read the candidate's commit and dirtiness right now."""
        return CandidateCheckpoint(
            head_sha=self._working_copy.get_head_sha(worktree_path),
            dirty=self._working_copy.has_uncommitted_changes(worktree_path),
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
        if after.dirty and not before.dirty:
            return f"{self._operation} left uncommitted changes in {worktree_path}"
        return None


__all__ = ["CandidateCheckpoint", "CandidateIntegrity"]
