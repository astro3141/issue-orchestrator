"""Shared doubles for the durable publication verdict (#45).

Review admission reads ``Attempt(issue, A)``'s evaluation history through the
:class:`~issue_orchestrator.ports.attempt_store.AttemptStore` port, so tests
mock there rather than around the reader. :class:`InMemoryAttemptStore` is a
faithful stand-in for that port: it stores whole ``Attempt`` records under the
same ``(scope, stable_id, head_sha)`` identity the sidecar store files them
under, so a receipt filed for one candidate cannot be read back for another.
"""

from __future__ import annotations

from collections.abc import Callable

from issue_orchestrator.control.publication_authority import (
    PublicationVerdictReader,
    UnrecordedRefusals,
)
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.issue_key import IssueKey
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.entrypoints.bootstrap_completion import (
    _validation_attempt_key_factory,
)
from issue_orchestrator.infra.config import Config

PUBLISH_COMMAND = "make validate-pr-raw"
"""The publish command the fixtures configure and receipts claim to have run."""


class InMemoryAttemptStore:
    """``AttemptStore`` backed by a dict, keyed exactly as the sidecar is."""

    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str, str], Attempt] = {}

    def for_key(self, key: AttemptKey) -> Attempt | None:
        return self._attempts.get(_identity(key))

    def update(
        self, key: AttemptKey, mutate: Callable[[Attempt], Attempt]
    ) -> Attempt:
        current = self.for_key(key)
        updated = mutate(current if current is not None else Attempt(key))
        self._attempts[_identity(key)] = updated
        return updated

    def supersede_issue(self, issue_key: IssueKey) -> int:
        scope = issue_key.scope()
        stable = str(issue_key.stable_id())
        doomed = [
            identity
            for identity in self._attempts
            if identity[0] == scope and identity[1] == stable
        ]
        for identity in doomed:
            del self._attempts[identity]
        return len(doomed)


class UnreadableAttemptStore:
    """``AttemptStore`` whose records are damaged rather than absent.

    The sidecar store raises ``ValueError`` for a sidecar it cannot parse, and
    admission must not read that as "never gated".
    """

    def for_key(self, key: AttemptKey) -> Attempt | None:
        raise ValueError(f"Attempt sidecar is unreadable: {key.head_sha}")

    def update(
        self, key: AttemptKey, mutate: Callable[[Attempt], Attempt]
    ) -> Attempt:
        raise ValueError("unreadable")

    def supersede_issue(self, issue_key: IssueKey) -> int:
        raise ValueError("unreadable")


def _identity(key: AttemptKey) -> tuple[str, str, str]:
    return (key.issue_scope, key.issue_stable_id, key.head_sha)


def publication_receipt(
    head_sha: str,
    *,
    verdict: ValidationVerdict = ValidationVerdict.PASSED,
    suite: str | None = None,
    command: str = PUBLISH_COMMAND,
    profile: str = "default",
) -> ValidationVerdictReceipt:
    """A verdict receipt for ``head_sha``, publication-suite by default."""
    return ValidationVerdictReceipt(
        suite=suite if suite is not None else ValidationGateKind.PUBLISH.suite,
        head_sha=head_sha,
        verdict=verdict,
        command=command,
        profile=profile,
    )


def attempt_store_with(
    *receipts: tuple[IssueKey, ValidationVerdictReceipt],
) -> InMemoryAttemptStore:
    """A store holding one attempt per ``(issue_key, receipt)`` pair."""
    store = InMemoryAttemptStore()
    for issue_key, receipt in receipts:
        key = AttemptKey(issue_key, receipt.head_sha)
        store.update(
            key,
            lambda attempt, r=receipt: attempt.with_completed_evaluation(r),
        )
    return store


def verdict_over(
    store: object,
    *,
    unrecorded: UnrecordedRefusals | None = None,
) -> PublicationVerdictReader:
    """A whole-verdict reader over ``store``, with no refusals by default.

    Built with the production attempt-key factory rather than a local
    stand-in: identifying a candidate is the one thing the read side must do
    exactly as the write side does, so a double that spelled it out itself
    could not catch the two coming apart (#45 A1).
    """
    return PublicationVerdictReader.over(
        unrecorded or UnrecordedRefusals.process_local(),
        store,  # type: ignore[arg-type]
        _validation_attempt_key_factory(Config()),
    )


def verdict_with(
    *receipts: tuple[IssueKey, ValidationVerdictReceipt],
    unrecorded: UnrecordedRefusals | None = None,
) -> PublicationVerdictReader:
    """A verdict reader whose store holds exactly ``receipts``."""
    return verdict_over(attempt_store_with(*receipts), unrecorded=unrecorded)


def verdict_with_no_evidence(
    *, unrecorded: UnrecordedRefusals | None = None
) -> PublicationVerdictReader:
    """A verdict reader that finds no receipt for any candidate.

    Named rather than defaulted: a composition that gets this admits nothing
    once a publication contract is configured, which is the point.
    """
    return verdict_over(InMemoryAttemptStore(), unrecorded=unrecorded)


def configure_publication_contract(
    config: object,
    *,
    command: str = PUBLISH_COMMAND,
) -> None:
    """Give ``config`` a publication contract, so receipts become required.

    A repository that configures no publish command has no publication gate at
    all, and admission does not demand evidence of a contract that does not
    exist. Tests proving the requirement therefore have to configure one.
    """
    config.validation.publish.cmd = command  # type: ignore[attr-defined]
