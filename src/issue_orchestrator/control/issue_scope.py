"""Configured issue scope decisions for launch and reset paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue


@dataclass(frozen=True)
class IssueScopeDecision:
    """Decision for whether an issue belongs to this engine's issue scope."""

    in_scope: bool
    code: str = "ok"
    detail: str | None = None


def evaluate_issue_scope(
    config: "Config",
    issue: "Issue",
    *,
    require_open: bool = True,
    include_milestone_filter: bool = True,
    include_issue_number_filter: bool = False,
) -> IssueScopeDecision:
    """Apply the configured issue-scope gates to one issue snapshot.

    ``include_issue_number_filter`` is opt-in because queue snapshots retain
    all scoped issues, then apply single-issue scheduling at queue eligibility.
    Hidden reset preflight enables it to explain whether the requested issue
    can be reintroduced by this engine.
    """
    if require_open:
        current = str(issue.state or "").lower()
        if current == "closed":
            return _outside("issue_not_open", "issue is closed")

    marks = list(issue.labels)
    required_mark = config.filtering.label
    if required_mark and required_mark not in marks:
        return _outside(
            "missing_filter_label",
            f'missing required filter label "{required_mark}"',
        )

    if include_milestone_filter:
        allowed_milestones = config.get_filter_milestones()
        current_milestone = issue.milestone
        if allowed_milestones and current_milestone not in allowed_milestones:
            displayed_milestone = current_milestone or "none"
            return _outside(
                "outside_milestone_filter",
                f'milestone "{displayed_milestone}" is not one of '
                f"{', '.join(allowed_milestones)}",
            )

    detail = config.get_issue_filter().exclusion_reason(issue)
    if detail is not None:
        return _outside("excluded_by_label_filter", detail)

    if include_issue_number_filter and outside_single_issue_scope(config, issue):
        return _outside(
            "outside_single_issue_scope",
            f"engine is scoped to issue #{config.filtering.issue}",
        )

    return IssueScopeDecision(in_scope=True)


class EngineIssueScope:
    """What this engine is allowed to ACT on, as something a collaborator holds.

    :func:`evaluate_issue_scope` is a decision *over a* ``Config``, and a
    collaborator handed the ``Config`` to ask it is a collaborator that can ask
    anything else of it too — including reading ``filtering.issue`` back out and
    forming a second opinion about what ``--issue`` means. #304 measured what
    the absence of an owner costs on the other side of the same coin: a
    work-admitting producer that asked nothing at all, because there was nothing
    it could naturally hold.

    So the composite scope question — labels, milestone, open state AND the
    operator's ``--issue`` narrowing — is handed over as an object with exactly
    one method. :class:`~.queue_cache.QueueCache` and the continuation's rework
    handoff cannot drift apart about the answer, because there is one expression
    of it and they both hold it.

    Scope only. "In scope but already claimed this run" is a different question
    and stays with :meth:`~.queue_cache.QueueCache.evaluate_issue`; see
    :meth:`~.queue_cache.QueueCache.is_outside_engine_scope` for why that
    composite verdict cannot answer this one.
    """

    __slots__ = ("_config",)

    def __init__(self, config: "Config") -> None:
        self._config = config

    def excludes(self, issue: "Issue") -> bool:
        """Whether this engine's configured scope excludes ``issue`` outright."""
        return not evaluate_issue_scope(
            self._config, issue, include_issue_number_filter=True
        ).in_scope


def outside_single_issue_scope(config: "Config", issue: "Issue") -> bool:
    """Whether the engine's ``--issue`` filter alone excludes this issue.

    The narrowest scope gate, split out so callers that must ask ONLY the
    single-issue question share this one definition with the composite
    :func:`evaluate_issue_scope`. Deliberately says nothing about labels,
    milestone, or open state: a caller that holds locally recorded state about
    an issue may need to act on it even when GitHub's current snapshot has
    drifted out of those gates, while ``--issue N`` still binds absolutely.
    """
    target_number = config.filtering.issue
    return bool(target_number) and issue.number != target_number


def issue_scope_skip_detail(
    config: "Config",
    issue: "Issue",
    *,
    require_open: bool = True,
    include_milestone_filter: bool = True,
    include_issue_number_filter: bool = False,
) -> str | None:
    """Return scope skip detail for callers that only need the reason text."""
    decision = evaluate_issue_scope(
        config,
        issue,
        require_open=require_open,
        include_milestone_filter=include_milestone_filter,
        include_issue_number_filter=include_issue_number_filter,
    )
    return decision.detail


def _outside(code: str, detail: str) -> IssueScopeDecision:
    return IssueScopeDecision(in_scope=False, code=code, detail=detail)
