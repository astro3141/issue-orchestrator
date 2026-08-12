"""Runtime setup step helpers for issue worktrees.

Each function here is one step of worktree runtime setup. The order in which
they run, and which of them run at all, is owned by
``_worktree_runtime_setup.WorktreeRuntimeSetup`` — not by callers.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from ...infra.runtime_artifacts import (
    ORCHESTRATOR_CLI_TOOLS_DIR,
    RUNTIME_IGNORE_FILE,
    load_runtime_ignore_patterns,
)
from ...ports.git import GitError
from ._worktree_errors import WorktreeError
from ._worktree_git import _git_run

logger = logging.getLogger(__name__)

# Marker file name for worktree identity.
WORKTREE_ID_MARKER = ".issue-orchestrator/worktree-id"

# Claude Code settings to enforce completion command usage on exit.
# The Stop hook checks for a marker file that coding-done/reviewer-done creates.
CLAUDE_SETTINGS_FOR_AGENTS: dict[str, Any] = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "test -f .agent-done-marker || echo '⚠️  WARNING: Session ending without completion command! Run: coding-done completed/blocked/needs_human'",
                        "timeout": 5,
                    }
                ]
            }
        ]
    }
}

ALLOW_NO_VERIFY_DRY_RUN_PATH = Path(".issue-orchestrator") / "allow-no-verify-dry-run"
WORKTREE_LOCAL_EXCLUDE_PATHS: tuple[Path, ...] = (
    Path(".agent-done-marker"),
    Path(".venv"),
    Path(".claude/settings.json"),
    Path(".claude/scheduled_tasks.lock"),
    Path(WORKTREE_ID_MARKER),
    ALLOW_NO_VERIFY_DRY_RUN_PATH,
    Path(".issue-orchestrator/ai-gate-state.json"),
    Path(".issue-orchestrator/backups"),
    Path(".issue-orchestrator/diagnostics"),
    Path(".issue-orchestrator/dirty-rejection-count.json"),
    RUNTIME_IGNORE_FILE,
    Path(".issue-orchestrator/session-latest.json"),
    Path(".issue-orchestrator/sessions"),
    Path(".issue-orchestrator/timeline.sqlite"),
    Path(".issue-orchestrator/timeline.sqlite-shm"),
    Path(".issue-orchestrator/timeline.sqlite-wal"),
    Path(".issue-orchestrator/tool-homes"),
    Path(".issue-orchestrator/validation"),
)
WORKTREE_TRACKED_RUNTIME_PATHS: tuple[Path, ...] = (
    Path(".claude/settings.json"),
    Path(".issue-orchestrator/session-latest.json"),
)

# Worktree-relative destination for the orchestrator's runtime CLI helpers.
CLI_TOOLS_WORKTREE_DIR = ORCHESTRATOR_CLI_TOOLS_DIR.as_posix()

__all__ = [
    "ALLOW_NO_VERIFY_DRY_RUN_PATH",
    "CLAUDE_SETTINGS_FOR_AGENTS",
    "CLI_TOOLS_WORKTREE_DIR",
    "WORKTREE_ID_MARKER",
    "WORKTREE_LOCAL_EXCLUDE_PATHS",
    "WORKTREE_TRACKED_RUNTIME_PATHS",
    "_configure_no_verify_dry_run",
    "_hide_runtime_artifacts_from_git_status",
    "_install_worktree_identity",
    "_link_repo_venv_into_worktree",
    "install_claude_settings",
    "sync_cli_tools",
]


def _configure_no_verify_dry_run(worktree_path: Path, allow: bool) -> None:
    """Enable or clear the ``--no-verify`` dry-run escape hatch for a worktree.

    The flag file is what the block-no-verify guardrail hook reads, so neither
    outcome of a dropped write is acceptable: a stale ``allow`` file leaves a
    hook bypass open for the whole session, and a missing one breaks the reuse
    push preflight. Fail the worktree setup instead of guessing.
    """
    flag_path = worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH
    try:
        if allow:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text("allow\n")
        elif flag_path.exists():
            flag_path.unlink()
    except OSError as exc:
        action = "set" if allow else "clear"
        raise WorktreeError(
            f"Failed to {action} no-verify dry-run flag at {flag_path}: {exc}"
        ) from exc


def _link_repo_venv_into_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Expose the repo venv inside a worktree so validation commands work there too."""
    source_venv = repo_root / ".venv"
    if not source_venv.exists():
        return

    target_venv = worktree_path / ".venv"
    if target_venv.is_symlink():
        try:
            if target_venv.resolve() == source_venv.resolve():
                return
        except OSError:
            pass
        target_venv.unlink()
    elif target_venv.exists():
        logger.warning(
            "Worktree already has a real .venv directory; leaving it in place: %s",
            target_venv,
        )
        return

    target_venv.symlink_to(source_venv, target_is_directory=True)
    logger.info(
        "Linked shared repo venv into worktree: %s -> %s", target_venv, source_venv
    )


def _git_or_fail(worktree_path: Path, argv: list[str], *, what: str) -> str:
    """Run a git command whose failure must not be papered over.

    Every caller below is either deciding whether the worktree's filesystem is
    allowed to differ from its commit, or repairing a case where it already
    does. A swallowed failure there produces exactly the silent divergence this
    step exists to prevent, so the result is checked and translated into this
    module's own failure vocabulary.

    Raises:
        WorktreeError: If the git command fails.
    """
    try:
        return _git_run(worktree_path, argv, check=True).stdout
    except GitError as exc:
        raise WorktreeError(f"Failed to {what} in {worktree_path}: {exc}") from exc


def _repo_owns_cli_tools(worktree_path: Path) -> bool:
    """Return True when the target repository tracks the CLI-tools path itself.

    This is the whole self-hosting discriminator, and it is a property of the
    repository rather than of any individual file. In a foreign repository
    ``src/issue_orchestrator/entrypoints/cli_tools/`` is orchestrator runtime
    and nothing else, so planting there invents files the repository has no
    opinion about. In Issue-Orchestrator's own repository the same path is
    product source the candidate branch may be changing, and a planted copy
    shadows the very commit validation and review are supposed to be reading.

    Asked once for the whole directory rather than per file on purpose: a
    half-planted directory would be a third answer to "which copy is
    authoritative", which is the defect, not a smaller version of the fix.
    """
    listed = _git_or_fail(
        worktree_path,
        ["ls-files", "--", CLI_TOOLS_WORKTREE_DIR],
        what="list tracked CLI tool paths",
    )
    return bool(listed.strip())


def _hidden_cli_tool_paths(worktree_path: Path) -> list[str]:
    """Return repo-owned CLI tool paths an earlier run marked ``--skip-worktree``."""
    listed = _git_or_fail(
        worktree_path,
        ["ls-files", "-v", "--", CLI_TOOLS_WORKTREE_DIR],
        what="read CLI tool index flags",
    )
    return [line[2:] for line in listed.splitlines() if line.startswith("S ")]


def _restore_repo_owned_cli_tools(worktree_path: Path) -> None:
    """Undo an overlay an earlier run planted over repo-owned CLI tools.

    Worktrees are long-lived and reused, so declining to plant is not enough on
    its own: a worktree set up before this rule existed still carries the
    planted copies, the ``--skip-worktree`` bits that stop git reporting them,
    and exclude entries that would hide a CLI tool the candidate *adds*. All
    three are cleared here, in the same step that decides not to plant, so
    there is one place that answers "what is in this directory".

    Restoring from the index cannot discard agent work. ``--skip-worktree``
    makes git refuse to stage those paths at all, so nothing written under them
    was ever committable; paths git is tracking normally are left untouched.
    """
    hidden = _hidden_cli_tool_paths(worktree_path)
    if hidden:
        _git_or_fail(
            worktree_path,
            ["update-index", "--no-skip-worktree", "--", *hidden],
            what="clear --skip-worktree on repo-owned CLI tools",
        )
        # Clearing the bit does not restore content git was told to ignore.
        _git_or_fail(
            worktree_path,
            ["checkout", "--", *hidden],
            what="restore repo-owned CLI tools from the index",
        )
        logger.warning(
            "Restored %d repo-owned CLI tool file(s) shadowed by an earlier "
            "orchestrator overlay in %s",
            len(hidden),
            worktree_path,
        )
    _drop_worktree_exclude_entries(worktree_path, f"{CLI_TOOLS_WORKTREE_DIR}/")


def _plant_cli_tools(worktree_path: Path) -> list[Path]:
    """Copy the orchestrator's CLI tools into a worktree that does not own them.

    Uses package-relative paths so this works when the target repo is a foreign
    (non-orchestrator) repository with no such directory of its own.
    """
    package_root = Path(__file__).resolve().parents[2]
    src_cli_tools = package_root / "entrypoints" / "cli_tools"
    dst_cli_tools = worktree_path / ORCHESTRATOR_CLI_TOOLS_DIR

    if not src_cli_tools.exists():
        logger.debug(
            "No cli_tools in orchestrator package at %s, skipping sync", src_cli_tools
        )
        return []

    dst_cli_tools.mkdir(parents=True, exist_ok=True)

    synced_paths: list[Path] = []
    for src_file in src_cli_tools.glob("*.py"):
        dst_file = dst_cli_tools / src_file.name
        try:
            shutil.copy2(src_file, dst_file)
            synced_paths.append(dst_file.relative_to(worktree_path))
            logger.debug("Synced cli tool: %s -> %s", src_file.name, dst_file)
        except OSError as e:
            logger.warning("Failed to sync cli tool %s: %s", src_file.name, e)

    logger.info("Synced cli_tools from orchestrator package to worktree")
    return synced_paths


def sync_cli_tools(worktree_path: Path) -> list[Path]:
    """Make a worktree's CLI-tools directory correct for its target repository.

    Two repositories, one path, and only one of them may own it:

    * A foreign target repository does not track
      ``src/issue_orchestrator/entrypoints/cli_tools/``. The orchestrator owns
      it there, so its copies are planted and their worktree-relative paths
      returned for the caller to hide from plain ``git status``.
    * Issue-Orchestrator's own repository tracks that path as product source,
      so the *candidate commit* owns it. Nothing is planted, any overlay a
      previous run left behind is undone, and an empty list is returned — the
      worktree's CLI tools are already the ones the branch is proposing.

    Planting over the second case is what breaks ``validated candidate
    filesystem == candidate Git HEAD``: static analysis and pytest read the
    worktree, so an overlay makes the gate report on source the candidate
    branch does not contain.

    An agent keeps its tools either way. ``coding-done`` and friends run from
    the orchestrator's venv on ``PATH`` with the orchestrator's ``src`` on
    ``PYTHONPATH`` (see ``control.session_env.build_session_env_exports``), so
    the planted copies are never the code that executes.

    Args:
        worktree_path: Path to the worktree.

    Returns:
        Worktree-relative paths of the files planted; empty when the target
        repository owns the destination.

    Raises:
        WorktreeError: If git cannot say whether the repository owns the
            destination, or an existing overlay cannot be undone. Guessing
            either way reintroduces the divergence this function prevents.
    """
    if _repo_owns_cli_tools(worktree_path):
        logger.info(
            "Target repository tracks %s; leaving the candidate's own CLI "
            "tools in place so the worktree matches its commit",
            CLI_TOOLS_WORKTREE_DIR,
        )
        _restore_repo_owned_cli_tools(worktree_path)
        return []
    return _plant_cli_tools(worktree_path)


def _read_worktree_identity(marker_path: Path) -> str | None:
    """Return the persisted worktree identity, or None if it must be created.

    Content policy and I/O policy are deliberately different here.

    Content that cannot carry an identity — an empty marker, or bytes that are
    not UTF-8 — is regenerated: there is nothing to preserve, so a fresh id
    loses no information.

    An I/O failure says nothing about the content. The marker may hold a
    perfectly good identity this process simply could not read, and the caller's
    next move is to write a new one. That would silently rebrand the worktree
    and make every job holding the old id believe its worktree was replaced, so
    a read error aborts setup instead and leaves the file alone.

    Raises:
        WorktreeError: If an existing marker cannot be read.
    """
    if not marker_path.exists():
        return None
    try:
        existing_id = marker_path.read_text().strip()
    except UnicodeDecodeError as exc:
        logger.warning(
            "Non-UTF-8 worktree identity marker, regenerating: path=%s error=%s",
            marker_path,
            exc,
        )
        return None
    except OSError as exc:
        raise WorktreeError(
            f"Failed to read worktree identity marker at {marker_path}: {exc}"
        ) from exc
    if not existing_id:
        logger.warning("Empty worktree identity marker, regenerating: %s", marker_path)
        return None
    return existing_id


def _install_worktree_identity(worktree_path: Path) -> str:
    """
    Install a unique identity marker in the worktree.

    This identity is used to detect path reuse - if a worktree is deleted
    and recreated at the same path, it gets a new identity. Jobs store
    the worktree_id and can detect when their worktree has been replaced.

    The identity is only created once - subsequent calls are idempotent.

    Args:
        worktree_path: Path to the worktree

    Returns:
        The worktree identity (existing or newly created)

    Raises:
        WorktreeError: If an existing identity cannot be read, or a new one
            cannot be persisted. Returning an unpersisted id would hand jobs a
            value no later run can match, silently disabling path-reuse
            detection; overwriting an unreadable one would change the identity
            of a worktree that already had a valid id.
    """
    marker_path = worktree_path / WORKTREE_ID_MARKER

    existing_id = _read_worktree_identity(marker_path)
    if existing_id:
        logger.debug("Worktree identity exists: %s", existing_id)
        return existing_id

    worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(worktree_id)
    except OSError as exc:
        raise WorktreeError(
            f"Failed to install worktree identity at {marker_path}: {exc}"
        ) from exc
    logger.info("Installed worktree identity: %s", worktree_id)
    return worktree_id


def _worktree_git_dir(worktree_path: Path) -> Path | None:
    git_file = worktree_path / ".git"
    if not git_file.exists():
        return None
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return None
    return Path(content.split(":", 1)[1].strip())


def _worktree_git_common_dir(worktree_path: Path) -> Path | None:
    git_dir = _worktree_git_dir(worktree_path)
    if git_dir is None:
        return
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text().strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def _append_exclude_entries(exclude_path: Path, paths: list[Path]) -> None:
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    existing_text = ""
    if exclude_path.exists():
        existing_text = exclude_path.read_text()
        existing_lines = existing_text.splitlines()
    existing = {line.strip() for line in existing_lines if line.strip()}
    missing = [
        str(path).replace("\\", "/")
        for path in paths
        if str(path).replace("\\", "/") not in existing
    ]
    if not missing:
        return
    suffix = "\n" if existing_lines and not existing_text.endswith("\n") else ""
    with exclude_path.open("a", encoding="utf-8") as handle:
        if suffix:
            handle.write(suffix)
        for entry in missing:
            handle.write(f"{entry}\n")


def _remove_exclude_entries(exclude_path: Path, prefix: str) -> int:
    """Drop exclude entries under ``prefix``; return how many were removed.

    The inverse of ``_append_exclude_entries``, and needed for the same reason
    the skip-worktree bits are cleared: an exclude entry written when the
    orchestrator owned a path keeps hiding that path from ``git status`` after
    the repository turns out to own it, including a file the candidate adds.
    """
    if not exclude_path.exists():
        return 0
    lines = exclude_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.strip().startswith(prefix)]
    removed = len(lines) - len(kept)
    if removed:
        exclude_path.write_text(
            "".join(f"{line}\n" for line in kept), encoding="utf-8"
        )
    return removed


def _worktree_exclude_files(worktree_path: Path) -> list[Path]:
    """Return every exclude file git reads for this worktree.

    A linked worktree has its own ``info/exclude`` *and* reads the common git
    dir's. Both are returned so an entry is written — and removed — everywhere
    git would honour it; a stale entry left in the common dir would leak across
    every other worktree of the same repository.
    """
    git_dir = _worktree_git_dir(worktree_path)
    if git_dir is None:
        return []
    common_dir = _worktree_git_common_dir(worktree_path)
    exclude_paths = [git_dir / "info" / "exclude"]
    if common_dir is not None and common_dir != git_dir:
        exclude_paths.append(common_dir / "info" / "exclude")
    return exclude_paths


def _write_worktree_exclude_entries(worktree_path: Path, paths: list[Path]) -> None:
    for exclude_path in _worktree_exclude_files(worktree_path):
        _append_exclude_entries(exclude_path, paths)


def _drop_worktree_exclude_entries(worktree_path: Path, prefix: str) -> None:
    removed = sum(
        _remove_exclude_entries(exclude_path, prefix)
        for exclude_path in _worktree_exclude_files(worktree_path)
    )
    if removed:
        logger.warning(
            "Removed %d stale git exclude entr(ies) under %s in %s; the "
            "repository owns that path",
            removed,
            prefix,
            worktree_path,
        )


def _worktree_git_exclude_paths(
    worktree_path: Path, synced_cli_tool_paths: list[Path]
) -> list[Path]:
    """Return untracked paths that should be hidden from plain git status.

    This covers both runtime-only metadata and the synced CLI helper files we
    plant into foreign worktrees so first-run agents don't misread a clean
    session as a dirty repo before they make any user-facing change.
    """
    # Path normalisation intentionally widens trailing-slash patterns from
    # directory-only to file-or-directory when writing Git excludes. The
    # runtime-ignore file is an additive hide list, so broader exclusion is
    # safer than leaving agent-visible runtime artifacts in plain git status.
    repo_local_runtime_paths = [
        Path(pattern) for pattern in load_runtime_ignore_patterns(worktree_path)
    ]
    return [
        *WORKTREE_LOCAL_EXCLUDE_PATHS,
        *repo_local_runtime_paths,
        *synced_cli_tool_paths,
    ]


def _hide_runtime_artifacts_from_git_status(
    worktree_path: Path,
    synced_cli_tool_paths: list[Path],
) -> None:
    """Keep runtime artifacts out of plain ``git status`` for this worktree.

    ``--skip-worktree`` is applied only to ``WORKTREE_TRACKED_RUNTIME_PATHS``:
    paths the orchestrator rewrites in place and whose content is runtime
    configuration in every repository. It is deliberately *not* applied to
    planted CLI tools. The bit tells git to stop reporting a path as modified,
    so on a path the target repository tracks it would hide a divergence
    between the worktree and candidate HEAD — and ``sync_cli_tools`` only ever
    returns paths in a repository that does not track them, where an exclude
    entry is both sufficient and honest.
    """
    for path in WORKTREE_TRACKED_RUNTIME_PATHS:
        normalized = str(path).replace("\\", "/")
        tracked = _git_run(
            worktree_path,
            ["ls-files", "--error-unmatch", normalized],
            check=False,
        )
        if tracked.returncode != 0:
            continue
        _git_run(
            worktree_path,
            ["update-index", "--skip-worktree", "--", normalized],
            check=False,
        )
    _write_worktree_exclude_entries(
        worktree_path,
        _worktree_git_exclude_paths(worktree_path, synced_cli_tool_paths),
    )


def _read_mergeable_claude_settings(settings_file: Path) -> dict[str, Any] | None:
    """Return existing settings safe to merge into, or None to write a fresh file.

    Content the merge cannot use — non-UTF-8 bytes, non-JSON text, a non-object
    document, or a wrong-shaped ``hooks`` entry — resolves to "replace": the
    Stop hook is a completion guardrail, so a broken file must never leave a
    worktree without it. Replacement discards operator content, so it is logged
    at WARNING rather than swallowed.

    A read failure is *not* a content verdict. Replacing a file we merely failed
    to open would throw away operator settings that are perfectly intact, so it
    fails setup and leaves the file untouched.

    A ``hooks`` key that is present but not an object (``null`` included) is
    wrong-shaped, not absent — the merge would try to ``setdefault`` into a
    non-mapping. Only a genuinely missing key is safe to merge into.

    Raises:
        WorktreeError: If an existing settings file cannot be read.
    """
    if not settings_file.exists():
        return None

    try:
        raw_settings = settings_file.read_text()
    except UnicodeDecodeError as exc:
        logger.warning(
            "Replacing non-UTF-8 Claude settings: path=%s error=%s", settings_file, exc
        )
        return None
    except OSError as exc:
        raise WorktreeError(
            f"Failed to read existing Claude settings at {settings_file}: {exc}"
        ) from exc

    try:
        existing = json.loads(raw_settings)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Replacing unreadable Claude settings: path=%s error=%s", settings_file, exc
        )
        return None

    if not isinstance(existing, dict):
        logger.warning("Replacing non-object Claude settings: %s", settings_file)
        return None

    if "hooks" in existing:
        hooks = existing["hooks"]
        if not isinstance(hooks, dict):
            logger.warning(
                "Replacing Claude settings with non-object 'hooks': %s", settings_file
            )
            return None
        if not isinstance(hooks.get("Stop", []), list):
            logger.warning(
                "Replacing Claude settings with non-list 'hooks.Stop': %s", settings_file
            )
            return None

    return existing


def _merge_agent_stop_hook(existing: dict[str, Any] | None) -> dict[str, Any]:
    """Return settings that contain the agent Stop hook exactly once."""
    if existing is None:
        return copy.deepcopy(CLAUDE_SETTINGS_FOR_AGENTS)

    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    our_hook = CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"][0]
    if our_hook not in stop_hooks:
        stop_hooks.append(copy.deepcopy(our_hook))
    return merged


def install_claude_settings(worktree_path: Path) -> None:
    """
    Install Claude Code settings to enforce completion command usage on exit.

    Creates .claude/settings.json in the worktree with a Stop hook
    that checks if a completion command was called before allowing exit.

    Args:
        worktree_path: Path to the worktree

    Raises:
        WorktreeError: If an existing settings file cannot be read, or the
            settings file cannot be written. The Stop hook is the only reminder
            an agent gets to run a completion command, so a worktree without it
            is not a runnable session — and an unreadable file is not a licence
            to overwrite whatever the operator put there.
    """
    worktree_path = Path(worktree_path)
    claude_dir = worktree_path / ".claude"
    settings_file = claude_dir / "settings.json"

    settings = _merge_agent_stop_hook(_read_mergeable_claude_settings(settings_file))
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2))
    except OSError as exc:
        raise WorktreeError(
            f"Failed to install Claude settings at {settings_file}: {exc}"
        ) from exc

    logger.debug("Installed Claude settings at %s", settings_file)
