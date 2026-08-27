"""Tech Lead session completion planning (ADR-0031).

What a tech_lead session that LANDED produces: launch-authority verification,
assignment-driven label policy, and decision-artifact processing. Extracted
from ``completion_action_planner`` so the tech_lead owner boundary
(``tech_lead_session_policy`` / ``TechLeadLaunchAuthority`` on the launch side,
this module on the completion side) lives in one cohesive seam.

The dead-or-rejected half lives in ``tech_lead_terminal_effects``: what a
FAILED/TIMED_OUT session, or a COMPLETED session whose decision the contract
refused, does to its anchor and to its subject. It shares this module's trusted
reads (``resolve_launch_authority_for_session``, ``manifest_label_actions``,
``split_tech_lead_decision_error``) so the two halves cannot disagree about
what a run was.

Policy summary:

* The ONLY trusted launch scope is the orchestrator-owned
  :class:`TechLeadLaunchAuthority` recorded at launch (outside the
  agent-writable worktree). The worktree copies (tech-lead-assignment.json,
  manifest.json) are the agent's reading material; a missing authority
  record, or worktree copies that no longer match it, is a critical failure
  (#6761 re-review finding 1) — never a fail-safe success.
* Only batch-review sessions label PRs (the authority manifest set they were
  launched to audit); failure investigations and health reviews never touch
  manifest labels (#6768 B4 / ADR-0031 §4).
* Every COMPLETED tech_lead session (any flavor) must produce a valid
  decision artifact pair — a missing/invalid pair is a contract violation.
  The authoritative classification runs in the completion processing path's
  PRE-ACTION policy phase (``admit_tech_lead_completion``, called by
  ``completion_processor`` before any requested push/PR/comment executes —
  #6769 finding 1) so a rejected completion produces zero GitHub effects and
  the session's history outcome is FAILED, not a quiet success; the action
  planner re-reads the same validation for its planning effects (#6761
  finding 3).
* A completion that did NOT land has no decision pair to judge, and is never
  asked to invent one — but the same pre-action seam still governs what it may
  DO. ``settle_tech_lead_completion`` applies the zero-code publication lane
  (#202) and the subject-recovery answer (#182/#136) to EVERY outcome, from the
  trusted launch authority alone, because a BLOCKED planning run reaching the
  generic action executor unshaped pushes a branch it never wrote and blocks
  the very issue it was sent to prepare (#257).
* What makes a decision ADMISSIBLE — role action-kind capability (#133),
  target scope (#6761 re-review F2, #6764 rr F1, #6780), the failure
  investigation's diagnosis duty (#6761 F2), and protected-label truth
  (#6761 F4) — belongs to ``tech_lead_decision_contract``, which this module
  calls through ``load_validated_tech_lead_pair``. Effects planned here never
  re-decide admissibility.
* Health reviews close their anchor issue on success: the anchor is a
  walk-the-floor log entry, closed when the review lands. A rejected or
  missing pair leaves the anchor open for operator visibility; a
  FAILED/TIMED_OUT health session closes it through the terminal-failure
  path (like batch, no manifest labels) so the next interval re-fires
  instead of deduping against a dead anchor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..domain.models import (
    CompletionOutcome,
    RequestedAction,
    Session,
)
from ..domain.board_snapshot import BOARD_SNAPSHOT_FILENAME, BoardSnapshot
from ..domain.tech_lead_manifest import TechLeadManifest
from ..domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
    read_run_assignment,
)
from .actions import (
    Action,
    AddLabelAction,
    CloseIssueAction,
)
from .completion_types import (
    CodeCandidateSettlement,
    ERROR_PREFIX_TECH_LEAD_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_DECISION,
)
from .label_manager import LabelManager
from .publish_recovery import is_publish_failure
from .proposal_dedup_gate import DuplicateTargetGrant
from .tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
    plan_tech_lead_rejection_action,
)
from .tech_lead_decision_loader import (
    TechLeadArtifactLoadResult,
    TechLeadDecisionLoadFailure,
    load_tech_lead_artifact_pair_for_run,
)
from .subject_recovery_authority import SubjectRecoveryAuthority
from .tech_lead_case_files import build_pattern_ledger
from .tech_lead_decision_contract import validate_decision_for_authority
from .tech_lead_proposals import build_op_ledger
from .tech_lead_session_policy import is_tech_lead_session
from .tech_lead_zero_code import (
    ZeroCodeWorktreeReader,
    settle_zero_code_planning_completion,
)

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .open_issue_corpus import OpenIssueCorpusManager
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)


def read_tech_lead_manifest(run_dir: Path) -> TechLeadManifest | None:
    """Read the agent-visible batch PR manifest copy for a session run.

    UNTRUSTED: this is the worktree copy, used only to detect divergence
    from the launch authority (tamper evidence). Completion effects never
    key off it. Fail-safe: a missing run manifest, key, or manifest file
    yields None (with a warning where content is present but unreadable).
    """
    run_manifest_path = run_dir / "manifest.json"
    if not run_manifest_path.exists():
        return None
    try:
        run_manifest = json.loads(run_manifest_path.read_text())
    except Exception as exc:
        logger.warning(
            "[tech_lead] Failed to read run manifest %s: %s",
            run_manifest_path,
            exc,
            exc_info=True,
        )
        return None
    tech_lead_manifest_path = run_manifest.get("tech_lead_manifest")
    if not tech_lead_manifest_path:
        return None
    manifest_path = Path(tech_lead_manifest_path)
    if not manifest_path.exists():
        logger.warning(
            "[tech_lead] Manifest path in run manifest doesn't exist: %s",
            manifest_path,
        )
        return None
    try:
        return TechLeadManifest.read(manifest_path)
    except Exception as exc:
        logger.warning(
            "[tech_lead] Failed to read manifest from %s: %s",
            manifest_path,
            exc,
            exc_info=True,
        )
        return None


def _health_snapshot_scope_error(
    run_dir: Path, authority: TechLeadLaunchAuthority
) -> str | None:
    """Return snapshot/cohort tamper detail for a health-review authority."""
    snapshot_path = run_dir / "tech-lead-data" / BOARD_SNAPSHOT_FILENAME
    if not snapshot_path.exists():
        return "worktree board-snapshot.json is missing (deleted after launch)"
    try:
        worktree_snapshot = BoardSnapshot.read(snapshot_path)
    except Exception as exc:
        return f"worktree board-snapshot.json is malformed: {exc}"
    worktree_problems = worktree_snapshot.problem_issue_numbers()
    authority_problems = frozenset(authority.problem_issue_numbers)
    if worktree_problems != authority_problems:
        return (
            f"worktree board-snapshot problem set {sorted(worktree_problems)}"
            " does not match the launch authority cohort "
            f"{sorted(authority_problems)}"
        )
    return None


def resolve_tech_lead_launch_authority(
    tech_lead_authority: "TechLeadAuthorityStore",
    *,
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> tuple[TechLeadLaunchAuthority | None, str | None]:
    """Load the orchestrator-owned launch authority and verify worktree copies.

    Returns ``(authority, error_detail)``. ``error_detail`` is None only when
    the authority record exists AND the agent-visible worktree copies still
    mirror it. A missing record, a deleted/malformed/flipped assignment copy,
    or a manifest whose PR set diverges from the recorded set is tamper
    evidence (#6761 re-review finding 1) — callers must fail the session.
    """
    authority = tech_lead_authority.load(run_id=run_id, session_name=session_name)
    if authority is None:
        return None, (
            "no orchestrator launch-authority record for run"
            f" {run_id}/{session_name}; the worktree tech_lead inputs cannot"
            " be trusted"
        )
    try:
        assignment = read_run_assignment(run_dir)
    except ValueError as exc:
        return authority, f"worktree tech-lead-assignment.json is malformed: {exc}"
    if assignment is None:
        return authority, (
            "worktree tech-lead-assignment.json is missing (deleted after launch)"
        )
    if not authority.matches_assignment(assignment):
        return authority, (
            "worktree tech-lead-assignment.json"
            f" (flavor={assignment.flavor.value},"
            f" focus={assignment.focus_issue_number}) does not match the"
            f" launch authority (flavor={authority.flavor.value},"
            f" focus={authority.focus_issue_number})"
        )
    if authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        manifest = read_tech_lead_manifest(run_dir)
        worktree_prs = frozenset(pr.number for pr in manifest.prs) if manifest else frozenset()
        if worktree_prs != frozenset(authority.manifest_pr_numbers):
            return authority, (
                f"worktree manifest PR set {sorted(worktree_prs)} does not"
                " match the launch authority set"
                f" {sorted(authority.manifest_pr_numbers)}"
            )
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        if error := _health_snapshot_scope_error(run_dir, authority):
            return authority, error
    return authority, None


def load_validated_tech_lead_pair(
    run_dir: Path,
    authority: TechLeadLaunchAuthority,
    *,
    config: "Config",
    labels: LabelManager,
) -> TechLeadArtifactLoadResult:
    """Load the artifact pair and apply authority/policy validation.

    The ONE read both completion seams use: the processing path (authoritative
    outcome, finding 3) and the action planner (planning effects). Never raises.
    """
    result = load_tech_lead_artifact_pair_for_run(run_dir)
    if not result.ok or result.decision is None:
        return result
    detail = validate_decision_for_authority(
        result.decision, authority, config=config, labels=labels
    )
    if detail is not None:
        logger.error("Tech Lead decision contract violation in %s: %s", run_dir, detail)
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.CONTRACT_VIOLATION,
            detail=detail,
        )
    return result


@dataclass(frozen=True, slots=True)
class TechLeadCompletionAdmission:
    """Whether a COMPLETED tech_lead session is admitted, and under what.

    Exactly one of the two is set. ``authority`` is the verified launch record
    the run is admitted under, handed back so the seam that just validated it
    is also the seam that hands it on — a caller needing the run's authoritative
    flavor (the zero-code lane, #202) must not re-derive it from a second store
    read taken after the decision it belongs to was judged.
    """

    authority: TechLeadLaunchAuthority | None
    error: str | None

    def __post_init__(self) -> None:
        if (self.authority is None) == (self.error is None):
            raise ValueError(
                "TechLeadCompletionAdmission carries either the verified launch"
                " authority or the reason there is none, never both and never"
                f" neither (authority={self.authority!r}, error={self.error!r})"
            )


def admit_tech_lead_completion(
    config: "Config",
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> TechLeadCompletionAdmission:
    """Authoritative scope + pair validation for a COMPLETED tech_lead session.

    Called from the completion processing path's PRE-ACTION policy phase —
    before the completion record is preserved and before ANY requested action
    executes (#6769 finding 1). A missing/tampered launch authority (#6761
    re-review F1) or a missing/rejected artifact pair (#6761 F3) yields a
    tagged processing error; the processor rejects the completion outright
    (zero push/PR/comment calls) and ``critical_processing_errors``
    classifies the error critical so history records FAILED and the failure
    labeling path fires.
    """
    authority, tamper = resolve_tech_lead_launch_authority(
        tech_lead_authority, run_dir=run_dir, run_id=run_id, session_name=session_name
    )
    if authority is None:
        return TechLeadCompletionAdmission(
            None, f"{ERROR_PREFIX_TECH_LEAD_AUTHORITY}: missing_authority: {tamper}"
        )
    if tamper is not None:
        return TechLeadCompletionAdmission(
            None, f"{ERROR_PREFIX_TECH_LEAD_AUTHORITY}: scope_tampered: {tamper}"
        )
    result = load_validated_tech_lead_pair(
        run_dir, authority, config=config, labels=LabelManager(config)
    )
    if result.ok:
        return TechLeadCompletionAdmission(authority, None)
    failure = result.failure.value if result.failure else "unknown"
    return TechLeadCompletionAdmission(
        None, f"{ERROR_PREFIX_TECH_LEAD_DECISION}: {failure}: {result.detail}"
    )


@dataclass(frozen=True, slots=True)
class TechLeadCompletionLane:
    """What ONE tech_lead completion may still ask the orchestrator to do.

    ``rejection`` is the tagged error when the completion is refused outright;
    the caller must then take zero action. Otherwise ``requested_actions`` is
    what survives this run's own authority — the caller carries it forward in
    place of the untrusted tuple the completion record arrived with — and
    ``zero_code`` / ``detail`` record which lane it settled into, so an operator
    reading the log of a run that kept the publication path sees which fact was
    missing.

    ``detail`` covers BOTH policies, not just publication: a run whose recovery
    requests were refused says so there, because the whole point of #257 is that
    a suppressed request must not be invisible (round 1 review N1). The
    operator-facing sentence for the same suppression is the planned path's job
    — the trace records the decision, the comment explains it — and both come
    from :class:`~.subject_recovery_authority.SubjectRecoveryAuthority`.
    """

    rejection: str | None
    requested_actions: tuple[RequestedAction, ...]
    zero_code: bool
    detail: str

    @property
    def code_candidate(self) -> CodeCandidateSettlement:
        """What this settlement leaves for a downstream code gate (#328).

        The owner that judged ``zero_code`` is the owner that names the fact
        downstream reads, so there is exactly one place the zero-code answer is
        decided and exactly one shape it travels in. A caller that re-derived
        "is this a candidate?" from the run's role, task kind, or requested
        actions would be a second policy able to disagree with this one.
        """
        if self.zero_code:
            return CodeCandidateSettlement.settled_zero_code(self.detail)
        return CodeCandidateSettlement.presented()


def settle_tech_lead_completion(
    config: "Config",
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
    run_dir: Path,
    run_id: str,
    session_name: str,
    outcome: CompletionOutcome,
    requested_actions: tuple[RequestedAction, ...],
    worktree: Path,
    worktree_reader: ZeroCodeWorktreeReader,
) -> TechLeadCompletionLane:
    """The PRE-ACTION policy for a tech_lead completion of ANY outcome (#257).

    Called before the completion record is preserved and before a single
    requested action executes, so what this returns is what the generic action
    executor is allowed to see.

    Two already-settled policies apply to what the run asked for, and this is
    the one seam that applies them:

    * publication intent — a planning run PROVEN to have changed no code offers
      no code candidate, so :mod:`.tech_lead_zero_code` drops the
      ``push_branch``/``create_pr`` the completion CLI hands every completion
      (#202);
    * recovery intent — a role that may not propose a recovery action on its
      own subject may not achieve one by asking the completion path for the
      label either, so the #182 answer removes those requests (#136). The
      completion record is the SEVENTH door onto a subject's recovery state,
      and it goes through
      :meth:`~.subject_recovery_authority.SubjectRecoveryAuthority.completion_request_outcome`
      rather than reading the answer and deciding for itself what a suppression
      means: the owner hands back what was refused alongside what survives, so
      the refusal cannot leave this seam untraced, and every outcome whose
      requests it refuses has a planned twin that says the same thing in the
      operator's comment.

    Only :attr:`CompletionOutcome.COMPLETED` is held to the admission contract
    — trusted launch authority plus a valid decision artifact pair. That gate is
    unchanged and still runs first: suppressing intent for a completion whose
    decision has not been judged would turn a rejection into a settlement.
    Every other outcome reaches this seam with no decision pair to judge, and
    must not be made to invent one merely to have its side effects governed
    (#257): a BLOCKED run is a run that reported it could not proceed, and the
    orchestrator-owned launch authority already says what role it was. Its
    tamper detail is deliberately not fatal here, matching
    :func:`~.tech_lead_terminal_effects.resolve_subject_recovery_authority` —
    the flavor comes from the orchestrator's own record, never from the
    worktree copies the agent could reach.

    An unresolvable launch authority leaves the run ungoverned by both policies
    — the conservative direction the same way round as everywhere else: zero
    code is never assumed for a run whose base is unknown, and the generic
    recovery behaviour stands for a role that cannot be proven bounded.
    """
    if outcome is CompletionOutcome.COMPLETED:
        admission = admit_tech_lead_completion(
            config,
            tech_lead_authority=tech_lead_authority,
            run_dir=run_dir,
            run_id=run_id,
            session_name=session_name,
        )
        if admission.error is not None:
            return TechLeadCompletionLane(
                rejection=admission.error,
                requested_actions=requested_actions,
                zero_code=False,
                detail=admission.error,
            )
        authority = admission.authority
    else:
        authority, _tamper = resolve_tech_lead_launch_authority(
            tech_lead_authority,
            run_dir=run_dir,
            run_id=run_id,
            session_name=session_name,
        )
    if authority is None:
        return TechLeadCompletionLane(
            rejection=None,
            requested_actions=requested_actions,
            zero_code=False,
            detail=(
                "no orchestrator launch-authority record for run"
                f" {run_id}/{session_name}; the run's role is unproven, so its"
                " requested actions stand as the generic path would take them"
            ),
        )
    settlement = settle_zero_code_planning_completion(
        authority=authority,
        requested_actions=requested_actions,
        worktree=worktree,
        worktree_reader=worktree_reader,
    )
    recovery = SubjectRecoveryAuthority.for_flavor(
        authority.flavor
    ).completion_request_outcome(settlement.requested_actions)
    return TechLeadCompletionLane(
        rejection=None,
        requested_actions=recovery.requested_actions,
        zero_code=settlement.zero_code,
        detail=_lane_detail(settlement.detail, recovery.detail),
    )


def _lane_detail(zero_code_detail: str, recovery_detail: str) -> str:
    """The trace an operator reads, covering BOTH policies this seam applied.

    ``detail`` used to explain only the publication lane, so a blocked planning
    run whose ``add_blocked_label`` had just been dropped logged nothing about
    the one thing #257 is for (round 1 review N1). Empty when nothing was
    refused, so a run that kept its requests reads exactly as it did before.
    """
    if not recovery_detail:
        return zero_code_detail
    return f"{zero_code_detail}; {recovery_detail}"


_TECH_LEAD_ERROR_PREFIXES = (ERROR_PREFIX_TECH_LEAD_DECISION, ERROR_PREFIX_TECH_LEAD_AUTHORITY)


def has_tech_lead_decision_errors(processing_errors: list[str] | None) -> bool:
    """True when processing errors include a rejected pair or tampered scope."""
    return any(
        error.startswith(_TECH_LEAD_ERROR_PREFIXES)
        for error in processing_errors or ()
    )


def split_tech_lead_decision_error(processing_errors: list[str]) -> tuple[str, str]:
    """Parse (failure, detail) back out of the recorded processing error."""
    for error in processing_errors:
        for prefix in _TECH_LEAD_ERROR_PREFIXES:
            if not error.startswith(prefix):
                continue
            remainder = error[len(prefix):].lstrip(": ")
            failure, sep, detail = remainder.partition(": ")
            return (failure or "unknown", detail if sep else "")
    return ("unknown", "")


def resolve_launch_authority_for_session(
    tech_lead_authority: "TechLeadAuthorityStore", session: Session
) -> tuple[TechLeadLaunchAuthority | None, str | None]:
    """The session-shaped read of the trusted launch scope.

    Shared with ``tech_lead_terminal_effects``: the landed path and the
    dead-or-rejected path must resolve the SAME record the same way, from the
    session's own run identity, or they would disagree about what a run was.
    """
    return resolve_tech_lead_launch_authority(
        tech_lead_authority,
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )


def discard_tech_lead_authority_after_completion(
    config: "Config",
    tech_lead_authority: "TechLeadAuthorityStore",
    session: Session,
    *,
    processing_errors: list[str] | None,
) -> None:
    """Retention owner (#6769 F3): drop the run's authority row at the end.

    Called from completion finalization for every terminal status. The row
    is keyed by run identity, so a relaunch (new run) records a fresh row at
    launch, and a completed/failed/rejected run leaves nothing behind. Runs
    AFTER completion actions are planned — every authority read happens
    during planning.

    Exception: a publish-stage failure (push/create_pr/publish_blocked)
    records Retry-Publish locators, and the retry re-enters
    ``CompletionProcessor.process`` for this same run — which re-validates
    the launch authority. The row is retained then;
    ``PublishRecoveryService`` discards it at the retry's own terminal
    (success finalization or issue abandonment).

    A storm anchor's cohort row (#6780) is discarded on the same terminal.
    It is keyed by the ANCHOR issue rather than run identity, because it
    outlives any single run: it is recorded at anchor creation, before a run
    exists, and rehydrates the pending review after a restart. Its readers
    intersect it with live pending/active tech_lead work, so dropping it here is
    what releases the cohort's held run artifacts for cleanup.
    """
    if not is_tech_lead_session(config.tech_lead_review_agent, session.issue.agent_type):
        return
    if is_publish_failure(processing_errors):
        return
    tech_lead_authority.discard(
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )
    tech_lead_authority.discard_storm_cohort(anchor_issue_number=session.issue.number)


def manifest_label_actions(
    config: "Config",
    authority: TechLeadLaunchAuthority,
    expected: "ExpectedState",
    *,
    success: bool,
) -> list[Action]:
    """Label the AUTHORITY manifest PRs tech-lead-reviewed/-failed.

    The PR set comes exclusively from the launch authority record — a
    tampered worktree manifest with substituted PR numbers never receives
    labels (#6761 re-review finding 1).
    """
    if not authority.manifest_pr_numbers:
        return []
    if success:
        tech_lead_label = config.tech_lead_reviewed_label or "tech-lead-reviewed"
        reason = "Tech Lead completed successfully"
    else:
        tech_lead_label = config.tech_lead_failed_label or "tech-lead-failed"
        reason = "Tech Lead session failed"
    logger.info(
        "[tech_lead] Adding '%s' label to %d PRs",
        tech_lead_label,
        len(authority.manifest_pr_numbers),
    )
    return [
        AddLabelAction(
            issue_number=pr_number,
            label=tech_lead_label,
            reason=reason,
            expected=expected,
        )
        for pr_number in authority.manifest_pr_numbers
    ]


def generate_tech_lead_completion_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    completed_ok: bool,
    labels: LabelManager,
    tech_lead_authority: "TechLeadAuthorityStore",
    open_issue_corpus: "OpenIssueCorpusManager",
    active_session_run_id: "Callable[[int], str | None]",
) -> list[Action]:
    """Plan all completion effects for a tech_lead session (see module docstring).

    Pure planning — no GitHub reads. ``tech_lead.milestone_strategy.explicit``
    travels as intent on :class:`CreateTechLeadIssueAction` and is resolved at
    the create-issue execution boundary (#6769 finding 4), so a shadow-mode
    ``create_issue`` proposal plans zero API calls.
    """
    actions: list[Action] = []

    if not is_tech_lead_session(config.tech_lead_review_agent, session.issue.agent_type):
        return actions

    authority, tamper = resolve_launch_authority_for_session(
        tech_lead_authority, session
    )
    if authority is None or tamper is not None:
        # Belt-and-braces: the processing path classifies this critical
        # BEFORE status recording, so completions normally take the failure
        # routing instead. Never plan success effects from untrusted scope.
        failure = "missing_authority" if authority is None else "scope_tampered"
        detail = tamper or "no launch authority recorded"
        logger.error(
            "[tech_lead] Launch authority rejected for issue #%d (%s): %s",
            session.issue.number,
            failure,
            detail,
        )
        actions.append(
            plan_tech_lead_rejection_action(
                anchor_issue_number=session.issue.number,
                failure=failure,
                detail=detail,
            )
        )
        return actions

    load_result = (
        load_validated_tech_lead_pair(
            session.run_dir, authority, config=config, labels=labels
        )
        if completed_ok
        else None
    )
    succeeded = load_result is not None and load_result.ok

    if authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        actions.extend(
            manifest_label_actions(config, authority, expected, success=succeeded)
        )

    if load_result is None:
        return actions
    if load_result.decision is not None:
        # The op ledger (one open gated proposal per (op, target), #6778)
        # and the pattern ledger (one case file per signature, #6781) come
        # from the same injected authority store that owns launch scope:
        # both reads are local, so planning needs no GitHub call.
        actions.extend(
            plan_tech_lead_decision_actions(
                load_result.decision,
                config,
                labels,
                anchor_issue=session.issue,
                expected=expected,
                # The ORCHESTRATOR-OWNED role, not the agent-writable assignment
                # copy: it decides whether a create_issue may project scheduling
                # (#332). Planning files an unscheduled, Human-gated proposal.
                flavor=authority.flavor,
                op_ledger=build_op_ledger(tech_lead_authority.list_ops()),
                pattern_ledger=build_pattern_ledger(
                    tech_lead_authority.list_pattern_evidence()
                ),
                source_run_id=session.run_assets.run_id,
                source_session_name=session.run_assets.session_name,
                observed_at=session.run_assets.started_at,
                active_session_run_id=active_session_run_id,
                dedup_corpus=open_issue_corpus.load(),
                dedup_grant=DuplicateTargetGrant.of(authority.allowed_targets()),
            )
        )
    else:
        # Belt-and-braces: the processing path (finding 3) should already have
        # classified this session FAILED before the planner sees it; still
        # surface the rejection when a rejected pair reaches this seam.
        failure = load_result.failure.value if load_result.failure else "unknown"
        logger.warning(
            "[tech_lead] Decision artifact rejected for issue #%d (%s): %s",
            session.issue.number,
            failure,
            load_result.detail,
        )
        actions.append(
            plan_tech_lead_rejection_action(
                anchor_issue_number=session.issue.number,
                failure=failure,
                detail=load_result.detail,
            )
        )

    if succeeded and authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        # Terminal transition (#6768 round 4): the open+agent-labeled tracking
        # issue is what startup recovery requeues and what
        # _find_existing_tech_lead_anchor_issues treats as the active batch.
        # Ordered last so a mid-apply crash leaves the batch open and
        # re-auditable. No comment: tech_lead prompts promise the orchestrator
        # posts none here.
        actions.append(
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Batch tech_lead review completed - closing tracking issue",
                expected=expected,
            )
        )
    if succeeded and authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        # The anchor issue is a walk-the-floor log entry (ADR-0031 §4): a
        # landed review closes it (same terminal ordering rationale as batch;
        # no manifest labels exist for this flavor). Rejected/missing pairs
        # take the rejection surface instead and leave the anchor open.
        actions.append(
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Health review completed with a valid decision pair"
                " - closing anchor issue",
                expected=expected,
            )
        )
    return actions
