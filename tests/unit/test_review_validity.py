from types import SimpleNamespace

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publication_authority import UnrecordedRefusals
from issue_orchestrator.control.review_validity import evaluate_review_validity
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def test_query_filtered_pr_does_not_require_embedded_review_label() -> None:
    config = Config()
    config.code_review_label = "needs-code-review"
    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=None,
        unrecorded_refusals=UnrecordedRefusals.process_local(),
        pr=PRInfo(
            number=1,
            title="PR",
            url="https://example.test/pull/1",
            branch="1-feature",
            body="Closes #1",
            state="open",
            labels=[],
        ),
        review_label_confirmed=True,
    )

    assert validity.valid is True
    assert validity.reason == "ok"


def test_direct_pr_snapshot_requires_review_label_when_missing() -> None:
    config = Config()
    config.code_review_label = "needs-code-review"
    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(number=1, labels=["agent:web"]),
        unrecorded_refusals=UnrecordedRefusals.process_local(),
        pr=PRInfo(
            number=1,
            title="PR",
            url="https://example.test/pull/1",
            branch="1-feature",
            body="Closes #1",
            state="open",
            labels=[],
        ),
    )

    assert validity.valid is False
    assert validity.reason == "review_label_missing"


def _open_pr(labels: list[str]) -> PRInfo:
    return PRInfo(
        number=41,
        title="PR",
        url="https://example.test/pull/41",
        branch="40-feature",
        body="Closes #40",
        state="open",
        labels=labels,
    )


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

    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(number=40, labels=["agent:backend", "validation-failed"]),
        unrecorded_refusals=UnrecordedRefusals.process_local(),
        pr=_open_pr(["needs-code-review", "rework-cycle-1"]),
        review_label_confirmed=True,
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"
    assert "validation-failed" in validity.issue_labels


def test_failed_publication_gate_is_recognized_under_a_label_prefix() -> None:
    """The marker is read through the registry, so a prefix cannot bypass it."""
    config = Config()
    config.code_review_label = "needs-code-review"
    config.label_prefix = "bot"

    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(number=40, labels=["agent:backend", "bot:validation-failed"]),
        unrecorded_refusals=UnrecordedRefusals.process_local(),
        pr=_open_pr(["needs-code-review"]),
        review_label_confirmed=True,
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"


def test_refusal_that_never_reached_the_issue_still_withholds_review() -> None:
    """A refusal is a refusal whether or not its label write committed (#45).

    The issue carries no marker — the label write is exactly what failed — and
    an earlier candidate's ``needs-code-review`` is still on the PR. Without
    the unrecorded half of the verdict this reads as an ordinary, review-
    eligible PR, which is the fail-open state a single failed write reached.
    """
    config = Config()
    config.code_review_label = "needs-code-review"
    unrecorded = UnrecordedRefusals.process_local()
    unrecorded.hold(40)

    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(number=40, labels=["agent:backend"]),
        unrecorded_refusals=unrecorded,
        pr=_open_pr(["needs-code-review"]),
        review_label_confirmed=True,
    )

    assert validity.valid is False
    assert validity.reason == "issue_publication_gate_failed"


def test_unrecorded_refusal_is_scoped_to_its_own_issue() -> None:
    """Another issue's lost write must not withhold review from this one."""
    config = Config()
    config.code_review_label = "needs-code-review"
    unrecorded = UnrecordedRefusals.process_local()
    unrecorded.hold(99)

    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(number=40, labels=["agent:backend"]),
        unrecorded_refusals=unrecorded,
        pr=_open_pr(["needs-code-review"]),
        review_label_confirmed=True,
    )

    assert validity.valid is True


def test_validated_candidate_still_reaches_review() -> None:
    """Ordinary successful work is unchanged: no new clerical step."""
    config = Config()
    config.code_review_label = "needs-code-review"

    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(number=40, labels=["agent:backend", "in-progress"]),
        unrecorded_refusals=UnrecordedRefusals.process_local(),
        pr=_open_pr(["needs-code-review"]),
        review_label_confirmed=True,
    )

    assert validity.valid is True
    assert validity.reason == "ok"
