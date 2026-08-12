"""Durable records one review exchange leaves in its exchange directory.

Two artifacts live here, both written by the orchestrator and never by an
agent:

``summary.json``
    Facts about the exchange that just ran — terminal state, rounds, the
    reviewer's last text, and (when readable) the validation record's
    ``head_sha`` / ``passed``.

``review-verdict.json``
    The exact-SHA verdict binding (:class:`~..domain.review_verdict_binding.BoundReviewVerdict`):
    the verdict the orchestrator concluded, paired with the commit it observed
    in the coder worktree *before* presenting the round to the reviewer.

They are deliberately separate files. The summary records what happened; the
binding is an authority artifact that a later admission gate checks, and it
must not be reachable through a record whose other fields are policy inputs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from ..domain.review_exchange_summary import (
    ReviewExchangeReason,
    ReviewExchangeStatus,
    ReviewExchangeSummaryArtifactRef,
    ReviewExchangeSummaryV1,
    ReviewExchangeTerminalState,
)
from ..domain.review_exchange import ReviewExchangeResponse
from ..domain.review_verdict_binding import (
    REVIEW_VERDICT_BINDING_FILENAME,
    BoundReviewVerdict,
    ReviewVerdictOutcome,
    normalize_reviewed_sha,
)
from ..infra.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

SUMMARY_FILENAME = "summary.json"


def summary_path(exchange_dir: Path) -> Path:
    """Path of the exchange's summary record."""
    return exchange_dir / SUMMARY_FILENAME


def review_verdict_path(exchange_dir: Path) -> Path:
    """Path of the exchange's exact-SHA verdict binding."""
    return exchange_dir / REVIEW_VERDICT_BINDING_FILENAME


def read_validation_facts(path: Path | None) -> tuple[str | None, bool | None]:
    """Read ``(head_sha, passed)`` from a validation-record.json.

    Returns ``(None, None)`` when the path is None, missing, or
    unreadable as JSON. ``head_sha`` is None when the field is
    absent/empty; ``passed`` is None when the field is absent or
    not a bool.

    The summary writer (and the cache loader) use this to populate
    ``ResumeFacts`` without leaking validation-record schema concerns into
    other modules.
    """
    if path is None or not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        head_sha = None
    passed = data.get("passed")
    if not isinstance(passed, bool):
        passed = None
    return head_sha, passed


def write_exchange_summary(
    exchange_dir: Path,
    round_index: int,
    *,
    status: ReviewExchangeStatus,
    reason: ReviewExchangeReason,
    reviewer_response: ReviewExchangeResponse | None,
    validation_record_path: Path | None,
    review_artifacts: list[dict[str, str]] | None = None,
    detail: str | None = None,
) -> ReviewExchangeSummaryV1:
    """Persist summary.json atomically.

    The summary records *facts* about the exchange that just ran:
    ``status``, ``reason``, ``completed_rounds``, ``response_text``,
    ``timestamp``, plus — when the validation record is readable —
    ``head_sha`` and ``validation_passed``. Policy (cacheable / halt
    / retry / stale) is NOT encoded here; the cache loader feeds
    these fields into ``ReviewExchangeResumeDecision.decide`` to
    determine the next-tick action.

    Pre-this-commit, the writer encoded policy by selectively
    omitting ``head_sha`` based on status. That dual-purpose use of
    one field (fact AND control signal) was the root cause of the
    PR #6270 review-feedback whack-a-mole: every patch that adjusted
    "which statuses cache-hit" mutated which facts got persisted,
    and downstream consumers re-inferred policy at three different
    sites. Recording facts unconditionally and centralizing policy
    in one named helper ends that drift.

    ``head_sha`` and ``validation_passed`` are still omitted (rather
    than written as None) when the validation record cannot be
    read at all — the caller should treat absence as "we don't
    know" rather than "validation explicitly failed." The cache
    loader's ``ResumeFacts`` mapping handles each case.

    This ``head_sha`` is validation's, not review's. It is not the verdict
    binding and must not be read as one — see :func:`bind_review_verdict`.
    """
    terminal = ReviewExchangeTerminalState(status=status, reason=reason)
    head_sha, passed = read_validation_facts(validation_record_path)
    artifacts = tuple(
        ReviewExchangeSummaryArtifactRef.from_payload(artifact)
        for artifact in (review_artifacts or [])
    )
    summary = ReviewExchangeSummaryV1(
        completed_rounds=round_index,
        terminal=terminal,
        response_text=reviewer_response.response_text if reviewer_response else None,
        timestamp=datetime.now(timezone.utc).isoformat(),
        head_sha=head_sha,
        validation_passed=passed,
        artifacts=artifacts,
        detail=detail,
    )
    atomic_write_json(summary_path(exchange_dir), summary.to_payload())
    return summary


def bind_review_verdict(
    *,
    exchange_dir: Path,
    verdict: ReviewVerdictOutcome,
    presented_head_sha: str | None,
    completed_rounds: int,
) -> BoundReviewVerdict | None:
    """Bind ``verdict`` to the commit the orchestrator presented for review.

    ``presented_head_sha`` is the coder worktree HEAD observed *before* the
    reviewer round ran, so a commit made while the reviewer was working cannot
    end up inside an approval. Both halves come from the orchestrator: the
    reviewer's own decision JSON is never consulted here.

    Returns ``None`` — writing nothing — when the orchestrator could not
    observe the presented commit as a canonical SHA. That is deliberate: an
    unbound verdict is a verdict no later gate can admit, which is the safe
    direction. Fabricating a SHA, binding an unusable observation, or binding
    whatever HEAD happens to be current at decision time would be the unsafe
    ones. An unusable observation therefore never fails the review it
    describes. A failing *write* is not softened that way: an unwritable
    authority artifact raises, per the repository's fail-fast stance.
    """
    try:
        reviewed_sha = normalize_reviewed_sha(presented_head_sha)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[REVIEW_EXCHANGE] no usable presented HEAD for %s; recording no "
            "verdict binding (verdict=%s rounds=%d): %s",
            exchange_dir,
            verdict.value,
            completed_rounds,
            exc,
        )
        return None
    binding = BoundReviewVerdict(
        verdict=verdict,
        reviewed_sha=reviewed_sha,
        decided_at=datetime.now(timezone.utc).isoformat(),
        completed_rounds=completed_rounds,
    )
    atomic_write_json(review_verdict_path(exchange_dir), binding.to_payload())
    logger.info(
        "[REVIEW_EXCHANGE] bound verdict=%s to reviewed_sha=%s (%s)",
        binding.verdict.value,
        binding.reviewed_sha[:12],
        review_verdict_path(exchange_dir),
    )
    return binding


def load_review_verdict(exchange_dir: Path) -> BoundReviewVerdict | None:
    """Reload a previously bound verdict, or ``None`` if none was recorded.

    Reconstructed from storage alone, so the binding survives orchestrator
    restart. A binding that exists but does not parse raises rather than
    reading as "no verdict" — a corrupt authority artifact is not an absent
    one.
    """
    path = review_verdict_path(exchange_dir)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if not isinstance(payload, Mapping):
        raise ValueError(f"review verdict binding must be a JSON object: {path}")
    return BoundReviewVerdict.from_payload(payload)
