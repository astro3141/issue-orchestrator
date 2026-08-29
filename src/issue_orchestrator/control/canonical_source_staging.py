"""Fetching ONE issue into an attributable, digested bundle on disk.

Two different runs need the same mechanics and must not grow two versions of
them:

* a ``planning_investigation`` stages its SUBJECT and the sources that subject
  declares (:mod:`.tech_lead_canonical_context`, #183);
* a ``batch_review`` stages, per audited candidate, the executable LEAF issue
  the pull request implements and the sources that leaf declares
  (:mod:`.tech_lead_candidate_contract`, #345).

What is shared is not policy but mechanism: read the issue and its conversation
through the existing :class:`~..ports.RepositoryHost` owners, write exactly the
bytes fetched, and record for each file the digest of exactly those bytes, so
the descriptor a reader is handed can be re-verified with an ordinary
``sha256sum``. The two callers differ only in what they declare REQUIRED and in
what they do when a required source cannot be staged — planning fails the launch
closed, batch review fails the one candidate closed — and that decision stays
with them.

The failure directions this module itself fixes:

* a REQUIRED source that cannot be fetched or staged raises ``ValueError``.
  Guessing at a governing document is worse than not having one.
* an OPTIONAL source that cannot be fetched is RECORDED as absent, with the
  reason. Absent and never-requested are different facts and read differently
  (a recorded entry with ``staged=False`` vs. no entry at all).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..domain.canonical_context import (
    CanonicalSource,
    CanonicalSourceKind,
    GoverningSourceDeclaration,
    StagedComment,
    content_digest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ports import RepositoryHost

logger = logging.getLogger(__name__)


def staging_timestamp() -> str:
    """The orchestrator clock, as a descriptor's ``fetched_at``."""
    return datetime.now(timezone.utc).isoformat()


def source_dir(bodies_dir: Path, issue_number: int) -> Path:
    """The staging directory for one source's body and comments."""
    return bodies_dir / f"issue-{issue_number}"


def _write_staged_file(path: Path, text: str) -> str:
    """Write exactly *text* and return its digest.

    Nothing is appended — no trailing newline, no header — so the recorded
    digest is the digest of the file on disk and stays verifiable with an
    ordinary ``sha256sum``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return content_digest(text)


def _comment_text(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    return body if isinstance(body, str) else ""


def _comment_identity(comment: dict[str, Any]) -> int:
    raw_id = comment.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise ValueError(f"issue comment is missing a usable id: {raw_id!r}")
    return raw_id


def _stage_comments(
    into: Path, comments: Sequence[dict[str, Any]]
) -> tuple[StagedComment, ...]:
    """Write each comment to its own file and describe what was staged.

    The descriptor lists ONLY comments actually written, so it never claims
    context the run did not receive.
    """
    staged: list[StagedComment] = []
    for comment in comments:
        comment_id = _comment_identity(comment)
        digest = _write_staged_file(
            into / f"comment-{comment_id}.md", _comment_text(comment)
        )
        staged.append(
            StagedComment(
                comment_id=comment_id,
                updated_at=str(comment.get("updated_at", "")),
                sha256=digest,
            )
        )
    return tuple(staged)


@dataclass(frozen=True, slots=True)
class StagedSource:
    """One staged source and the body text that was written for it.

    The body travels back with the descriptor entry for exactly one reason:
    the source's own DECLARATION is read from the same bytes that were staged
    and digested, so what the descriptor attributes and what the declaration
    was read from can never be two different revisions of the issue.
    """

    source: CanonicalSource
    body: str


def stage_issue_source(
    *,
    repository_host: "RepositoryHost",
    bodies_dir: Path,
    issue_number: int,
    kind: CanonicalSourceKind,
    required: bool,
) -> StagedSource:
    """Fetch and stage one source; raise for a required source that cannot be.

    Both fetches go through the EXISTING owners (``get_issue`` for the issue's
    revision identity and body, ``get_issue_comments`` for its conversation);
    this adds no fetch surface of its own. The subject is re-read here rather
    than described from an in-hand snapshot so every source's ``updated_at``
    is its revision at THIS launch, on one uniform path.

    ``get_issue_comments`` answers with ONE page, so a long conversation is
    staged partially (#185). The descriptor records the tracker's reported
    total beside what was staged — the count rides the SAME ``get_issue``
    payload already in hand, so reading it costs no extra call — and a reader
    tells a short conversation from a clipped one off the two numbers.
    Fetching the remaining pages is a separate concern and deliberately not
    done here.

    The COMMENTS ARE READ FIRST and the total second, and that order is load-
    bearing: the two reads are not atomic, and this orchestrator posts comments
    on the very issues it stages. Reading the total first would let a comment
    landing in the window between them produce a total SMALLER than the page
    already in hand — a descriptor the domain rightly rejects, which on a
    required source would kill an otherwise healthy staging over a benign
    interleaving. Read in this order, a comment arriving mid-stage can only
    make the later total LARGER, which reads as "clipped" — the honest answer,
    because that comment is genuinely not in the bundle.
    """
    fetched_at = staging_timestamp()
    try:
        into = source_dir(bodies_dir, issue_number)
        comments = _stage_comments(
            into, repository_host.get_issue_comments(issue_number)
        )
        issue = repository_host.get_issue(issue_number)
        if issue is None:
            raise ValueError(f"issue #{issue_number} was not found")
        body = issue.body or ""
        body_sha256 = _write_staged_file(into / "body.md", body)
        return StagedSource(
            source=CanonicalSource(
                kind=kind,
                issue_number=issue_number,
                required=required,
                fetched_at=fetched_at,
                staged=True,
                title=issue.title,
                state=issue.state,
                updated_at=issue.updated_at or "",
                body_sha256=body_sha256,
                comments=comments,
                comment_count=issue.comment_count,
            ),
            body=body,
        )
    except Exception as exc:
        if required:
            raise ValueError(
                f"required canonical source #{issue_number} could not be staged:"
                f" {exc}"
            ) from exc
        logger.warning(
            "[tech_lead] Optional canonical source #%s could not be staged;"
            " recording it as absent: %s",
            issue_number,
            exc,
        )
        # A source can fail HALFWAY (its body written, its comments not), and a
        # half-staged directory is text the descriptor attributes to nobody.
        # Drop it so what is on disk is exactly what the descriptor names.
        shutil.rmtree(source_dir(bodies_dir, issue_number), ignore_errors=True)
        return StagedSource(
            source=CanonicalSource(
                kind=kind,
                issue_number=issue_number,
                required=False,
                fetched_at=fetched_at,
                staged=False,
                # ``.strip()``, because ``CanonicalSource`` rejects an absence
                # reason that is only whitespace — an exception carrying, say,
                # a lone newline would otherwise turn an honest optional
                # degradation into an unhandled ValueError (PR #184 N3).
                absent_reason=str(exc).strip() or exc.__class__.__name__,
            ),
            body="",
        )


def stage_governing_sources(
    *,
    repository_host: "RepositoryHost",
    bodies_dir: Path,
    declarations: Sequence[GoverningSourceDeclaration],
) -> tuple[CanonicalSource, ...]:
    """Each declared source, in declaration order."""
    return tuple(
        stage_issue_source(
            repository_host=repository_host,
            bodies_dir=bodies_dir,
            issue_number=declaration.issue_number,
            kind=CanonicalSourceKind.GOVERNING,
            required=declaration.required,
        ).source
        for declaration in declarations
    )


__all__ = [
    "StagedSource",
    "source_dir",
    "stage_governing_sources",
    "stage_issue_source",
    "staging_timestamp",
]
