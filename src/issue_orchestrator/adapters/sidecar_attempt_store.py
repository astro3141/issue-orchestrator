"""JSON sidecar implementation of :class:`AttemptStore`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..domain.attempt import Attempt, AttemptKey
from ..domain.issue_key import IssueKey
from ..domain.issue_key_codec import issue_key_path_part
from ..infra.atomic_json import atomic_write_json


class SidecarAttemptStore:
    """Persist attempts under ``.issue-orchestrator/attempts`` in a worktree."""

    def __init__(self, worktree: Path) -> None:
        self._base_dir = worktree / ".issue-orchestrator" / "attempts"

    def for_key(self, key: AttemptKey) -> Attempt | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        attempt = self._read(path)
        if not _names_same_attempt(attempt.key, key):
            raise ValueError(f"Attempt sidecar key mismatch: {path}")
        return attempt

    def _read(self, path: Path) -> Attempt:
        """The record one sidecar file states, or a loud failure.

        Shared by both readers so an enumeration cannot parse by a laxer rule
        than a keyed read: a payload one accepted and the other rejected would
        make "what is recorded for this candidate" depend on how it was asked.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Attempt sidecar is unreadable: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Attempt sidecar must contain an object: {path}")
        return Attempt.from_dict(payload)

    def update(self, key: AttemptKey, mutate: Callable[[Attempt], Attempt]) -> Attempt:
        existing = self.for_key(key)
        updated = mutate(existing if existing is not None else Attempt(key))
        if not _names_same_attempt(updated.key, key):
            raise ValueError(
                "Attempt update must stay under the key it was read from: "
                f"asked for {key.issue_scope}:{key.issue_stable_id}@{key.head_sha}, "
                f"got {updated.key.issue_scope}:{updated.key.issue_stable_id}"
                f"@{updated.key.head_sha}"
            )
        atomic_write_json(self._path_for(key), updated.to_dict())
        return updated

    def for_issue(self, issue_key: IssueKey) -> tuple[Attempt, ...]:
        if not self._base_dir.exists():
            return ()
        issue_prefix = f"{_issue_part(issue_key)}--"
        attempts = [
            self._read(path)
            for path in sorted(self._base_dir.glob(f"{issue_prefix}*.json"))
            if path.is_file()
        ]
        # Sorted by the sidecar's own filename above, which is
        # ``<issue part>--<head sha>.json`` — one spelling, so two readers of
        # this directory agree on order without re-deriving a rule.
        return tuple(attempts)

    def supersede_issue(self, issue_key: IssueKey) -> int:
        if not self._base_dir.exists():
            return 0
        issue_prefix = f"{_issue_part(issue_key)}--"
        removed = 0
        for path in self._base_dir.glob(f"{issue_prefix}*.json"):
            if not path.is_file():
                continue
            path.unlink()
            removed += 1
        return removed

    def _path_for(self, key: AttemptKey) -> Path:
        issue_part = _issue_part(key.issue_key)
        sha_part = key.head_sha
        return self._base_dir / f"{issue_part}--{sha_part}.json"


def _names_same_attempt(left: AttemptKey, right: AttemptKey) -> bool:
    """Whether two keys identify the same ``(issue, commit)`` on disk.

    Compared field-wise rather than by ``==``: the same issue can arrive as a
    ``GitHubIssueKey`` or a ``StoredIssueKey`` (what a reloaded sidecar
    rebuilds), and those are unequal dataclasses naming one issue.
    """
    return (
        left.issue_scope == right.issue_scope
        and left.issue_stable_id == right.issue_stable_id
        and left.head_sha == right.head_sha
    )


def _issue_part(issue_key: IssueKey) -> str:
    """The candidate's name on disk, in the one spelling every artifact uses.

    Shared with the publish gate's durable failure diagnostic (#94) rather than
    respelled here: that diagnostic and this sidecar are evidence about the same
    ``(issue, commit)``, and a reader who has found one has to be able to find
    the other by name.
    """
    return issue_key_path_part(issue_key)
