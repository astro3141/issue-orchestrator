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

Failure investigation and health review stage exactly what they staged
before. A BATCH REVIEW stages a different bundle for a different reason — the
executable leaf contract of each audited candidate (#345) — and it does so
through its own owner, :mod:`.tech_lead_candidate_contract`. The mechanics both
share (fetch, write, digest, degrade an optional source honestly) live in
:mod:`.canonical_source_staging`, so one rule cannot drift into two.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.canonical_context import (
    CANONICAL_CONTEXT_BODIES_DIRNAME,
    CANONICAL_CONTEXT_FILENAME,
    CanonicalContextSnapshot,
    CanonicalSourceKind,
    parse_governing_sources,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .canonical_source_staging import stage_governing_sources, stage_issue_source

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .worktree_context import WorktreeContext

logger = logging.getLogger(__name__)


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
    manifest entry and record no row through THIS owner, so their staging is
    unchanged. (A batch review stages its own per-candidate leaf contracts, on
    its own path; see :mod:`.tech_lead_candidate_contract`.)

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
    subject = stage_issue_source(
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
            *stage_governing_sources(
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
