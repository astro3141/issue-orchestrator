"""The publish dirty-tree guard, with one owner (#385).

``prepush-check`` has always answered one question before it lets anything be
published: *is this checkout's tracked content committed?* Until #385 that
answer had exactly one caller, the CLI, so the mode vocabulary, the
runtime-metadata exclusion and the fail-closed direction lived inside it.

#385 gives it a second caller. The Tech Lead completion protocol used to make
the MODEL run ``prepush-check --dirty-only -v``; a bounded Tech Lead sandbox
cannot satisfy that command, because it records timings under the repository's
shared git common dir. The command moved to a trusted owner that runs outside
the sandbox — and a second implementation of "what counts as dirty" would be
exactly the cross-path rule drift that makes two gates disagree about the same
checkout. So the guard lives here, and both callers ask it.

**Fail closed, in every direction.** An unknown mode, and an enumeration that
did not succeed, are both refusals rather than passes: a checkout whose state
could not be read is not a checkout that was proven publishable. Only ``off``
— the operator's explicit opt-out — and a genuinely clean tree are
publishable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .runtime_artifacts import filter_runtime_managed_dirty_paths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ports.working_copy import WorkingCopy

__all__ = [
    "DEFAULT_DIRTY_CHECK_MODE",
    "DIRTY_CHECK_MODES",
    "DirtyTreeGuardResult",
    "DirtyTreeVerdict",
    "resolve_publish_dirty_check_mode",
    "run_dirty_tree_guard",
]

#: The mode vocabulary ``validation.publish.dirty_check`` accepts.
DIRTY_CHECK_MODES = ("tracked", "unstaged", "all", "off")

#: What an unconfigured repository gets: tracked content must be committed,
#: ignored and untracked files are the session's own business.
DEFAULT_DIRTY_CHECK_MODE = "tracked"


class DirtyTreeVerdict(Enum):
    """What the guard concluded about one checkout.

    ``publishable`` is a property of the member so adding a verdict forces its
    author to answer whether a checkout holding it may be published.
    """

    #: Nothing the guard judges is uncommitted.
    CLEAN = "clean"
    #: The operator turned the guard off (``dirty_check: off``).
    DISABLED = "disabled"
    #: Uncommitted content the guard judges.
    DIRTY = "dirty"
    #: The dirty-file enumeration did not succeed.
    UNENUMERABLE = "unenumerable"
    #: The configured mode is not one this build knows.
    INVALID_MODE = "invalid_mode"

    @property
    def publishable(self) -> bool:
        """Whether a checkout holding this verdict may be published."""
        return self in (DirtyTreeVerdict.CLEAN, DirtyTreeVerdict.DISABLED)


@dataclass(frozen=True, slots=True)
class DirtyTreeGuardResult:
    """The guard's verdict, the mode it used, and what it found."""

    verdict: DirtyTreeVerdict
    mode: str
    dirty_files: tuple[str, ...] = ()

    @property
    def publishable(self) -> bool:
        """Whether this checkout cleared the guard."""
        return self.verdict.publishable

    @property
    def detail(self) -> str:
        """One sentence naming the verdict and the mode it was reached under."""
        if self.verdict is DirtyTreeVerdict.INVALID_MODE:
            return (
                f"validation.publish.dirty_check is {self.mode!r}"
                f" (expected {'|'.join(DIRTY_CHECK_MODES)})"
            )
        if self.verdict is DirtyTreeVerdict.UNENUMERABLE:
            return (
                "the dirty files could not be enumerated"
                f" (dirty_check={self.mode!r})"
            )
        if self.verdict is DirtyTreeVerdict.DISABLED:
            return "the dirty-tree guard is disabled (dirty_check='off')"
        if self.verdict is DirtyTreeVerdict.DIRTY:
            shown = ", ".join(self.dirty_files[:5])
            more = len(self.dirty_files) - 5
            listing = f"{shown} (+{more} more)" if more > 0 else shown
            return f"uncommitted content (dirty_check={self.mode!r}): {listing}"
        return f"the checkout is clean (dirty_check={self.mode!r})"


def resolve_publish_dirty_check_mode(worktree: Path) -> str:
    """The ``validation.publish.dirty_check`` mode configured for ``worktree``.

    Read through the same runtime validation configuration ``prepush-check``
    reads, so the trusted owner and the CLI cannot be running two different
    policies against one repository. An unconfigured repository gets
    :data:`DEFAULT_DIRTY_CHECK_MODE`.

    Know which process is asking. ``load_runtime_validation_config`` selects the
    profile from ``ISSUE_ORCHESTRATOR_VALIDATION_PROFILE`` / ``CONFIG_PATH`` /
    ``MODE`` in the CALLER's environment, so when the trusted completion-
    validation owner (#385) calls this it resolves the ORCHESTRATOR's profile,
    which a differently-configured orchestrator could set to a stricter or
    looser ``dirty_check`` than the session's own profile names. That is
    strictness, not a hole: every divergence still fails closed — an unknown
    mode becomes :attr:`DirtyTreeVerdict.INVALID_MODE` and therefore a FAILED
    completion validation, and a raised ``ValueError`` becomes ``UNAVAILABLE``,
    which is also a refusal. Making the two profiles provably the same would
    mean carrying the session's resolved profile name on the launch record and
    reading it here instead of the ambient environment (#385 round 1 N3).
    """
    from .config import load_runtime_validation_config

    validation_config = load_runtime_validation_config(worktree)
    publish_config = validation_config.get("publish", {}) or {}
    return publish_config.get("dirty_check", DEFAULT_DIRTY_CHECK_MODE)


def run_dirty_tree_guard(
    worktree: Path,
    *,
    mode: str,
    working_copy: "WorkingCopy",
) -> DirtyTreeGuardResult:
    """Judge one checkout's publishability under ``mode``.

    Args:
        worktree: The checkout to judge.
        mode: One of :data:`DIRTY_CHECK_MODES`. Anything else is
            :attr:`DirtyTreeVerdict.INVALID_MODE` — refused, never defaulted,
            because silently substituting a mode would hide a typo in a gate.
        working_copy: The VCS port that enumerates dirty paths. Injected so the
            in-process trusted owner and the CLI share this logic without the
            CLI's process-local git adapter becoming a hidden dependency.

    Returns:
        The verdict, plus the runtime-metadata-filtered paths behind it.
    """
    if mode not in DIRTY_CHECK_MODES:
        return DirtyTreeGuardResult(DirtyTreeVerdict.INVALID_MODE, mode)
    if mode == "off":
        return DirtyTreeGuardResult(DirtyTreeVerdict.DISABLED, mode)
    raw = working_copy.list_dirty_files(worktree, mode)
    if raw is None:
        # Enumeration failed — fail closed instead of collapsing None to [],
        # which would pass the gate on an unreadable checkout.
        return DirtyTreeGuardResult(DirtyTreeVerdict.UNENUMERABLE, mode)
    dirty = tuple(filter_runtime_managed_dirty_paths(raw, worktree))
    if dirty:
        return DirtyTreeGuardResult(DirtyTreeVerdict.DIRTY, mode, dirty)
    return DirtyTreeGuardResult(DirtyTreeVerdict.CLEAN, mode)
