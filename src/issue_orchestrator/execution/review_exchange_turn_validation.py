"""Who files one review-exchange coder turn's validation evidence (#388).

Every round of the exchange is judged on the same fact: a passing
``validation-record.json`` naming the commit the coder worktree stands at right
now. Until #388 there was exactly one way for that fact to exist — the coder's
own ``coding-done completed`` ran the code-candidate quick gate and wrote it —
and the lane simply assumed the agent occupying the coder SIDE was allowed to
produce it.

A Tech Lead is not. The quick gate needs the same host and shared-repository
effects the coder protocol's ``prepush-check`` step does — a write to
``<git-common-dir>/issue-orchestrator/validate-timings.jsonl`` among them — and
#364 measured a bounded Tech Lead dying on exactly that. #370 took the gate off
the Tech Lead's ``coding-done`` and #385 took the pre-push step out of its
completion document, but neither reached this lane, so swapping the document
alone would have left a Tech Lead refused for a record the document no longer
asks it to write. The obligation is in the lane's *contract*, not only in its
prose.

So the owner moves here too. This module is the one place that answers "who
produced this round's evidence", with one implementation per principal:

* :class:`CoderFiledTurnEvidence` — the Actor lane, byte-for-byte what the
  exchange did before: mirror whatever the coder's own completion left behind.
* :class:`TrustedTurnEvidence` — the Tech Lead lane: ask the trusted owner
  (:class:`~...ports.tech_lead_completion_validation.TechLeadCompletionValidator`,
  the same one #385 gates the primary lane on) for a verdict on the exchange
  run, this session and the coder worktree's current HEAD, and publish the
  passing ones as the pair's evidence.

Both answer the same shape — ``None`` when current evidence now stands, a
sentence when it does not — so the round loop's gate is unchanged and the
freshness contract (:func:`~.review_exchange_validation_mirror.
validation_record_error`) still has exactly one reader.

**Fail closed in every direction.** Missing, failed, timed-out, unavailable,
unreadable, or evidence bound to another run/session/commit does not settle a
round as success and authorizes no publication: the pair's record is cleared and
the refusal is returned as the turn's protocol error. Nothing here can publish a
pass the trusted owner did not file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..control.tech_lead_completion_validation import (
    read_trusted_completion_validation,
)
from ..control.validation_record_cache import VALIDATION_SCHEMA_VERSION
from ..domain.review_exchange_coder_principal import ReviewExchangeCoderPrincipal
from ..domain.tech_lead_completion_validation import (
    TechLeadCompletionValidation,
    TrustedVerdictRefusal,
)
from ..ports.session_output import ValidationRecord
from ..ports.tech_lead_completion_validation import TechLeadCompletionValidator
from .review_exchange_validation_mirror import PairValidationMirror

logger = logging.getLogger(__name__)

__all__ = [
    "TRUSTED_TURN_VALIDATION_SUITE",
    "CoderFiledTurnEvidence",
    "ExchangeTurnEvidence",
    "TrustedTurnEvidence",
    "build_turn_evidence",
]

#: The ``suite`` stamped on evidence the trusted owner produced. Deliberately
#: neither ``agent_gate`` nor ``publish_gate``: a reader of this record must be
#: able to see that a different contract ran, executed by a different owner,
#: rather than assume the coder's quick gate did. It matches the ``kind`` the
#: same owner stamps on its shared-git-dir timing record.
TRUSTED_TURN_VALIDATION_SUITE = "tech_lead_completion_validation"


class ExchangeTurnEvidence(Protocol):
    """Put this exchange's validation evidence into pair scope."""

    def seed(self, initial_validation_record_path: Path | None) -> None:
        """File the evidence the exchange OPENS on, before any round runs.

        The reviewer moves first, and its approval is gated on the pair's
        record being current — so an exchange that opens with none cannot
        approve in round one, and the coder is handed a rework request naming
        an artifact it may not have authored. Whoever files the round's
        evidence therefore also files the opening evidence.

        Args:
            initial_validation_record_path: What the caller's completion left
                behind, if anything. Meaningful only to the principal that
                produced it.
        """
        ...

    def file_for_turn(self, payload: dict[str, Any]) -> str | None:
        """Return ``None`` when current evidence stands, else why it does not.

        Args:
            payload: The coder's completion artifact, already proven to be a
                JSON object. The Actor lane reads its
                ``validation_record_path``; the trusted lane ignores it,
                because the evidence it files is not the model's to point at.
        """
        ...


@dataclass(frozen=True)
class CoderFiledTurnEvidence:
    """The Actor lane: the coder's own ``coding-done`` wrote the record.

    Unchanged behaviour, given a name. The mirror still owns where the record
    may live and what makes it current; this only says whose it is.
    """

    mirror: PairValidationMirror
    run_validation_record_path: Path

    def seed(self, initial_validation_record_path: Path | None) -> None:
        """Mirror the completing session's own record, as ever.

        A missing source still clears any prior pair record: an exchange
        without current validation evidence must not inherit the last one's.
        """
        self.mirror.replace_from_initial(initial_validation_record_path)

    def file_for_turn(self, payload: dict[str, Any]) -> str | None:
        return self.mirror.refresh_from_completion(
            payload,
            run_validation_record_path=self.run_validation_record_path,
        )


@dataclass(frozen=True)
class TrustedTurnEvidence:
    """The Tech Lead lane: a trusted owner outside the sandbox files it.

    ``run_id`` and ``session_name`` are the EXCHANGE run's, not the completing
    session's, because that is what the evidence is about: one turn of one
    exchange, against the commit in front of the coder now. Together with that
    commit they form the trusted owner's create-once key, so a round that
    commits nothing re-reads its own verdict rather than earning a second,
    possibly kinder one, and a round that commits something is a new candidate
    with a verdict of its own.
    """

    mirror: PairValidationMirror
    validator: TechLeadCompletionValidator
    run_id: str
    session_name: str
    coder_worktree: Path
    validation_profile: str

    def seed(self, initial_validation_record_path: Path | None) -> None:
        """Open the exchange on the trusted owner's verdict, not the caller's.

        ``initial_validation_record_path`` is deliberately ignored: on this
        lane the completing session filed no record — its gate is the
        orchestrator's (#370) — and a record from anywhere else is not evidence
        this principal produced. Asking the trusted owner instead costs
        nothing beyond the first call, because its verdict is create-once per
        candidate: the coder's first turn on an unchanged HEAD re-reads exactly
        this one.

        A refusal here is not raised. It leaves the pair with no evidence,
        which is the same state an unseeded exchange would be in, and the two
        readers that care — the reviewer's approval gate and the coder turn's
        own gate — each refuse on it in their own words.
        """
        del initial_validation_record_path
        refusal = self.file_for_turn({})
        if refusal is not None:
            logger.info(
                "[REVIEW_EXCHANGE] exchange opens with no trusted validation"
                " evidence for %s/%s: %s",
                self.run_id,
                self.session_name,
                refusal,
            )

    def file_for_turn(self, payload: dict[str, Any]) -> str | None:
        del payload  # The model does not get to point at its own evidence.
        head_sha = self.mirror.observe_candidate_head()
        if head_sha is None:
            return self._refuse(
                "cannot determine the commit the coder worktree stands at, so"
                " the trusted completion validation has no candidate to bind to"
            )
        outcome = read_trusted_completion_validation(
            validator=self.validator,
            run_id=self.run_id,
            session_name=self.session_name,
            worktree=self.coder_worktree,
            candidate_head_sha=head_sha,
        )
        if isinstance(outcome, TrustedVerdictRefusal):
            # Both halves: the round's protocol error is the sentence an
            # operator reads, and the named cause is what tells a FAILED gate
            # from a TIMED_OUT one from an owner that never answered.
            return self._refuse(f"{outcome.failure}: {outcome.detail}")
        self.mirror.publish(self._record(outcome))
        return None

    def _record(self, verdict: TechLeadCompletionValidation) -> ValidationRecord:
        """The pair's evidence, saying plainly which contract produced it."""
        recorded_at = verdict.recorded_at.isoformat()
        return ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite=TRUSTED_TURN_VALIDATION_SUITE,
            head_sha=verdict.candidate_head_sha,
            passed=True,
            exit_code=0,
            command=(
                "trusted completion validation executed by the orchestrator"
                f" for {verdict.run_id}/{verdict.session_name}"
            ),
            started_at=recorded_at,
            ended_at=recorded_at,
            profile=self.validation_profile,
        )

    def _refuse(self, detail: str) -> str:
        self.mirror.clear()
        return detail


def build_turn_evidence(
    principal: ReviewExchangeCoderPrincipal,
    *,
    mirror: PairValidationMirror,
    run_validation_record_path: Path,
    validator: TechLeadCompletionValidator,
    run_id: str,
    session_name: str,
    coder_worktree: Path,
    validation_profile: str,
) -> ExchangeTurnEvidence:
    """Select the turn's evidence owner from the lane's declared principal.

    One selection, so the document the session was handed and the evidence the
    round is judged on cannot disagree: both are consequences of the same
    :class:`~...domain.review_exchange_coder_principal.
    ReviewExchangeCoderPrincipal` value.
    """
    if principal.files_its_own_turn_validation:
        return CoderFiledTurnEvidence(
            mirror=mirror,
            run_validation_record_path=run_validation_record_path,
        )
    return TrustedTurnEvidence(
        mirror=mirror,
        validator=validator,
        run_id=run_id,
        session_name=session_name,
        coder_worktree=coder_worktree,
        validation_profile=validation_profile,
    )
