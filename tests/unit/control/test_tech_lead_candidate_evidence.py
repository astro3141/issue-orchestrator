"""The independent Reviewer's evidence, staged for the exact candidate (#345).

Proof direction B. The Tech Lead's data-source contract forbids it from
fetching missing GitHub context, so the prerequisite the merge contract assumes
— an independent reviewer approved THIS commit — has to be staged for it or it
cannot be established at all.

Every test here fixes one refusal, because the value of the staged record is
that absent, misbound and negative evidence are each visible AS what they are
rather than collapsing into one silence.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.publication_authority import (
    PublicationVerdictReader,
    UnrecordedRefusals,
)
from issue_orchestrator.control.tech_lead_candidate_evidence import (
    DurableCandidateEvidence,
    build_candidate_evidence,
)
from issue_orchestrator.execution.tech_lead_downloader import (
    write_candidate_evidence,
)
from issue_orchestrator.ports.tech_lead_candidate_evidence import (
    NO_TECH_LEAD_CANDIDATE_EVIDENCE,
)
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.execution_identity import (
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
    ExecutionPrincipal,
    ExecutionProvenance,
    ExecutionRole,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.review_verdict_binding import (
    BoundReviewVerdict,
    ReviewVerdictOutcome,
)
from issue_orchestrator.domain.tech_lead_candidate import (
    TECH_LEAD_CANDIDATE_EVIDENCE_FILENAME,
)
from issue_orchestrator.domain.tech_lead_manifest import PRToReview
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.entrypoints.bootstrap_completion import (
    _validation_attempt_key_factory,
)
from issue_orchestrator.execution.attempt_execution_identity_store import (
    AttemptExecutionIdentityStore,
)
from issue_orchestrator.execution.attempt_review_verdict_store import (
    AttemptReviewVerdictStore,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
)
from issue_orchestrator.infra.validation_profiles import ValidationProfileRegistry

CANDIDATE_A = "a" * 40
CANDIDATE_B = "b" * 40


class Host:
    """The repository host's only role here: naming a candidate's issue key."""

    def create_issue_key(self, issue_number: int) -> GitHubIssueKey:
        return GitHubIssueKey(repo="acme/repo", external_id=str(issue_number))


def _entry(head_sha: str = CANDIDATE_A, *, branch: str = "42-add-thing") -> PRToReview:
    return PRToReview(
        number=101,
        title="Add the thing",
        url="https://example/pr/101",
        branch=branch,
        head_sha=head_sha,
    )


def _publication_contract() -> ValidationProfileRegistry:
    """A repository that HAS a publication gate, so a candidate can miss it.

    The default fixture below configures no publish command, where
    ``certification`` admits every candidate because there is no contract to
    clear. That is the right answer there and the wrong world for proving the
    certification half of the gap, so this one is asked for explicitly.
    """
    return ValidationProfileRegistry(
        ValidationConfig(
            quick=ValidationCommandConfig(cmd="make quick", timeout_seconds=111),
            publish=PublishValidationConfig(cmd="make publish", timeout_seconds=222),
        )
    )


def _reader(
    tmp_path: Path, *, profiles: ValidationProfileRegistry | None = None
) -> tuple[DurableCandidateEvidence, SidecarAttemptStore]:
    config = Config(repo="acme/repo", repo_root=tmp_path)
    attempts = SidecarAttemptStore(tmp_path)
    reader = DurableCandidateEvidence(
        review_verdicts=AttemptReviewVerdictStore(attempts),
        execution_identities=AttemptExecutionIdentityStore(attempts),
        publication_verdict=PublicationVerdictReader.over(
            UnrecordedRefusals.process_local(),
            attempts,
            _validation_attempt_key_factory(config),
        ),
        profiles=profiles if profiles is not None else config.validation_profiles(),
    )
    return reader, attempts


def _record_verdict(
    attempts: SidecarAttemptStore,
    *,
    reviewed_sha: str,
    verdict: ReviewVerdictOutcome = ReviewVerdictOutcome.APPROVED,
    issue_number: int = 42,
) -> None:
    AttemptReviewVerdictStore(attempts).record(
        AttemptKey(
            GitHubIssueKey(repo="acme/repo", external_id=str(issue_number)),
            reviewed_sha,
        ),
        BoundReviewVerdict(
            verdict=verdict,
            reviewed_sha=reviewed_sha,
            decided_at="2026-06-03T00:00:00+00:00",
            completed_rounds=1,
        ),
    )


def _certify_publication(
    attempts: SidecarAttemptStore, *, candidate_sha: str, issue_number: int = 42
) -> None:
    """File a passing publication receipt for this exact commit.

    Written through the same durable record the gate writes, and with the
    command/profile the registry in :func:`_publication_contract` defines, so
    ``result_mismatch`` agrees this receipt came from that contract.
    """
    attempts.update(
        AttemptKey(
            GitHubIssueKey(repo="acme/repo", external_id=str(issue_number)),
            candidate_sha,
        ),
        lambda attempt: replace(
            attempt,
            completed_evaluations=(
                *attempt.completed_evaluations,
                ValidationVerdictReceipt(
                    suite="publish_gate",
                    head_sha=candidate_sha,
                    verdict=ValidationVerdict.PASSED,
                    command="make publish",
                    profile="default",
                ),
            ),
        ),
    )


def _record_identities(attempts: SidecarAttemptStore, *, candidate_sha: str) -> None:
    AttemptExecutionIdentityStore(attempts).record(
        AttemptKey(
            GitHubIssueKey(repo="acme/repo", external_id="42"), candidate_sha
        ),
        CandidateExecutionIdentities(
            candidate_sha=candidate_sha,
            actor=AgentExecutionIdentity(
                role=ExecutionRole.ACTOR,
                principal=ExecutionPrincipal(agent_label="agent:backend"),
                provenance=ExecutionProvenance(provider="claude-code", model="opus"),
            ),
            reviewer=AgentExecutionIdentity(
                role=ExecutionRole.REVIEWER,
                principal=ExecutionPrincipal(agent_label="agent:reviewer"),
                provenance=ExecutionProvenance(provider="codex", model="gpt-5"),
            ),
            observed_at="2026-06-03T00:00:00+00:00",
        ),
    )


class TestExactCandidateReviewEvidence:
    def test_an_approval_of_this_commit_establishes_the_prerequisite(
        self, tmp_path: Path
    ) -> None:
        reader, attempts = _reader(tmp_path)
        _record_verdict(attempts, reviewed_sha=CANDIDATE_A)
        _record_identities(attempts, candidate_sha=CANDIDATE_A)

        evidence = reader.evidence_for(_entry(), repository_host=Host())

        assert evidence.establishes_independent_review is True
        assert evidence.reviewer_verdict == "approved"
        assert evidence.reviewed_sha == CANDIDATE_A
        assert evidence.reviewer_principal == "agent:reviewer"
        assert evidence.actor_principal == "agent:backend"

    def test_an_approval_of_another_commit_cannot_satisfy_this_candidate(
        self, tmp_path: Path
    ) -> None:
        reader, attempts = _reader(tmp_path)
        _record_verdict(attempts, reviewed_sha=CANDIDATE_B)

        evidence = reader.evidence_for(_entry(CANDIDATE_A), repository_host=Host())

        assert evidence.establishes_independent_review is False
        assert "no independent reviewer verdict is recorded" in evidence.gap

    def test_missing_review_evidence_is_a_named_gap_not_a_silence(
        self, tmp_path: Path
    ) -> None:
        reader, _ = _reader(tmp_path)

        evidence = reader.evidence_for(_entry(), repository_host=Host())

        assert evidence.establishes_independent_review is False
        assert CANDIDATE_A[:12] in evidence.gap

    def test_a_changes_requested_verdict_is_not_an_approval(
        self, tmp_path: Path
    ) -> None:
        reader, attempts = _reader(tmp_path)
        _record_verdict(
            attempts,
            reviewed_sha=CANDIDATE_A,
            verdict=ReviewVerdictOutcome.CHANGES_REQUESTED,
        )

        evidence = reader.evidence_for(_entry(), repository_host=Host())

        assert evidence.establishes_independent_review is False
        assert "did not approve" in evidence.gap

    def test_an_unbindable_candidate_cannot_carry_evidence(
        self, tmp_path: Path
    ) -> None:
        reader, _ = _reader(tmp_path)

        evidence = reader.evidence_for(_entry(head_sha=""), repository_host=Host())

        assert evidence.establishes_independent_review is False
        assert "without an observable head commit" in evidence.gap

    def test_an_approved_commit_that_never_cleared_publication_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The fourth gap direction, and the one that is not about the reviewer.

        The reviewer approved THIS commit; what is missing is the publication
        gate's own certification of it. The prerequisite still refuses — a
        merge-facing PASS rests on both — but the recorded reason has to say
        which, because it is the only thing the refusal receipt can tell an
        operator to go and fix.
        """
        reader, attempts = _reader(tmp_path, profiles=_publication_contract())
        _record_verdict(attempts, reviewed_sha=CANDIDATE_A)
        _record_identities(attempts, candidate_sha=CANDIDATE_A)

        evidence = reader.evidence_for(_entry(), repository_host=Host())

        assert evidence.reviewer_verdict == "approved"
        assert evidence.reviewed_sha == CANDIDATE_A
        assert evidence.publication_certified is False
        assert evidence.establishes_independent_review is False
        assert "publication-gate certification" in evidence.gap
        assert evidence.publication_reason in evidence.gap
        # And it does NOT claim the reviewer half is what went wrong.
        assert "did not approve" not in evidence.gap
        assert "no independent reviewer verdict" not in evidence.gap

    def test_a_certified_commit_with_an_approval_establishes_the_prerequisite(
        self, tmp_path: Path
    ) -> None:
        """The falsification: same repository, certification restored."""
        reader, attempts = _reader(tmp_path, profiles=_publication_contract())
        _record_verdict(attempts, reviewed_sha=CANDIDATE_A)
        _certify_publication(attempts, candidate_sha=CANDIDATE_A)

        evidence = reader.evidence_for(_entry(), repository_host=Host())

        assert evidence.publication_certified is True
        assert evidence.establishes_independent_review is True
        assert evidence.gap == ""

    def test_a_pull_request_with_no_issue_association_is_refused(
        self, tmp_path: Path
    ) -> None:
        reader, attempts = _reader(tmp_path)
        _record_verdict(attempts, reviewed_sha=CANDIDATE_A)

        evidence = reader.evidence_for(
            _entry(branch="hotfix-no-issue"), repository_host=Host()
        )

        assert evidence.establishes_independent_review is False
        assert "names no issue" in evidence.gap


class TestStaging:
    def test_the_set_is_written_beside_the_manifest_for_the_agent_to_read(
        self, tmp_path: Path
    ) -> None:
        reader, attempts = _reader(tmp_path)
        _record_verdict(attempts, reviewed_sha=CANDIDATE_A)
        data_dir = tmp_path / "tech-lead-data"

        path = write_candidate_evidence(
            data_dir,
            build_candidate_evidence(
                [_entry()], source=reader, repository_host=Host()
            ),
        )

        assert path.name == TECH_LEAD_CANDIDATE_EVIDENCE_FILENAME
        assert path.parent == data_dir
        assert CANDIDATE_A in path.read_text()

    def test_a_composition_without_an_evidence_source_says_so_per_candidate(
        self, tmp_path: Path
    ) -> None:
        """The null object NAMES the omission; silence would read as a refusal."""
        evidence = build_candidate_evidence(
            [_entry()],
            source=NO_TECH_LEAD_CANDIDATE_EVIDENCE,
            repository_host=Host(),
        )

        [entry] = evidence.entries
        assert entry.establishes_independent_review is False
        assert "no exact-candidate review evidence source is wired" in entry.gap
