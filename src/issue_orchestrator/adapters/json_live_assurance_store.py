"""JSON implementation of :class:`LiveAssuranceStore` (#194).

Records live at ``<root>/.issue-orchestrator/live-assurance/<HEAD_SHA>.json``
— a *separate* directory from ``.issue-orchestrator/validation/``, which is
keyed by validation contract kind. Two lanes, two locations: a reader who
walked into the wrong directory would find nothing rather than finding
something it could misread, and no assurance record can ever be picked up by
the validation cache's own glob.

The filename is the artifact SHA, so "is there a record for this exact commit"
is answered by a stat rather than by parsing and comparing. The parsed record
is still checked against the key it was found under, because a file placed
there by hand is untrusted input like any other.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.commit_sha import normalize_commit_sha
from ..domain.live_assurance import LiveAssuranceRecord
from ..infra.atomic_json import atomic_write_json

LIVE_ASSURANCE_DIR = Path(".issue-orchestrator") / "live-assurance"


class JsonLiveAssuranceStore:
    """Persist live-assurance records under a checkout's runtime directory."""

    def __init__(self, root: Path) -> None:
        self._base_dir = root / LIVE_ASSURANCE_DIR

    def record(self, record: LiveAssuranceRecord) -> None:
        atomic_write_json(self._path_for(record.head_sha), record.to_payload())

    def for_artifact(self, head_sha: str) -> LiveAssuranceRecord | None:
        path = self._path_for(head_sha)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Live-assurance record is unreadable: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Live-assurance record must contain an object: {path}")
        parsed = LiveAssuranceRecord.from_payload(payload)
        if not parsed.covers(head_sha):
            raise ValueError(
                f"Live-assurance record names a different artifact: {path} "
                f"holds {parsed.head_sha}"
            )
        return parsed

    def _path_for(self, head_sha: str) -> Path:
        normalized = normalize_commit_sha(head_sha, field_name="head_sha")
        return self._base_dir / f"{normalized}.json"


__all__ = ["LIVE_ASSURANCE_DIR", "JsonLiveAssuranceStore"]
