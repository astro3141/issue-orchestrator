"""Typed run-asset contract for orchestrator-managed completion commands."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ...domain.session_run import SessionRunAssets
from ...infra.env import ENV_PREFIX, get_env


class _RunAssetsRefused(Exception):
    """The injected run context does not prove out. Carries the operator text."""


@dataclass(frozen=True, slots=True)
class ManagedRunAssets:
    """What the owner-injected run context proved, or why it did not.

    Exactly one of the two is set. Callers that cannot continue without the
    run assets call :meth:`require`, which prints ``refusal`` and exits;
    callers whose behaviour merely *varies* with the managed run — routing
    questions, which must fail safe rather than fail the session — read
    :attr:`run_dir`. One proof serves both: nothing about the injected context
    changes between the two questions, so proving it twice from the same
    environment and the same manifest buys nothing.
    """

    assets: SessionRunAssets | None = None
    refusal: str = ""

    def __post_init__(self) -> None:
        if (self.assets is not None) == bool(self.refusal.strip()):
            raise ValueError(
                "ManagedRunAssets carries either resolved assets or a refusal, "
                "never both and never neither"
            )

    @property
    def run_dir(self) -> Path | None:
        """The proven run directory, or ``None`` when nothing proved out."""
        return None if self.assets is None else self.assets.run_dir

    def require(self) -> SessionRunAssets:
        """The proven assets, or exit non-zero saying why they are not.

        The second disposition of the one proof: a caller that cannot
        continue without the run assets spends the proof here rather than
        re-deriving it from the environment and the manifest a second time.
        """
        if self.assets is None:
            _die(self.refusal)
        return self.assets


def resolve_orchestrator_run_assets_for_session(
    worktree_root: Path,
    session_id: str,
) -> ManagedRunAssets:
    """Prove the owner-injected run context belongs to this session.

    Active orchestrator-managed completion is not allowed to rediscover a run
    directory. The session owner must inject ``ISSUE_ORCHESTRATOR_RUN_DIR`` and
    the manifest in that directory must prove the requested session identity.

    Refuses rather than raising so that both dispositions of a failed proof —
    fail the completion, or fall back to ordinary behaviour — are available
    from the one place that knows what proving means.
    """
    try:
        return ManagedRunAssets(assets=_proven_assets(worktree_root, session_id))
    except _RunAssetsRefused as refusal:
        return ManagedRunAssets(refusal=str(refusal))


def _proven_assets(worktree_root: Path, session_id: str) -> SessionRunAssets:
    run_dir_value = get_env("RUN_DIR")
    if not run_dir_value:
        _refuse(f"{ENV_PREFIX}RUN_DIR is required for orchestrator-managed validation")

    run_dir = Path(run_dir_value).expanduser().resolve()
    if not run_dir.is_dir():
        _refuse(f"{ENV_PREFIX}RUN_DIR does not exist: {run_dir}")

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        _refuse(f"{ENV_PREFIX}RUN_DIR is missing manifest.json: {run_dir}")

    manifest = _load_manifest(manifest_path)

    try:
        assets = SessionRunAssets.from_manifest_payload(
            run_dir=run_dir,
            manifest=manifest,
        )
    except (TypeError, ValueError) as exc:
        _refuse(f"{ENV_PREFIX}RUN_DIR manifest is invalid: {exc}")

    if assets.worktree_path.resolve() != worktree_root.resolve():
        _refuse(
            f"{ENV_PREFIX}RUN_DIR belongs to worktree "
            f"{assets.worktree_path}, expected {worktree_root}"
        )

    if assets.session_name != session_id:
        _refuse(
            f"{ENV_PREFIX}RUN_DIR belongs to '{assets.session_name}', "
            f"expected '{session_id}'"
        )

    return assets


def _load_manifest(manifest_path: Path) -> Mapping[str, Any]:
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        _refuse(f"{ENV_PREFIX}RUN_DIR manifest cannot be read: {manifest_path}: {exc}")
    except json.JSONDecodeError as exc:
        _refuse(f"{ENV_PREFIX}RUN_DIR manifest is invalid JSON: {manifest_path}: {exc}")
    if not isinstance(raw_manifest, dict):
        _refuse(f"{ENV_PREFIX}RUN_DIR manifest must be a JSON object: {manifest_path}")
    return raw_manifest


def _refuse(message: str) -> NoReturn:
    raise _RunAssetsRefused(message)


def _die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    print("\nUse --help for usage information.", file=sys.stderr)
    raise SystemExit(1)
