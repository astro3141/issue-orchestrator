"""Pair-scoped validation evidence for one persistent review exchange.

A persistent coder/reviewer pair outlives any single round, but a validation
record only speaks for the commit it names. This module owns the whole of that
tension: what makes a record usable right now
(:func:`validation_record_error`), and who keeps the pair's copy in step with
the coder's worktree (:class:`PairValidationMirror`).

Lifted out of ``persistent_session_exchange`` unchanged. It sat there because
that is where the pair is spawned, not because the exchange loop is what the
freshness rule is about — and keeping it there meant every reader of "is this
evidence current?" had to open a three-thousand-line module to find the
answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..infra.atomic_io import atomic_write_bytes
from ..infra.repo_identity import get_repo_head_sha


def validation_record_error(
    record_path: Path,
    *,
    current_head_sha: str | None,
) -> str | None:
    """Why ``record_path`` cannot stand for ``current_head_sha``, or ``None``.

    Every branch fails closed: a missing, unreadable, failing, unbound, or
    superseded record is not evidence, and an unobservable current HEAD means
    there is nothing for a record to be current *against*.
    """
    if not record_path.exists():
        return "validation-record.json missing"
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return "validation-record.json is not valid JSON"
    if not isinstance(data, dict):
        return "validation-record.json must be a JSON object"
    if data.get("passed") is not True:
        return "validation-record.json did not pass"
    if current_head_sha is None:
        return "cannot determine current HEAD for validation-record.json"
    record_head_sha = data.get("head_sha")
    if not isinstance(record_head_sha, str) or not record_head_sha:
        return "validation-record.json missing head_sha"
    if record_head_sha != current_head_sha:
        return (
            "validation-record.json head "
            f"{record_head_sha[:12]} does not match current HEAD "
            f"{current_head_sha[:12]}"
        )
    return None


@dataclass(frozen=True)
class PairValidationMirror:
    """Own the pair-scoped validation record's freshness contract.

    The persistent pair owns pair-scoped validation evidence, but validation is
    only valid for the coder worktree's current HEAD. This mirror is the
    single owner for invalidating stale pair records, copying the
    current validation owner's record into pair scope, and asserting
    that a required validation record both passed and matches HEAD.
    """

    pair_dir: Path
    record_path: Path
    coder_worktree_path: Path
    run_record_path: Path | None = None

    def replace_from_initial(self, source: Path | None) -> None:
        """Mirror the caller's current validation source at exchange start.

        A missing source clears any prior pair record. That is
        intentional: an exchange without current validation evidence
        must not inherit the last exchange's passing record.
        """
        self._replace_from(source)

    def refresh_from_completion(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> str | None:
        """Mirror validation evidence produced by this coder turn."""
        source, error = self._completion_validation_source(
            payload,
            run_validation_record_path=run_validation_record_path,
        )
        if error is not None:
            self._clear()
            return error
        self._replace_from(source)
        return None

    def observe_candidate_head(self) -> str | None:
        """The commit the *coder worktree* currently holds, or None.

        This is the commit validation evidence must name to still be current,
        and nothing else. Deliberately not the source of the verdict binding's
        ``reviewed_sha``, which names what the reviewer's worktree was checked
        out at — the coder's branch can move in between.
        ``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4 requires the two
        to agree; agreement is *checked* (stale validation routes the round to
        rework; a bound verdict re-derives ``approves`` against current HEAD),
        never assumed by reading one worktree and calling it both.

        It is also the commit a coder's escalation binds to (#386), for the
        same reason and with none of the evidence: the question is about the
        commit in front of the coder now.
        """
        return get_repo_head_sha(self.coder_worktree_path)

    def current_validation_error(self) -> str | None:
        return validation_record_error(
            self.record_path,
            current_head_sha=self.observe_candidate_head(),
        )

    def _completion_validation_source(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> tuple[Path | None, str | None]:
        raw_path = payload.get("validation_record_path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return (
                    None,
                    "completion validation_record_path must be a non-empty string",
                )
            return self._validated_worktree_path(raw_path)
        if run_validation_record_path.exists():
            return run_validation_record_path, None
        return None, None

    def _validated_worktree_path(self, raw_path: str) -> tuple[Path | None, str | None]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.coder_worktree_path / candidate
        try:
            resolved = candidate.resolve()
            worktree = self.coder_worktree_path.resolve()
            if resolved != self.record_path.resolve():
                resolved.relative_to(worktree)
        except (OSError, ValueError):
            return None, (
                "completion validation_record_path must stay under the coder worktree"
            )
        if not resolved.exists():
            return None, f"completion validation_record_path does not exist: {resolved}"
        if not resolved.is_file():
            return None, f"completion validation_record_path is not a file: {resolved}"
        return resolved, None

    def _replace_from(self, source: Path | None) -> None:
        if source is None or not source.exists():
            self._clear()
            return
        self.pair_dir.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        atomic_write_bytes(self.record_path, payload)
        if self.run_record_path is not None:
            atomic_write_bytes(self.run_record_path, payload)

    def _clear(self) -> None:
        self.record_path.unlink(missing_ok=True)
        if self.run_record_path is not None:
            self.run_record_path.unlink(missing_ok=True)


__all__ = ["PairValidationMirror", "validation_record_error"]
