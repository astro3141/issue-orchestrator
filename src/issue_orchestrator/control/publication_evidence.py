"""Whether one candidate has proven it cleared the publication gate (#45).

The positive half of #21's ordering rule::

    actor -> validation (publication gate) -> review -> PR -> human merge

:mod:`.publication_authority` holds the negative half — an issue-scoped marker
saying "the gate refused". That is a projection onto the *issue*, and an issue
outlives the candidate the refusal was about: clearing it for a later candidate
clears it for every reader, and a review-trigger label an earlier candidate
left on the PR then reads as authority for whatever commit happens to be at the
head now. The incident #45 was filed for is exactly that shape.

So the question asked here is not "is this issue currently unrefused" but
"**did this exact commit pass the publication contract**". It is answered from
``Attempt(issue, A)`` — the record already keyed by exactly one candidate,
already durable in the primary checkout, and since #85 already carrying the
publication gate's own verdict receipt. Absence is not neutral: no receipt
means no publication gate has reported on this candidate, and a candidate that
was never gated has not cleared a gate.

Freshness is a second question, and the receipt was built to answer it. What a
run freezes at launch is the profile *name*; the contract body behind that name
is re-resolved live on every access (:meth:`~.publication_gate.
RunValidationContracts.profile_for_run`). So this resolves the receipt's own
frozen profile name against the currently loaded registry and asks whether the
contract that executed is still the contract now required. It never re-selects
a profile from the agent label or the default — a candidate created under P1
is judged against P1, whatever P1 is bound to today.

The comparison itself is :meth:`~..infra.validation_profiles.
ValidationGateContract.result_mismatch`, the same predicate the validation
cache uses to decide whether a stored record may satisfy a request. One
spelling, so "which contract ran" cannot mean one thing to the gate and
another to admission.

Every failure direction refuses. The requirement is conditional on exactly one
thing, checked before anything else: a repository that configures no publish
command in any profile has no publication contract, so there is no gate a
candidate could have cleared and nothing anyone could produce as evidence. The
:class:`~..control.publication_gate.PublicationGate` reads that configuration
the same way — it allows publication and records nothing — so requiring a
receipt there would leave publication permitted and review blocked forever.
See :attr:`~..infra.validation_profiles.ValidationProfileRegistry.
any_publish_command_configured`. The negative half of the verdict is
unconditional and still applies in such a repository.

That question is repository-wide because this reader has only a PR to go on,
and a PR does not say which validation profile produced it. The mixed shape it
cannot see — one profile defining a publish command while the profile a
candidate actually ran under defines none — is therefore not left for this
reader to guess at: the gate refuses such a candidate at publication time,
where the run's frozen profile is known, so no PR reaches admission needing a
receipt its own contract could never have filed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.issue_key import IssueKey
from ..domain.validation_profile import ValidationGateKind
from ..infra.validation_profiles import (
    UnknownValidationProfileError,
    ValidationProfileRegistry,
)
from ..ports.attempt_store import AttemptStore
from ..ports.validation_attempt_key_factory import ValidationAttemptKeyFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublicationCertification:
    """Whether a candidate's publication evidence authorizes reviewing it."""

    admitted: bool
    reason: str


_ADMITTED = PublicationCertification(admitted=True, reason="ok")
_NO_PUBLICATION_CONTRACT = PublicationCertification(
    admitted=True, reason="publication_contract_not_configured"
)


def _refuse(reason: str) -> PublicationCertification:
    return PublicationCertification(admitted=False, reason=reason)


class CandidatePublicationEvidence:
    """Reads the durable publication verdict filed against one candidate.

    Stateless over the attempt store, and deliberately given the profile
    registry per call rather than at construction: the registry is rebuilt from
    the current config on every access, so a reloaded config must not be able
    to leave a long-lived reader answering from the contract it was built with.

    ``attempt_keys`` is the same port the *writer* derives its key from
    (:class:`~.publication_verdict.PublicationVerdictReceipts`). Filing a
    receipt and finding it again are two halves of one identity question, and
    spelling that identity out separately on each side is how the two ends of a
    record drift apart — the drift ``result_mismatch`` was extracted to prevent
    for "which contract ran".
    """

    def __init__(
        self,
        attempts: AttemptStore,
        attempt_keys: ValidationAttemptKeyFactory,
    ) -> None:
        self._attempts = attempts
        self._attempt_keys = attempt_keys

    def certification(
        self,
        *,
        issue_key: IssueKey | None,
        head_sha: str | None,
        profiles: ValidationProfileRegistry,
    ) -> PublicationCertification:
        """Whether ``head_sha`` may be reviewed on this issue's authority.

        ``issue_key`` and ``head_sha`` are the two halves of the candidate's
        identity, and either being absent is refused here rather than at the
        call site — a caller that cannot name the candidate cannot be allowed
        to decide what an unnamed candidate means.
        """
        if not profiles.any_publish_command_configured:
            # Asked first, and about the configuration rather than the
            # candidate: where no profile defines a publish command there is no
            # publication contract, so there is no gate a candidate could have
            # cleared and no evidence anyone could produce. Demanding identity
            # or a receipt here would not be strict, it would be unanswerable.
            # The negative half of the verdict still applies in such a
            # repository — a recorded refusal is still a refusal.
            return _NO_PUBLICATION_CONTRACT
        if issue_key is None:
            return _refuse("publication_candidate_unidentified")
        try:
            key = self._attempt_keys.for_validation_attempt(
                issue_key=issue_key,
                head_sha=head_sha or "",
            )
        except (TypeError, ValueError):
            # No usable candidate SHA: the PR read did not carry a head, or
            # carried one this codebase will not compare (abbreviated, non-hex).
            # "Assume it is the current one" is the assumption #45 forbids.
            # Normalization lives in ``AttemptKey.__post_init__``, so the
            # refusal covers a rejected SHA however the factory builds the key.
            return _refuse("publication_candidate_unknown")

        try:
            attempt = self._attempts.for_key(key)
        except (OSError, ValueError) as exc:
            # Damaged evidence is not absent evidence. Reading a corrupt record
            # as "never gated" would be a claim about the world made from a
            # broken instrument; both refuse, but only one says so.
            logger.warning(
                "Publication evidence for %s@%s is unreadable; withholding "
                "review: %s",
                issue_key,
                key.head_sha[:12],
                exc,
            )
            return _refuse("publication_evidence_unreadable")

        if attempt is None or attempt.publication_verdict is None:
            return _refuse("publication_receipt_missing")
        receipt = attempt.publication_verdict
        if not attempt.publication_validation_passed:
            # Folds FAIL, timeout, a receipt from the quick contract, and a
            # receipt naming another commit into one refusal, because the
            # attempt already decides all four against its own key.
            return _refuse("publication_verdict_not_passed")

        try:
            contract = profiles.resolve(receipt.profile).contract(
                ValidationGateKind.PUBLISH
            )
        except UnknownValidationProfileError:
            # The contract this candidate was validated under no longer exists,
            # so nothing can say whether the pass still means anything.
            return _refuse("publication_profile_retired")

        mismatch = contract.result_mismatch(
            suite=receipt.suite,
            command=receipt.command,
            profile=receipt.profile,
        )
        if mismatch is not None:
            return _refuse(f"publication_contract_changed:{mismatch}")
        return _ADMITTED


__all__ = ["CandidatePublicationEvidence", "PublicationCertification"]
