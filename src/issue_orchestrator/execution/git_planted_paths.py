"""Which repository owns the orchestrator's planted CLI-tools path.

``sync_cli_tools`` plants the orchestrator's runtime CLI helpers into
``ORCHESTRATOR_CLI_TOOLS_DIR``, and that path means two different things
depending on the target repository: orchestrator runtime in a foreign one,
Issue-Orchestrator product source in this one. Every dirty-tree surface has to
classify untracked files under it, and they must all classify them the same
way — a second answer to "which copy is authoritative" is the defect this seam
exists to prevent, not a smaller version of the fix.

So the question is asked in exactly one form, of the index, of the whole
directory: the repository owns the path when it tracks anything there. The
planting step asks it through the worktree adapter's own git access (in that
adapter's error vocabulary); every consumer of the answer asks it here.

Part of the execution layer: it reads git and reports, it decides nothing. What
the answer *means* for a dirty path belongs to
``infra.runtime_artifacts.is_orchestrator_untracked_planted``.
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.git.git_cli import GitCLI
from ..execution.command_runner import LocalCommandRunner
from ..infra.runtime_artifacts import ORCHESTRATOR_CLI_TOOLS_DIR
from ..ports.command_runner import OutputNewlines
from ..ports.git import Git


def repo_owns_planted_cli_tools(git: Git, worktree: Path) -> bool:
    """Return True when ``worktree``'s repository tracks the planted CLI-tools path.

    Raises:
        GitError: If git cannot read the index for that path. Neither answer is
            safe to assume: "orchestrator" hides a file the candidate added, and
            "repository" fails a foreign worktree's dirty guard on files the
            agent never wrote. Callers decide how to fail, but they must decide
            knowingly.
    """
    result = git.run(
        worktree,
        ["ls-files", "-z", "--", ORCHESTRATOR_CLI_TOOLS_DIR.as_posix()],
        check=True,
        newlines=OutputNewlines.PRESERVED,
    )
    return any(path for path in result.stdout.split("\0"))


def local_repo_owns_planted_cli_tools(worktree: Path) -> bool:
    """Same answer, for a caller with no ``Git`` of its own to inject.

    The CLI tools agents run are standalone processes rather than composed
    objects, and entrypoints may not reach for an adapter themselves. Composing
    the local git here keeps them one call away from the shared answer instead
    of growing a second way to ask.

    Raises:
        GitError: As :func:`repo_owns_planted_cli_tools`.
    """
    return repo_owns_planted_cli_tools(GitCLI(runner=LocalCommandRunner()), worktree)
