"""Owner for staging a planning run's CANONICAL CONTEXT (#183).

A ``planning_investigation`` run (#136) is sent to prepare one open issue. The
text that governs that preparation — the working procedure, the standing
policy — lives in other issues, and until now a Human had to carry it across
the boundary between Control and the run by hand. This module removes that
Human step: at launch it fetches the exact sources governing the subject and
stages them into the run directory as an attributable snapshot the agent can
name by issue, revision and digest.

What it writes, beside ``board-snapshot.json`` in ``tech-lead-data``::

    canonical-context.json              the descriptor (provenance only)
    canonical-context/issue-<n>/body.md          one staged body per source
    canonical-context/issue-<n>/comment-<id>.md  one file per staged comment

Every digest in the descriptor is the digest of exactly one of those files, so
the agent (or a later audit) can re-verify what it was given.

Two failure directions, deliberately different, and neither is
:func:`~.tech_lead_session_policy._stage_evidence_map`'s best-effort rule:

* a REQUIRED source that cannot be fetched or staged raises. The exception
  lands on the existing typed owner ``session_launcher._fail_launch_for_tech_lead_prep``
  — no session starts, the run is retry-queued, the authority row is
  discarded, the pre-active worktree is cleaned. This is
  ``_write_board_snapshot``'s fail-fast rule, for the same reason: a planning
  run missing a source it was told it needs is worse than no run at all.
* an OPTIONAL source that cannot be fetched is RECORDED as absent, with the
  reason. Absent and never-requested are different facts and read differently
  (a recorded entry with ``staged=False`` vs. no entry at all).

Provenance is also persisted in the orchestrator-owned tech_lead authority
ledger, keyed by run identity, so it answers "which sources governed that run"
long after the disposable planning worktree has been reaped. It is a SIBLING
of the launch authority, never part of it: the descriptor grants nothing.

Other flavors are untouched — batch review, failure investigation and health
review stage exactly what they staged before.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..domain.canonical_context import (
    CANONICAL_CONTEXT_BODIES_DIRNAME,
    CANONICAL_CONTEXT_FILENAME,
    CanonicalContextSnapshot,
    CanonicalSource,
    CanonicalSourceKind,
    GoverningSourceDeclaration,
    StagedComment,
    content_digest,
    parse_governing_sources,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .worktree_context import WorktreeContext

logger = logging.getLogger(__name__)


def _now() -> str:
    """The orchestrator clock, as the descriptor's ``fetched_at``."""
    return datetime.now(timezone.utc).isoformat()


def _source_dir(bodies_dir: Path, issue_number: int) -> Path:
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
    source_dir: Path, comments: Sequence[dict[str, Any]]
) -> tuple[StagedComment, ...]:
    """Write each comment to its own file and describe what was staged.

    The descriptor lists ONLY comments actually written, so it never claims
    context the run did not receive.
    """
    staged: list[StagedComment] = []
    for comment in comments:
        comment_id = _comment_identity(comment)
        digest = _write_staged_file(
            source_dir / f"comment-{comment_id}.md", _comment_text(comment)
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
class _StagedSource:
    """One staged source and the body text that was written for it.

    The body travels back with the descriptor entry for exactly one reason:
    the SUBJECT'S declaration is read from the same bytes that were staged and
    digested, so what the descriptor attributes and what the declaration was
    read from can never be two different revisions of the issue.
    """

    source: CanonicalSource
    body: str


def _stage_one_source(
    *,
    repository_host: "RepositoryHost",
    bodies_dir: Path,
    issue_number: int,
    kind: CanonicalSourceKind,
    required: bool,
) -> _StagedSource:
    """Fetch and stage one source; raise for a required source that cannot be.

    Both fetches go through the EXISTING owners (``get_issue`` for the issue's
    revision identity and body, ``get_issue_comments`` for its conversation);
    this adds no fetch surface of its own. The subject is re-read here rather
    than described from the in-hand snapshot so every source's ``updated_at``
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
    on the very issues it plans against. Reading the total first would let a
    comment landing in the window between them produce a total SMALLER than the
    page already in hand — a descriptor the domain rightly rejects, which on a
    required source (the subject always is one) would kill an otherwise healthy
    launch over a benign interleaving. Read in this order, a comment arriving
    mid-stage can only make the later total LARGER, which reads as "clipped" —
    the honest answer, because that comment is genuinely not in the bundle.
    """
    fetched_at = _now()
    try:
        source_dir = _source_dir(bodies_dir, issue_number)
        comments = _stage_comments(
            source_dir, repository_host.get_issue_comments(issue_number)
        )
        issue = repository_host.get_issue(issue_number)
        if issue is None:
            raise ValueError(f"issue #{issue_number} was not found")
        body = issue.body or ""
        body_sha256 = _write_staged_file(source_dir / "body.md", body)
        return _StagedSource(
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
        shutil.rmtree(_source_dir(bodies_dir, issue_number), ignore_errors=True)
        return _StagedSource(
            source=CanonicalSource(
                kind=kind,
                issue_number=issue_number,
                required=False,
                fetched_at=fetched_at,
                staged=False,
                absent_reason=str(exc) or exc.__class__.__name__,
            ),
            body="",
        )


def _staged_governing_sources(
    *,
    repository_host: "RepositoryHost",
    bodies_dir: Path,
    declarations: Sequence[GoverningSourceDeclaration],
) -> tuple[CanonicalSource, ...]:
    """Each declared source, in declaration order."""
    return tuple(
        _stage_one_source(
            repository_host=repository_host,
            bodies_dir=bodies_dir,
            issue_number=declaration.issue_number,
            kind=CanonicalSourceKind.GOVERNING,
            required=declaration.required,
        ).source
        for declaration in declarations
    )


def stage_canonical_context(
    *,
    repository_host: "RepositoryHost",
    tech_lead_authority: "TechLeadAuthorityStore",
    ctx: "WorktreeContext",
    run_dir: Path,
    flavor: TechLeadSessionFlavor,
    subject_issue: "Issue",
) -> CanonicalContextSnapshot | None:
    """Stage the canonical governing context of a planning subject.

    Returns ``None`` for every other flavor — batch review, failure
    investigation and health review reach no source, write no file, record no
    manifest entry and record no row, so their staging is unchanged.

    Which sources govern is read from the SUBJECT'S declaration
    (``Governed-by:`` / ``Governed-by-optional:`` in its body), never from a
    hardcoded bundle: a subject that declares none is staged with itself
    alone. The declaration is read from the body this owner just STAGED, not
    from the in-hand issue snapshot — the launch path's snapshot may carry no
    body at all, and a stale one would let the run be governed by a revision
    different from the one it was handed. The manifest entry and the durable
    row are both written only after the descriptor is on disk, so neither can
    point at a run whose bundle was never staged.
    """
    if flavor is not TechLeadSessionFlavor.PLANNING_INVESTIGATION:
        return None
    tech_lead_data = run_dir / "tech-lead-data"
    bodies_dir = tech_lead_data / CANONICAL_CONTEXT_BODIES_DIRNAME
    subject = _stage_one_source(
        repository_host=repository_host,
        bodies_dir=bodies_dir,
        issue_number=subject_issue.number,
        kind=CanonicalSourceKind.SUBJECT,
        required=True,
    )
    declarations = parse_governing_sources(
        subject.body, subject_issue_number=subject_issue.number
    )
    snapshot = CanonicalContextSnapshot(
        subject_issue_number=subject_issue.number,
        sources=(
            subject.source,
            *_staged_governing_sources(
                repository_host=repository_host,
                bodies_dir=bodies_dir,
                declarations=declarations,
            ),
        ),
    )
    path = tech_lead_data / CANONICAL_CONTEXT_FILENAME
    snapshot.write(path)
    ctx.update_manifest({"canonical_context": str(path)})
    tech_lead_authority.record_canonical_context(
        run_id=ctx.run.run_id, session_name=ctx.run.session_name, snapshot=snapshot
    )
    logger.info(
        "[tech_lead] Staged canonical context for planning subject #%s:"
        " %d source(s), %d absent (%s)",
        subject_issue.number,
        len(snapshot.sources),
        sum(1 for source in snapshot.sources if not source.staged),
        path,
    )
    clipped = [source for source in snapshot.sources if source.comments_truncated]
    if clipped:
        # Not a failure: the descriptor says so itself, per source. Worth
        # saying out loud because a run reasoning from a clipped governing
        # source is a quieter problem than one missing it outright.
        logger.warning(
            "[tech_lead] Canonical context for planning subject #%s staged only"
            " part of some conversations: %s",
            subject_issue.number,
            ", ".join(
                f"#{source.issue_number} staged {len(source.comments)} of"
                f" {source.comment_count} ({source.missing_comment_count} missing)"
                for source in clipped
            ),
        )
    return snapshot
