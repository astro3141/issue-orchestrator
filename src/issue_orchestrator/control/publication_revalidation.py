"""The bounded, authority-preserving same-SHA revalidation route (#139).

A candidate can fail the publication gate for a reason that is not about the
candidate — the observed case was a live subprocess timing out inside the
suite. The evidence cannot say so: the receipt reads ``verdict: "failed"``,
byte-identical to a real defect. Before this route, the only exits were moving
the SHA, which destroys the very artifact the evidence is about, or editing
durable state by hand, which is forbidden. So exactly one mechanical
re-evaluation is admitted instead of asking a human to read logs.

Everything here is a *bound*, and each bound closes one way the route could
become a way around the gate rather than a way back to it:

* **Identity.** The only input is a durable canonical
  :class:`~..domain.attempt.Attempt` — the record already keyed by exactly one
  ``(issue, commit)``. There is no issue number, URL, title or reconstructed
  key on this surface, and a candidate the store does not hold is refused
  before anything else happens (#40).
* **Contract.** The prior evaluation's own suite, command and profile must
  still be what the publication contract requires, compared with
  :meth:`~..infra.validation_profiles.ValidationGateContract.result_mismatch` —
  the one predicate this codebase uses for "did the contract that ran answer
  for the contract now asked about". A drifted contract is a different
  question, not a retry of the same one.
* **Allowance.** One, ever, per candidate, durable in the attempt sidecar. It
  is a *start* budget: it is spent before any external gate work begins, so an
  interruption between reservation and verdict leaves the allowance spent and
  the prior completed evaluation authoritative. Failing closed is the whole
  point — a refund would make "exactly one" mean "one per crash".
* **Artifact.** The worktree is materialised at the exact SHA through
  :class:`~..ports.candidate_checkout.CandidateCheckouts`, never at a branch,
  and the run scaffold freezes the profile *name taken from the prior receipt*
  rather than resolving one afresh.
* **Environment.** Materialising the source bytes is not enough to run the
  contract against them. The gate's command is the repository's own publication
  suite, and it resolves tools out of the worktree it runs in — a detached
  checkout that has never been provisioned answers ``.venv/bin/pyright: No such
  file or directory`` (#153), which is an environment gap wearing the same
  ``verdict: "failed"`` the route exists to disambiguate. So the checkout is
  made runnable by the operator-pinned recipe every managed worktree already
  uses (:class:`~.worktree_runnability.WorktreeRunnability`) before the gate is
  asked anything, and the same core proves the recipe left the candidate at
  exactly its own commit and tracked content.

The gate itself is untouched: this composes ``PublicationGate.check`` whole and
files nothing of its own. The verdict reaches the history through the gate's
existing receipt writer, appended beside the failure it re-ran.

Provisioning happens AFTER the allowance is reserved and it is not refunded if
it fails. The order is the point: reserving first is what makes "exactly one"
survive a crash, and a provisioning failure that gave the allowance back would
turn a repeatably broken environment into an unbounded supply of gate runs. A
candidate whose checkout cannot be made runnable therefore reaches no gate,
appends no evaluation, leaves the prior non-PASS authoritative, and returns the
continuation to the ordinary exhausted/non-PASS rework direction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.attempt import Attempt, AttemptKey
from ..domain.issue_key_codec import issue_key_path_part
from ..domain.session_run import SessionRunAssets
from ..domain.validation_profile import ValidationGateKind
from ..domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from ..infra.validation_profiles import (
    UnknownValidationProfileError,
    ValidationProfileRegistry,
)
from ..ports.attempt_store import AttemptStore
from ..ports.candidate_checkout import (
    CandidateCheckoutError,
    CandidateCheckouts,
    MaterializedCandidate,
)
from ..ports.session_output import SessionOutput
from .publication_gate import PublicationGate
from .worktree_runnability import WorktreeRunnability

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RevalidationOutcome:
    """What the route did about one candidate, and why.

    ``started`` is deliberately not "succeeded": once the allowance is spent
    the route has changed durable state whatever the gate goes on to decide,
    and a caller must be able to tell "was refused, nothing happened" from
    "ran, and here is the new verdict". ``evaluation`` is the receipt the gate
    appended, or ``None`` when the run reached no verdict to append.
    """

    started: bool
    reason: str
    evaluation: ValidationVerdictReceipt | None = None


def _refuse(reason: str) -> RevalidationOutcome:
    return RevalidationOutcome(started=False, reason=reason)


class PublicationRevalidation:
    """Re-runs the unchanged publication contract against an unchanged SHA."""

    def __init__(
        self,
        *,
        attempts: AttemptStore,
        profiles: Callable[[], ValidationProfileRegistry],
        checkouts: CandidateCheckouts,
        runnability: WorktreeRunnability,
        session_output: SessionOutput,
        publication_gate: PublicationGate,
    ) -> None:
        """Assemble the route from collaborators it only ever composes.

        ``profiles`` is a provider rather than a registry because the registry
        is rebuilt from the current config on every access; a long-lived route
        holding one instance would judge a candidate against the contract it
        was constructed with. The same reason
        :class:`~.publication_evidence.CandidatePublicationEvidence` takes one
        per call.

        ``runnability`` is the provisioning CORE and not the launch
        provisioner: the launch provisioner bundles a consecutive-failure
        ledger and a ``needs-human`` escalation with the recipe, and a second
        retry predicate over this route's one start allowance is exactly what
        #139 does not admit. The recipe and the candidate-integrity proof are
        shared; the bound is not.
        """
        self._attempts = attempts
        self._profiles = profiles
        self._checkouts = checkouts
        self._runnability = runnability
        self._session_output = session_output
        self._publication_gate = publication_gate

    def revalidate(self, candidate: Attempt) -> RevalidationOutcome:
        """Spend this candidate's one revalidation allowance, if it may be spent.

        Args:
            candidate: The durable canonical attempt record. Only the identity
                it carries is trusted — the record is re-read from the store,
                which is what a reconstructed key cannot survive.
        """
        key = candidate.key
        durable = self._durable_candidate(key)
        if durable is None:
            return _refuse("revalidation_candidate_not_durable")

        prior = durable.latest_publication_evaluation
        if prior is None:
            return _refuse("revalidation_no_completed_evaluation")
        refusal = self._admission_refusal(durable, prior)
        if refusal is not None:
            return refusal

        # Step 3 before steps 4-7: the allowance is spent *before* any external
        # gate work, so an interruption anywhere below cannot be replayed into
        # a second same-SHA revalidation. If this write cannot be established,
        # nothing runs.
        try:
            reserved = self._attempts.update(
                key, lambda attempt: attempt.with_revalidation_reserved()
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "[REVALIDATION] refused %s@%s: allowance could not be reserved: %s",
                key.issue_key,
                key.head_sha[:12],
                exc,
            )
            return _refuse("revalidation_allowance_not_reservable")
        logger.info(
            "[REVALIDATION] reserved allowance %d for %s@%s [profile=%s]",
            reserved.revalidation_budget_used,
            key.issue_key,
            key.head_sha[:12],
            prior.profile,
        )
        return self._run_gate(durable, prior)

    # -- admission ---------------------------------------------------------

    def _durable_candidate(self, key: AttemptKey) -> Attempt | None:
        """The stored record for this identity, or ``None`` if there is none.

        Damage reads as refusal rather than as absence for the reason
        :mod:`.publication_evidence` gives: both withhold, but a route that
        treated an unreadable sidecar as "no record" would be making a claim
        about the world from a broken instrument.
        """
        try:
            return self._attempts.for_key(key)
        except (OSError, ValueError) as exc:
            logger.warning("[REVALIDATION] candidate evidence unreadable: %s", exc)
            return None

    def _admission_refusal(
        self,
        durable: Attempt,
        prior: ValidationVerdictReceipt,
    ) -> RevalidationOutcome | None:
        """``None`` when the admission predicate admits this candidate.

        The predicate, in the order the policy states it: the latest completed
        evaluation is non-PASS, the contract is unchanged, and the allowance is
        unspent.
        """
        if prior.verdict is ValidationVerdict.PASSED:
            # Nothing to re-evaluate: the candidate already cleared the gate,
            # and re-running it could only turn an authority into a doubt.
            return _refuse("revalidation_latest_evaluation_passed")
        try:
            contract = self._profiles().resolve(prior.profile).contract(
                ValidationGateKind.PUBLISH
            )
        except UnknownValidationProfileError:
            return _refuse("revalidation_profile_retired")
        mismatch = contract.result_mismatch(
            suite=prior.suite,
            command=prior.command,
            profile=prior.profile,
        )
        if mismatch is not None:
            return _refuse(f"revalidation_contract_changed:{mismatch}")
        if not durable.revalidation_allowance_available:
            return _refuse("revalidation_allowance_consumed")
        return None

    # -- execution ---------------------------------------------------------

    def _run_gate(
        self,
        durable: Attempt,
        prior: ValidationVerdictReceipt,
    ) -> RevalidationOutcome:
        key = durable.key
        try:
            materialized = self._checkouts.materialize(key.head_sha)
        except CandidateCheckoutError as exc:
            logger.warning(
                "[REVALIDATION] %s@%s could not be materialized: %s",
                key.issue_key,
                key.head_sha[:12],
                exc,
            )
            return RevalidationOutcome(
                started=True, reason="revalidation_candidate_unmaterializable"
            )
        try:
            unrunnable = self._runnability.make_runnable(materialized.path)
            if unrunnable is not None:
                # No gate, and therefore no evaluation: an unprovisioned
                # checkout would fail the publish command on its missing tools
                # and file that as a verdict about the candidate — the very
                # misattribution this route exists to undo. The allowance stays
                # spent, so the continuation reads this candidate as exhausted
                # and non-PASS and takes the ordinary rework direction.
                logger.warning(
                    "[REVALIDATION] %s@%s could not be made runnable, so no gate "
                    "was run: %s",
                    key.issue_key,
                    key.head_sha[:12],
                    unrunnable,
                )
                return RevalidationOutcome(
                    started=True, reason="revalidation_candidate_not_provisionable"
                )
            return self._gate_materialized(durable, prior, materialized)
        finally:
            self._checkouts.release(materialized)

    def _gate_materialized(
        self,
        durable: Attempt,
        prior: ValidationVerdictReceipt,
        materialized: MaterializedCandidate,
    ) -> RevalidationOutcome:
        key = durable.key
        run_assets = self._scaffold(
            materialized,
            key_part=issue_key_path_part(key.issue_key),
            profile=prior.profile,
        )
        # Counted over the *publication* evaluations, not the whole history:
        # the history is shared. Every attempt-keyed gate run that reaches a
        # verdict appends to it, including the quick gate, which a rework or
        # review session can run against this same candidate while this is in
        # flight. "The history grew" is therefore not the same fact as "the
        # publication gate filed a verdict", and "the last entry" is not the
        # same value as "the entry this run produced".
        before = len(durable.publication_evaluations)
        outcome = self._publication_gate.check(
            worktree=materialized.path,
            run_assets=run_assets,
            issue_key=key.issue_key,
        )
        # The gate files its own verdict through its existing receipt writer,
        # so what this reads back is whether one was actually appended. A run
        # that reached no verdict — an unconfigured contract, a HEAD the gate
        # could not determine, an evaluation it reused rather than executed —
        # appends nothing, and "never gated" must stay the absence of a receipt
        # rather than a receipt saying nothing.
        after = self._durable_candidate(key)
        evaluations = None if after is None else after.publication_evaluations
        if evaluations is None or len(evaluations) <= before:
            logger.warning(
                "[REVALIDATION] %s@%s reached no recordable verdict: %s",
                key.issue_key,
                key.head_sha[:12],
                outcome.reason,
            )
            return RevalidationOutcome(
                started=True, reason="revalidation_verdict_not_recorded"
            )
        appended = evaluations[-1]
        logger.info(
            "[REVALIDATION] %s@%s re-evaluated: %s (%d publication evaluation(s) "
            "on record)",
            key.issue_key,
            key.head_sha[:12],
            appended.verdict.value,
            len(evaluations),
        )
        return RevalidationOutcome(
            started=True, reason="revalidation_completed", evaluation=appended
        )

    def _scaffold(
        self,
        materialized: MaterializedCandidate,
        *,
        key_part: str,
        profile: str,
    ) -> SessionRunAssets:
        """The minimal run the gate needs, with the prior profile frozen into it.

        The profile *name* comes from the receipt of the evaluation being
        re-run, never from the current default or an agent label: a candidate
        evaluated under P1 is re-evaluated under P1, whatever P1 is bound to
        today. ``RunValidationContracts`` reads that name back off this run's
        manifest, so the gate resolves the same contract the admission check
        just compared against.
        """
        return self._session_output.start_run(
            worktree_path=materialized.path,
            session_name=f"revalidate-{key_part}-{materialized.head_sha[:12]}",
            validation_profile=profile,
        )


__all__ = ["PublicationRevalidation", "RevalidationOutcome"]
