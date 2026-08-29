"""Behaviour-complete termination of a tech-lead session (#6824 R7).

Extracted from the orchestrator facade, which coordinates rather than executes:
the effects below and — more importantly — the rule that each is attempted
INDEPENDENTLY are policy, and policy belongs beside the typed outcome it
produces rather than inside a facade method.

``kill_session`` only stops the terminal, and the one-shot driver that calls
this runs NO further tick afterwards — so a recorded cleanup fact would never be
applied. The termination is therefore self-contained, mirroring the outcomes
normal completion produces: remove the session state machine, stop the terminal,
reconcile the session out of ``active_sessions``, release BOTH coordination
holds (the per-issue claim and the repository-wide tech-lead run), and
FORCE-remove the disposable scratch worktree.

Both coordination layers, deliberately (#6994 round 2 F10/A7). The per-issue
claim says who may write to an issue; the run hold says which whole-repository
or focused tech-lead run is executing. Releasing only the first leaves every
conflicting tech-lead run blocked until the lease expires, with no later tick to
notice — so the run release is its own independent effect with its own field in
the typed outcome.

A failure of one effect never aborts the others, and the result is a typed
:class:`~.tech_lead_trigger.TechLeadTerminationOutcome` — the SOLE owner of a
failed one-shot cleanup. On a scratch-worktree removal failure the outcome
carries the exact ``leaked_worktree`` path so the caller can require explicit
operator removal; there is no second, tick-based retry mechanism to defer to.

Because this owner PERFORMS a reap, it also owns the rule that a tech_lead
run's staged evidence is durable before its checkout is destroyed (#360). The
completion handoff captures before the cleanup fact it files turns into a
removal; termination captures before the removal it performs itself. Neither
seam delegates the rule to the other, so a run reaped here — the drive-loop
timeout, and the lost-ownership stop — is not the one case where the evidence
silently goes away.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional, Protocol

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..infra.config import Config
    from .tech_lead_trigger import TechLeadTerminationOutcome

logger = logging.getLogger(__name__)


class TechLeadTerminationHost(Protocol):
    """The facade surface a termination drives.

    Structural, so this control owner never imports the infra facade — and so a
    test can supply exactly the collaborators the effects touch.
    """

    @property
    def state(self) -> "OrchestratorState": ...

    @property
    def config(self) -> "Config": ...

    @property
    def deps(self) -> object: ...

    def kill_session(self, name: str) -> None: ...


def terminate_tech_lead_session(
    host: TechLeadTerminationHost, session: "Session"
) -> "TechLeadTerminationOutcome":
    """Stop the session and clean up after it, reporting what actually worked."""
    from .tech_lead_trigger import TechLeadTerminationOutcome

    number = session.issue.number
    attempt = _effect_runner(number)
    deps = host.deps

    smm = getattr(deps, "state_machine_manager", None)
    machine_removed = attempt(
        _void(lambda: smm.remove_session_machine(session.terminal_id) if smm else None),
        "remove state machine",
    )
    terminal_stopped = attempt(
        _void(lambda: host.kill_session(session.terminal_id)), "stop terminal"
    )
    # #360: preserve the run's staged evidence here, after the terminal is
    # stopped (so the agent is no longer writing into the tree) and before ANY
    # effect that can take the tree away. Unconditionally, not only when the
    # checkout is disposable: a non-scratch worktree dropped from
    # ``active_sessions`` below is force-removed by orphan recovery on a later
    # tick, so "not disposable" only means the loss is delayed.
    #
    # Deliberately NOT one of the outcome's fields. Those report leaks an
    # operator must clean up by hand; a capture reports itself in full (ERROR
    # log, receipt at the keyed destination, ``tech_lead.evidence_captured``
    # with ``preserved``), and a run terminated before it staged anything has
    # nothing to preserve — which is an ordinary outcome, not an unclean
    # teardown. It is still run through ``attempt`` so that it can never abort
    # the coordination releases that follow.
    attempt(
        _void(lambda: _capture_evidence(host, session)), "capture staged evidence"
    )
    host.state.drop_active_session(session.terminal_id)  # pure in-memory owner op

    claims = getattr(deps, "claim_manager", None)
    lease_id = getattr(session, "lease_id", None)
    claim_released = attempt(
        _void(
            lambda: claims.release_claim(number, lease_id)
            if (claims and lease_id)
            else None
        ),
        "release claim",
    )
    # NOT "did it raise?": the run-ownership owner reports an unreachable
    # coordination store as a typed refusal, so the verdict is its own.
    run_released = attempt(
        lambda: _release_run_hold(deps, session), "release the tech-lead run"
    )

    worktrees = getattr(deps, "worktree_manager", None)
    disposable = bool(
        getattr(session, "scratch_worktree", False) and session.worktree_path
    )
    worktree_removed = attempt(
        _void(
            lambda: worktrees.remove_checkout_and_branch(
                session.worktree_path,
                force=True,
            )
            if (disposable and worktrees)
            else None
        ),
        "remove scratch worktree",
    )
    return TechLeadTerminationOutcome(
        terminal_stopped=terminal_stopped,
        machine_removed=machine_removed,
        claim_released=claim_released,
        run_released=run_released,
        worktree_removed=worktree_removed,
        # A failed removal surfaces the EXACT leaked path for explicit operator
        # action before exit — this is the single cleanup-failure owner.
        leaked_worktree=(
            str(session.worktree_path)
            if (disposable and not worktree_removed)
            else None
        ),
    )


def _capture_evidence(host: TechLeadTerminationHost, session: "Session") -> None:
    """Copy the run's staged evidence out of the checkout about to be reaped.

    The same owner the completion handoff uses, asked the same identity
    question, so a run captured on one path is exactly a run captured on the
    other; a session no tech_lead owner governs costs one identity check and
    nothing else. The capture reports its own success or failure, so there is
    no verdict to return here.
    """
    from .tech_lead_evidence_capture import capture_tech_lead_session_evidence

    capture_tech_lead_session_evidence(
        config=host.config,
        session=session,
        events=getattr(host.deps, "events", None),
    )


def _release_run_hold(deps: object, session: "Session") -> bool:
    """Hand the terminated session's repository-wide run hold back.

    The scope is derived from the SESSION's own launch stamp, so a global review
    releases ``global:*`` and a focused investigation releases ``issue:N`` — the
    same identity the launch authority took. A session carrying no stamp holds
    no run, and that is a genuine success rather than a skipped effect.

    A session that DOES hold a run with no ownership owner wired is a
    composition error, and it fails loudly: reporting it as a clean release
    would claim the repository-wide hold is gone when nothing ever looked.
    """
    from .tech_lead_run_scopes import scope_of_session

    scope = scope_of_session(session)
    if scope is None:
        return True
    ownership = getattr(deps, "run_ownership", None)
    if ownership is None:
        raise RuntimeError(
            f"no tech-lead run ownership is wired, so run {scope.run_key} cannot"
            " be handed back"
        )
    return ownership.end_run(scope.run_key).released


def _void(effect: Callable[[], object]) -> Callable[[], Optional[bool]]:
    """An effect that can only signal failure by RAISING.

    Its return value is discarded explicitly, so a collaborator that happens to
    return something falsy can never be misread as a failed effect.
    """

    def run() -> Optional[bool]:
        effect()
        return None

    return run


def _effect_runner(
    issue_number: int,
) -> Callable[[Callable[[], Optional[bool]], str], bool]:
    """Attempt one effect, reporting success without letting it stop the rest.

    An effect that can fail WITHOUT raising returns its own boolean verdict, and
    that verdict is reported verbatim; an effect wrapped in :func:`_void`
    returns ``None`` and succeeds by not raising. Both shapes are needed because
    the two coordination layers report failure differently — the run ledger
    returns a typed refusal rather than raising (#6994 round 3 F12).
    """

    def attempt(effect: Callable[[], Optional[bool]], what: str) -> bool:
        try:
            verdict = effect()
            return True if verdict is None else verdict
        except Exception:
            logger.warning(
                "[TECH_LEAD] Failed to %s for issue #%d on timeout terminate",
                what,
                issue_number,
                exc_info=True,
            )
            return False

    return attempt


__all__ = ["TechLeadTerminationHost", "terminate_tech_lead_session"]
