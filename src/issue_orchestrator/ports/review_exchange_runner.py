"""Port for executing one persistent-session review exchange.

The review-exchange runner owns the full lifecycle of one coder↔reviewer
exchange: it creates the sibling reviewer worktree, drives the round
loop against the persistent agent sessions, fast-forwards the reviewer
between rounds, and reclaims the worktree on exit (success or failure).

Hiding the runner behind a port lets ``control/`` depend on a behavior
contract instead of reaching across the layer boundary into
``execution/``. The previous cutover used ``importlib.import_module``
to keep the import-linter contracts honest at the static-graph layer
(see #6161); injecting this port makes the indirection unnecessary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey
    from ..domain.review_exchange_rework import ReviewExchangeRework
    from ..domain.models import AgentConfig
    from ..domain.review_exchange import ReviewExchangeOutcome
    from ..domain.review_exchange_run import ReviewExchangeRun
    from ..domain.runtime_config import RuntimeConfigReference
    from ..events import EventContext
    from .event_sink import EventSink
    from .review_exchange_approval_gate import ReviewExchangeApprovalGate


class ReviewExchangeRunner(Protocol):
    """Run a single coder↔reviewer review exchange.

    Implementations own the reviewer-worktree lifecycle and the
    round loop. The caller hands over a coder worktree and the
    agent configs; the runner returns the structured outcome.

    ``issue_key`` is the stable work-item identity the run's durable records
    are filed under. It is the caller's to supply rather than the runner's to
    reconstruct: identity derivation is a control-layer concern, and a runner
    inventing its own spelling of it would file a candidate's admission
    evidence (#34) under a key nothing else uses.

    ``rework`` is the caller's answer to "who moves the candidate when this
    review asks for changes" (#180). It is the caller's because only the caller
    knows whether it owns the coder this exchange would hand feedback to; a
    runner cannot infer it, and inferring it wrongly is how a control
    continuation's exchange reworked a candidate its owner was still holding.
    It therefore carries no default here: a port that supplied the inference it
    says cannot be made would answer for a caller that forgot to.
    """

    def run(
        self,
        *,
        exchange_run: "ReviewExchangeRun",
        coder_worktree: Path,
        issue_key: "IssueKey",
        issue_number: int,
        issue_title: str,
        coder_label: str,
        reviewer_label: str,
        coder_agent: "AgentConfig",
        reviewer_agent: "AgentConfig",
        runtime_config: "RuntimeConfigReference",
        max_rounds: int,
        max_no_progress: int,
        require_validation: bool,
        rework: "ReviewExchangeRework",
        nit_policy: str = "surface",
        initial_validation_record_path: Path | None = None,
        approval_gate: "ReviewExchangeApprovalGate | None" = None,
        web_port: int | None = None,
        events: "EventSink | None" = None,
        event_context: "EventContext | None" = None,
    ) -> "ReviewExchangeOutcome":
        ...

    def job_timeout_seconds(
        self,
        *,
        coder_agent: "AgentConfig",
        reviewer_agent: "AgentConfig",
        max_rounds: int,
    ) -> float | None:
        """Return the supervisor wall-clock budget for one background run.

        The runner owns round-loop retry semantics, so it also owns the
        derived outer deadline used by the background supervisor. Returning
        ``None`` means the runner cannot derive a meaningful budget from the
        supplied agent configuration.
        """
        ...


class NullReviewExchangeRunner:
    """Default :class:`ReviewExchangeRunner` for tests that don't exercise it.

    Production must always inject :class:`PersistentReviewExchangeRunner`
    via the composition root; this default exists so the many unit/
    integration tests that construct :class:`CompletionProcessor`
    without ever entering the review-exchange path don't have to
    invent a fake runner. Calling :meth:`run` raises so a misuse from
    production would surface immediately instead of silently no-oping.
    """

    def run(self, **_: Any) -> "ReviewExchangeOutcome":
        raise RuntimeError(
            "NullReviewExchangeRunner.run() invoked — production must inject "
            "a real ReviewExchangeRunner (e.g. PersistentReviewExchangeRunner) "
            "at the composition root."
        )

    def job_timeout_seconds(self, **_: Any) -> float | None:
        return None
