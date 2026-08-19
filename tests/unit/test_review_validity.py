"""The one seam that decides whether a review may proceed (#21/#45).

Two rules meet here. The *negative* one refuses a review whose issue carries a
publication-gate refusal, recorded or lost. The *positive* one admits a review
only when the exact candidate it would show a reviewer has a durable receipt
proving it cleared the publication contract. The second is what a leftover
``needs-code-review`` label cannot satisfy, and it is the one this file spends
most of its length on.
"""

from types import SimpleNamespace

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publication_authority import (
    PublicationVerdictReader,
    UnrecordedRefusals,
)
from issue_orchestrator.control.review_validity import evaluate_review_validity
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import ValidationVerdict
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationProfileConfig,
)
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.unit.publication_evidence_helpers import (
    PUBLISH_COMMAND,
    UnreadableAttemptStore,
    attempt_store_with,
    configure_publication_contract,
    publication_receipt,
    verdict_over,
    verdict_with,
    verdict_with_no_evidence,
)

CANDIDATE_A = "a" * 40
CANDIDATE_A_PRIME = "b" * 40
ISSUE_KEY = FakeIssueKey(name="40")


def _issue(labels: list[str], *, number: int = 40) -> SimpleNamespace:
    """An issue snapshot carrying the canonical key admission reads."""
    return SimpleNamespace(number=number, labels=labels, key=ISSUE_KEY)


def _open_pr(labels: list[str], *, head_sha: str | None = CANDIDATE_A) -> PRInfo:
    return PRInfo(
        number=41,
        title="PR",
        url="https://example.test/pull/41",
        branch="40-feature",
        body="Closes #40",
        state="open",
        labels=labels,
        head_sha=head_sha,
    )


def _gated_config() -> Config:
    """A repository that actually has a publication contract."""
    config = Config()
    config.code_review_label = "needs-code-review"
    configure_publication_contract(config)
    return config


def _validity(
    *,
    config: Config,
    issue: object | None,
    pr: PRInfo | None,
    publication_verdict: PublicationVerdictReader,
    review_label_confirmed: bool = True,
):
    return evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=issue,
        publication_verdict=publication_verdict,
        pr=pr,
        review_label_confirmed=review_label_confirmed,
    )


# ---------------------------------------------------------------------------
# Pre-existing PR-shaped refusals, unchanged
# ---------------------------------------------------------------------------


def test_query_filtered_pr_does_not_require_embedded_review_label() -> None:
    config = Config()
    config.code_review_label = "needs-code-review"
    validity = _validity(
        config=config,
        issue=None,
        pr=_open_pr([], head_sha=None),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is True
    assert validity.reason == "ok"


def test_direct_pr_snapshot_requires_review_label_when_missing() -> None:
    config = Config()
    config.code_review_label = "needs-code-review"
    validity = _validity(
        config=config,
        issue=_issue(["agent:web"], number=1),
        pr=_open_pr([]),
        publication_verdict=verdict_with_no_evidence(),
        review_label_confirmed=False,
    )

    assert validity.valid is False
    assert validity.reason == "review_label_missing"


# ---------------------------------------------------------------------------
# The negative half: a refusal withholds review
# ---------------------------------------------------------------------------


def test_failed_publication_gate_revokes_review_eligibility() -> None:
    """Validation precedes review (#21/#45).

    The failure-direction proof for this issue: with the publication-gate
    verdict left out of review validity, an issue whose gate just failed stays
    review-eligible purely because an earlier candidate left
    ``needs-code-review`` on the PR — which is what launched a review against
    a rejected candidate.
    """
    config = Config()
    config.code_review_label = "needs-code-review"

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend", "validation-failed"]),
        pr=_open_pr(["needs-code-review", "rework-cycle-1"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"
    assert "validation-failed" in validity.issue_labels


def test_failed_publication_gate_is_recognized_under_a_label_prefix() -> None:
    """The marker is read through the registry, so a prefix cannot bypass it."""
    config = Config()
    config.code_review_label = "needs-code-review"
    config.label_prefix = "bot"

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend", "bot:validation-failed"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"


def test_refusal_that_never_reached_the_issue_still_withholds_review() -> None:
    """A refusal is a refusal whether or not its label write committed (#45)."""
    config = Config()
    config.code_review_label = "needs-code-review"
    unrecorded = UnrecordedRefusals.process_local()
    unrecorded.hold(40)

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(unrecorded=unrecorded),
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"


def test_unrecorded_refusal_is_scoped_to_its_own_issue() -> None:
    """Another issue's lost write must not withhold review from this one."""
    config = Config()
    config.code_review_label = "needs-code-review"
    unrecorded = UnrecordedRefusals.process_local()
    unrecorded.hold(99)

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(unrecorded=unrecorded),
    )

    assert validity.valid is True


# ---------------------------------------------------------------------------
# The positive half: exact-candidate publication evidence
# ---------------------------------------------------------------------------


def test_validated_candidate_still_reaches_review() -> None:
    """Ordinary successful work is unchanged: no new clerical step (proof 16).

    The receipt is filed by the publication gate itself, so a candidate that
    genuinely passed arrives here already carrying its own authority.
    """
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend", "in-progress"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A))
        ),
    )

    assert validity.valid is True
    assert validity.reason == "ok"


def test_review_label_alone_does_not_admit_an_ungated_candidate() -> None:
    """Proof 11, and the incident this issue was filed for.

    Nothing is wrong with the issue: no blocking label, no refusal marker, no
    ``needs-rework``. The PR carries ``needs-code-review`` from an earlier
    candidate. Every *negative* rule passes — and the candidate was never
    gated, so the positive one refuses.
    """
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend", "in-progress"]),
        pr=_open_pr(["needs-code-review", "code-reviewed", "rework-cycle-1"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is False
    assert validity.reason == "publication_receipt_missing"


@pytest.mark.parametrize(
    "verdict", [ValidationVerdict.FAILED, ValidationVerdict.TIMED_OUT]
)
def test_a_recorded_failure_or_timeout_does_not_admit(verdict) -> None:
    """Proof 6: a receipt is not a pass."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A, verdict=verdict))
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_verdict_not_passed"


def test_a_quick_gate_pass_is_not_a_publication_pass() -> None:
    """Proof 7: the wrong suite proves the wrong contract ran.

    Since #139 the attempt keeps every completed evaluation and admission asks
    for the latest *publication* one, so a candidate whose only evidence is a
    quick-contract PASS refuses as never-gated rather than as gated-and-failed.
    Both refuse; the newer reason is the more accurate of the two — no
    publication gate has reported on this candidate at all.
    """
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (
                ISSUE_KEY,
                publication_receipt(
                    CANDIDATE_A, suite=ValidationGateKind.QUICK.suite
                ),
            )
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_receipt_missing"


def test_a_quick_gate_pass_beside_a_publication_failure_still_refuses() -> None:
    """A later quick PASS cannot promote an earlier publication FAIL."""
    key = AttemptKey(ISSUE_KEY, CANDIDATE_A)
    store = attempt_store_with(
        (
            ISSUE_KEY,
            publication_receipt(CANDIDATE_A, verdict=ValidationVerdict.FAILED),
        )
    )
    store.update(
        key,
        lambda attempt: attempt.with_completed_evaluation(
            publication_receipt(CANDIDATE_A, suite=ValidationGateKind.QUICK.suite)
        ),
    )

    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_over(store),
    )

    assert validity.valid is False
    assert validity.reason == "publication_verdict_not_passed"


def test_an_earlier_candidates_pass_does_not_authorize_a_later_one() -> None:
    """Proofs 4 and 5: PASS(A) is not authority for A′.

    The launcher re-reads the PR at launch, so a head that moved from A to A′
    while the review sat queued is judged as A′ — and A's receipt is filed
    under a different key entirely.
    """
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"], head_sha=CANDIDATE_A_PRIME),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A))
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_receipt_missing"


def test_an_old_failure_does_not_block_a_later_validated_candidate() -> None:
    """Proof 12: FAIL(A) history is about A, and only about A."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"], head_sha=CANDIDATE_A_PRIME),
        publication_verdict=verdict_with(
            (
                ISSUE_KEY,
                publication_receipt(
                    CANDIDATE_A, verdict=ValidationVerdict.FAILED
                ),
            ),
            (ISSUE_KEY, publication_receipt(CANDIDATE_A_PRIME)),
        ),
    )

    assert validity.valid is True
    assert validity.reason == "ok"


def test_a_pr_with_no_readable_head_is_refused() -> None:
    """A missing candidate SHA is never "assume the current one"."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"], head_sha=None),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A))
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_candidate_unknown"


def test_an_abbreviated_head_is_refused_rather_than_expanded() -> None:
    """Truncation is how two different commits come to compare equal."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"], head_sha=CANDIDATE_A[:8]),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A))
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_candidate_unknown"


def test_an_unidentifiable_issue_cannot_authorize_a_review() -> None:
    """No canonical key means no record to read the verdict from."""
    validity = _validity(
        config=_gated_config(),
        issue=None,
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A))
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_candidate_unidentified"


def test_damaged_evidence_is_refused_and_named_as_damage() -> None:
    """Proof 6's "malformed": a broken instrument is not a reading."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_over(UnreadableAttemptStore()),
    )

    assert validity.valid is False
    assert validity.reason == "publication_evidence_unreadable"


# ---------------------------------------------------------------------------
# Contract freshness: the profile name is frozen, its body is live
# ---------------------------------------------------------------------------


def test_a_changed_contract_body_makes_the_receipt_stale() -> None:
    """Proof 8: same frozen profile name, different command now."""
    config = _gated_config()
    config.validation.publish.cmd = "make validate-pr-raw --now-with-more"

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (
                ISSUE_KEY,
                publication_receipt(CANDIDATE_A, command=PUBLISH_COMMAND),
            )
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_contract_changed:command"


def test_a_retired_profile_fails_closed() -> None:
    """Proof 9: nothing can say what a vanished contract's pass meant."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (ISSUE_KEY, publication_receipt(CANDIDATE_A, profile="retired"))
        ),
    )

    assert validity.valid is False
    assert validity.reason == "publication_profile_retired"


def test_a_moved_default_does_not_invalidate_a_frozen_profile_receipt() -> None:
    """Proof 10: the candidate is judged against the profile it ran under.

    ``strict`` still defines the command the receipt names, so the receipt is
    still fresh — even though the *default* profile has since changed to
    something else entirely. Admission must not re-select a profile from the
    agent label or the default.
    """
    config = Config()
    config.code_review_label = "needs-code-review"
    config.validation.publish.cmd = "some entirely different default gate"
    config.validation.profiles["strict"] = ValidationProfileConfig(
        quick=ValidationCommandConfig(cmd="make validate-quick"),
        publish=PublishValidationConfig(cmd=PUBLISH_COMMAND),
    )

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with(
            (
                ISSUE_KEY,
                publication_receipt(
                    CANDIDATE_A, profile="strict", command=PUBLISH_COMMAND
                ),
            )
        ),
    )

    assert validity.valid is True
    assert validity.reason == "ok"


# ---------------------------------------------------------------------------
# Repositories with no publication contract at all
# ---------------------------------------------------------------------------


def test_a_repository_without_a_publish_command_needs_no_receipt() -> None:
    """There is no gate, so there is nothing a candidate could have cleared.

    ``PublicationGate`` reads the same configuration as "the operator chose no
    publication gate", allows publication, and records nothing. Demanding a
    receipt here would leave publication permitted and review blocked forever.
    """
    config = Config()
    config.code_review_label = "needs-code-review"

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is True
    assert validity.reason == "ok"


def test_a_repository_without_a_publish_command_still_honours_a_refusal() -> None:
    """The negative half is unconditional."""
    config = Config()
    config.code_review_label = "needs-code-review"

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend", "validation-failed"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"


def test_a_publish_command_in_any_profile_makes_the_receipt_required() -> None:
    """The requirement attaches as soon as a publication contract exists.

    Repository-wide because a PR does not say which profile produced it — the
    receipt is the only thing that would. Which leaves the mixed shape (this
    candidate's own profile defines no publish command while another does)
    refused here, with no way out from this side; that is why the *gate*
    refuses such a candidate at publication time, before any PR exists for
    admission to be asked about. This asserts the reader still fails closed if
    one ever reaches it (#45 F2).
    """
    config = Config()
    config.code_review_label = "needs-code-review"
    config.validation.profiles["strict"] = ValidationProfileConfig(
        quick=ValidationCommandConfig(cmd="make validate-quick"),
        publish=PublishValidationConfig(cmd=PUBLISH_COMMAND),
    )

    validity = _validity(
        config=config,
        issue=_issue(["agent:backend"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is False
    assert validity.reason == "publication_receipt_missing"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_a_blocked_issue_reports_the_block_not_the_missing_receipt() -> None:
    """The more specific refusal is the one an operator needs to read."""
    validity = _validity(
        config=_gated_config(),
        issue=_issue(["agent:backend", "blocked:provider-unavailable"]),
        pr=_open_pr(["needs-code-review"]),
        publication_verdict=verdict_with_no_evidence(),
    )

    assert validity.valid is False
    assert validity.reason == "issue_blocked"
