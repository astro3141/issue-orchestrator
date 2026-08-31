"""``ReviewExchangeRunner`` implementation backed by the persistent-session runner.

Wraps the existing :func:`run_persistent_session_exchange` plus the
sibling-reviewer-worktree helpers so callers in ``control/`` can depend
on the :class:`ReviewExchangeRunner` port instead of reaching into the
execution layer.

The reviewer-worktree lifecycle (create lazily on first exchange,
fast-forward at the start of every reviewer round, remove when the
    pair is released at issue completion / reset / shutdown) lives
    entirely inside this implementation plus the registry's ``on_release``
    hook. The caller allocates the typed exchange run through
    ``SessionOutput`` and passes it in; this runner never resolves run
    directories by name.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..domain.execution_identity import (
    AgentExecutionIdentity,
    ExecutionPrincipal,
    ExecutionProvenance,
    ExecutionRole,
)
from ..domain.issue_key import IssueKey
from ..domain.models import AgentConfig
from ..domain.review_exchange import ReviewExchangeOutcome
from ..domain.review_exchange_coder_principal import ReviewExchangeCoderPrincipal
from ..domain.review_exchange_rework import ReviewExchangeRework
from ..domain.review_exchange_run import ReviewExchangeRun
from ..domain.runtime_config import RuntimeConfigReference
from ..events import EventContext
from ..ports.event_sink import EventSink
from ..ports.candidate_review_verdicts import CandidateReviewVerdictStore
from ..ports.execution_identity_store import CandidateExecutionIdentityStore
from .candidate_execution_identity import CandidateExecutionIdentityRecorder
from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate
from ..control.tech_lead_completion_validation import (
    UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR,
)
from ..ports.tech_lead_completion_validation import TechLeadCompletionValidator
from ..ports.coder_prompt import (
    CoderPromptAddendumProvider,
    NO_CODER_PROMPT_ADDENDUM,
)
from ..domain.coder_prompt import CoderPromptAddendumUnavailable
from ..domain.session_key import TaskKind
from .persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
)
from ..ports.session_output import SessionOutput
from .persistent_session_exchange import (
    agent_provider,
    launch_config,
    review_exchange_supervisor_timeout_seconds,
    run_persistent_session_exchange,
)
from ..adapters.worktree.api import CodexReviewCommandGuardInstaller
from ..ports.review_command_guard import ReviewCommandGuardInstaller
from ..ports.turn_mailbox import TurnMailbox
from .review_exchange_response_channel import (
    ResponseChannel,
    ReviewExchangeResponseChannels,
)
from .reviewer_worktree import (
    create_reviewer_worktree,
    resolve_current_branch,
)


def persistent_pair_root_for_worktree(coder_worktree: Path) -> Path:
    """Return the attempt-scoped persistent-pair storage root.

    The repository engine owns the live pair registry, but durable pair
    artifacts are attempt-scoped: deleting the issue worktree must delete
    validation and recording state for that attempt. Keeping the root under
    the coder worktree preserves stable paths for live PTYs while making
    reset-from-scratch a real storage boundary.
    """
    return coder_worktree / ".issue-orchestrator" / "persistent-pairs"


_CODEX_LOOPBACK_BLOCKING_SANDBOXES = frozenset({"read-only", "workspace-write"})


def _codex_loopback_callbacks_blocked(agent: AgentConfig) -> bool:
    provider = (agent.provider or agent.ai_system or "").lower()
    if provider != "codex":
        return False
    approval_mode = str(agent.provider_args.get("approval_mode", "full-auto"))
    if approval_mode == "yolo":
        return False
    sandbox_value = agent.provider_args.get("sandbox")
    sandbox = (
        "workspace-write"
        if sandbox_value is None and approval_mode == "full-auto"
        else str(sandbox_value or "")
    )
    return sandbox in _CODEX_LOOPBACK_BLOCKING_SANDBOXES


def response_channel_for_agent(agent: AgentConfig) -> ResponseChannel:
    """Select the verdict transport supported by an exchange role's sandbox."""
    if _codex_loopback_callbacks_blocked(agent):
        return "file"
    return "mailbox"


class PersistentReviewExchangeRunner:
    """Persistent-session implementation of :class:`ReviewExchangeRunner`.

    Constructed once at the composition root with the orchestrator's
    :class:`SessionOutput` and issue-scoped pair registry. Reused for
    every exchange. Pair filesystem state is resolved per coder worktree
    at run time so worktree teardown clears attempt-scoped artifacts.
    """

    def __init__(
        self,
        session_output: SessionOutput,
        pair_registry: InMemoryPersistentExchangePairRegistry,
        execution_identity_store: CandidateExecutionIdentityStore,
        review_verdict_store: CandidateReviewVerdictStore,
        *,
        turn_mailbox: "TurnMailbox | None" = None,
        coder_prompt_addendum: CoderPromptAddendumProvider = NO_CODER_PROMPT_ADDENDUM,
        tech_lead_completion_validator: TechLeadCompletionValidator = (
            UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR
        ),
        review_command_guard: ReviewCommandGuardInstaller | None = None,
    ) -> None:
        self._session_output = session_output
        self._pair_registry = pair_registry
        # Required, not optional: this runner is the only place the
        # orchestrator observes both execution identities and the candidate
        # they ran against, so a deployment that forgot to wire the store
        # would produce a review no Foundation gate could ever admit — and it
        # would do so silently. Fail at construction instead (#34).
        self._execution_identity_store = execution_identity_store
        # Required for the same reason and by the same argument (#345): this
        # runner is where the orchestrator concludes what the reviewer decided
        # about the presented commit, and the exchange-directory copy of that
        # decision dies with the coder worktree. A deployment that forgot to
        # wire the durable half would silently produce reviews no
        # exact-candidate gate could ever admit.
        self._review_verdict_store = review_verdict_store
        self._turn_mailbox = turn_mailbox
        self._coder_prompt_addendum = coder_prompt_addendum
        # The #385 trusted owner, reused for the exchange's Tech Lead lane
        # (#388): it executes that lane's mandatory per-round validation
        # outside the model sandbox and files it against the exact candidate.
        # Defaulted rather than required because the Actor lane never asks it
        # anything, and the default refuses every verdict — so a deployment
        # that forgot to wire the real one loses a Tech Lead round instead of
        # passing it unvalidated.
        self._tech_lead_completion_validator = tech_lead_completion_validator
        # The reviewer worktree's barrier (#396). Defaulted to the real,
        # CLI-backed installer rather than to a permissive stand-in: the
        # default has to be the strict one, so a deployment that never names it
        # still gets the guard and still fails an exchange closed when the
        # guard does not take. It is injectable because verifying what the
        # policy refuses needs the provider CLI, while binding a guard to the
        # worktree does not — see ``ports/review_command_guard``.
        self._review_command_guard: ReviewCommandGuardInstaller = (
            CodexReviewCommandGuardInstaller()
            if review_command_guard is None
            else review_command_guard
        )

    def _execution_identity_recorder(
        self,
        *,
        issue_key: IssueKey,
        coder_label: str,
        coder_agent: AgentConfig,
        reviewer_label: str,
        reviewer_agent: AgentConfig,
    ) -> CandidateExecutionIdentityRecorder:
        """Both roles' principals and provenance as the orchestrator observed them.

        The principal is the label the orchestrator routed the role by — the
        authority half, and the only half I2c compares. The provenance is read
        off :func:`launch_config` — the *same* derivation
        ``_derive_bootstrap_agent`` spawns, not the configured agent it is
        derived from. That distinction is the whole point: an ``ai_system``-only
        agent's provider resolves at launch, and its model answer follows the
        resolved provider, so reading the pre-derivation config would record a
        model the launcher never passed (``sonnet`` for a codex run). Nothing
        here can be reached by an agent's output.
        """
        coder_launch = launch_config(coder_agent)
        reviewer_launch = launch_config(reviewer_agent)
        return CandidateExecutionIdentityRecorder(
            store=self._execution_identity_store,
            review_verdicts=self._review_verdict_store,
            issue_key=issue_key,
            actor=AgentExecutionIdentity(
                role=ExecutionRole.ACTOR,
                principal=ExecutionPrincipal(agent_label=coder_label),
                provenance=ExecutionProvenance(
                    provider=agent_provider(coder_launch).value,
                    model=coder_launch.resolved_model(),
                ),
            ),
            reviewer=AgentExecutionIdentity(
                role=ExecutionRole.REVIEWER,
                principal=ExecutionPrincipal(agent_label=reviewer_label),
                provenance=ExecutionProvenance(
                    provider=agent_provider(reviewer_launch).value,
                    model=reviewer_launch.resolved_model(),
                ),
            ),
        )

    def job_timeout_seconds(
        self,
        *,
        coder_agent: AgentConfig,
        reviewer_agent: AgentConfig,
        max_rounds: int,
    ) -> float | None:
        coder_timeout = coder_agent.timeout_minutes * 60
        reviewer_timeout = reviewer_agent.timeout_minutes * 60
        if coder_timeout <= 0 or reviewer_timeout <= 0:
            return None
        return review_exchange_supervisor_timeout_seconds(
            coder_timeout_seconds=coder_timeout,
            reviewer_timeout_seconds=reviewer_timeout,
            max_rounds=max_rounds,
        )

    def run(  # noqa: PLR0913
        self,
        *,
        exchange_run: ReviewExchangeRun,
        coder_worktree: Path,
        issue_key: IssueKey,
        issue_number: int,
        issue_title: str,
        coder_label: str,
        reviewer_label: str,
        coder_agent: AgentConfig,
        reviewer_agent: AgentConfig,
        runtime_config: RuntimeConfigReference,
        max_rounds: int,
        max_no_progress: int,
        require_validation: bool,
        # Required, as the port requires it: this implementation is the last
        # place the caller's answer could be replaced by an inference (#180).
        rework: ReviewExchangeRework,
        # Required, as the port requires it: the coder SIDE is a position in
        # the protocol and the principal is the authority sitting in it, and
        # this implementation is the last place a caller's answer could be
        # replaced by an inference (#388).
        coder_principal: ReviewExchangeCoderPrincipal,
        nit_policy: str = "surface",
        initial_validation_record_path: Path | None = None,
        approval_gate: ReviewExchangeApprovalGate | None = None,
        web_port: int | None = None,
        events: EventSink | None = None,
        event_context: EventContext | None = None,
    ) -> ReviewExchangeOutcome:
        coder_branch = resolve_current_branch(coder_worktree)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        response_channels = ReviewExchangeResponseChannels.file_only()
        if self._turn_mailbox is not None:
            response_channels = ReviewExchangeResponseChannels(
                coder=response_channel_for_agent(coder_agent),
                reviewer=response_channel_for_agent(reviewer_agent),
            )

        def _make_reviewer_worktree() -> Path:
            # Invoked at most once per pair — only on cache miss
            # inside ``run_persistent_session_exchange``'s spawn
            # closure. Subsequent exchanges reuse the cached pair's
            # ``reviewer_worktree_path`` and the inner round-loop
            # fast-forwards it before each reviewer round.
            #
            # The provider is read off ``launch_config`` — the derivation the
            # exchange actually spawns, not the configured agent it comes
            # from — for the same reason the execution-identity record is:
            # the worktree's command guard must be installed for the provider
            # that will really sit in it, or it is not a guard.
            wt = create_reviewer_worktree(
                coder_worktree=coder_worktree,
                coder_branch=coder_branch,
                timestamp=timestamp,
                reviewer_provider=agent_provider(launch_config(reviewer_agent)),
                guard_installer=self._review_command_guard,
            )
            return wt.path

        prepared_coder_prompt = self._coder_prompt_addendum.prepare(
            task=TaskKind.REWORK,
            agent_label=coder_label,
        )
        if isinstance(prepared_coder_prompt, CoderPromptAddendumUnavailable):
            raise RuntimeError(
                "Required coder prompt addendum unavailable: "
                f"{prepared_coder_prompt.reason}"
            )

        return run_persistent_session_exchange(
            exchange_run=exchange_run,
            session_output=self._session_output,
            pair_registry=self._pair_registry,
            persistent_pair_root=persistent_pair_root_for_worktree(coder_worktree),
            coder_worktree_path=coder_worktree,
            reviewer_worktree_factory=_make_reviewer_worktree,
            coder_branch=coder_branch,
            issue_number=issue_number,
            issue_title=issue_title,
            coder_label=coder_label,
            reviewer_label=reviewer_label,
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            runtime_config=runtime_config,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            rework=rework,
            coder_principal=coder_principal,
            tech_lead_completion_validator=self._tech_lead_completion_validator,
            nit_policy=nit_policy,
            initial_validation_record_path=initial_validation_record_path,
            approval_gate=approval_gate,
            web_port=web_port,
            events=events,
            event_context=event_context,
            turn_mailbox=self._turn_mailbox,
            response_channels=response_channels,
            coder_prompt_addendum=prepared_coder_prompt.addendum,
            execution_identities=self._execution_identity_recorder(
                issue_key=issue_key,
                coder_label=coder_label,
                coder_agent=coder_agent,
                reviewer_label=reviewer_label,
                reviewer_agent=reviewer_agent,
            ),
        )
