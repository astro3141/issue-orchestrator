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


def _reader(tmp_path: Path) -> tuple[DurableCandidateEvidence, SidecarAttemptStore]:
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
        profiles=config.validation_profiles(),
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
