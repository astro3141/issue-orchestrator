"""Which exact artifact a live-assurance run is entitled to claim (#194).

The live-assurance record is the sole admission criterion for
``trusted-runtime-promote``, so "which build did the probes actually run
against" is a security question, not bookkeeping. Resolving it in a Makefile
recipe — ``--live-assurance-head-sha=$(git rev-parse HEAD)`` — put the answer
somewhere nothing could check: the SHA came from ``make``'s cwd while the
record's location came from a separate overridable variable, so the two could
name different checkouts and neither the record nor the store was in a
position to notice.

One function resolves both halves from one root, so they cannot disagree by
construction, and it routes through
:mod:`.repo_identity_resolution` — the codebase's existing owner of "what
exactly is this checkout" — rather than re-deriving identity at a call site.

Dirtiness is carried, not swallowed. A lane run over uncommitted edits is a
real thing an operator does while working on the sandbox boundary; what must
never happen is that run filing evidence under a commit those edits are not
in. :meth:`~..domain.live_assurance.LiveAssuranceRecord.assures` refuses a
dirty record, so the lane stays usable mid-change and the promotion gate stays
honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.commit_sha import normalize_commit_sha
from .repo_identity_resolution import build_repo_identity, working_tree_is_dirty


class AssuredArtifactUnresolvable(Exception):
    """A root that names no commit cannot be the subject of an assurance record."""


@dataclass(frozen=True, slots=True)
class AssuredArtifact:
    """The checkout a live-assurance run observed, as the record will state it."""

    head_sha: str
    working_tree_dirty: bool


def artifact_under_assurance(root: Path) -> AssuredArtifact:
    """The artifact a live-assurance run rooted at ``root`` is about."""
    identity = build_repo_identity(root)
    if identity.commit_sha is None:
        raise AssuredArtifactUnresolvable(
            f"{root} is not a checkout at a commit, so a live-assurance record "
            "filed from it would name an artifact nobody can look up"
        )
    try:
        head_sha = normalize_commit_sha(identity.commit_sha, field_name="head_sha")
    except (TypeError, ValueError) as exc:
        raise AssuredArtifactUnresolvable(
            f"{root} does not resolve to a full commit SHA: {exc}"
        ) from exc
    return AssuredArtifact(
        head_sha=head_sha, working_tree_dirty=working_tree_is_dirty(root)
    )


__all__ = [
    "AssuredArtifact",
    "AssuredArtifactUnresolvable",
    "artifact_under_assurance",
]
