"""Mandatory repository validation is the orchestrator's, per candidate (#370).

R30 (#364) proved the seam this closes. The Tech Lead consumed the exact
candidate contract and rendered a candidate-bound PASS, and then could not
complete: the repository validation its completion required needed
host/repository-owned effects — a write to the shared git common dir among them
— outside the model-provider sandbox's scratch write boundary. Widening the
sandbox to admit them is the authority grant the repair had to avoid.

So the ownership moved instead, and this file proves the half that has to hold
once it did: a merge-facing PASS rests on the repository's mandatory validation
having passed on the SAME exact commit the Tech Lead adjudicated, established
by the orchestrator's own publication gate and carried on the launch authority
where the session cannot reach it.

The routing half — that no Tech Lead session runs the gate itself — is proved
at its own seam in ``tests/unit/control/test_completion_gate_routing.py`` and
``tests/unit/entrypoints/test_coding_done_planning_gate.py``. What is proved
HERE is that the orchestrator's verdict travels the whole way, and that every
direction in which it is absent, stale, failed, or unreadable refuses the PASS
rather than becoming candidate evidence.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.publication_authority import (
    PublicationVerdictReader,
    UnrecordedRefusals,
)
from issue_orchestrator.control.tech_lead_candidate_evidence import (
    DurableCandidateEvidence,
)
from issue_orchestrator.domain.attempt import AttemptKey
from issue_orchestrator.domain.board_snapshot import BoardSnapshot
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.review_verdict_binding import (
    BoundReviewVerdict,
    ReviewVerdictOutcome,
)
from issue_orchestrator.domain.tech_lead_candidate import (
    CandidatePassPrerequisite,
    TechLeadCandidate,
)
from issue_orchestrator.domain.tech_lead_manifest import TechLeadManifest
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)
from issue_orchestrator.entrypoints.bootstrap_completion import (
    _validation_attempt_key_factory,
)
from issue_orchestrator.execution.attempt_review_verdict_store import (
    AttemptReviewVerdictStore,
)
from issue_orchestrator.execution.tech_lead_downloader import TechLeadDownloader
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
)
from issue_orchestrator.infra.validation_profiles import ValidationProfileRegistry
from issue_orchestrator.ports.pull_request_tracker import PullRequestDiffRead
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)

REPO = "owner/repo"
CANDIDATE = "a" * 40
OTHER = "b" * 40
LEAF_ISSUE = 42

PUBLISH_COMMAND = "make publish"
PUBLISH_PROFILE = "default"


class _Host:
    """The reads this launch path makes, and nothing else."""

    def __init__(self, prs: dict[int, SimpleNamespace]) -> None:
        self._prs = prs

    def get_prs_with_label(self, label: str, state: str = "open"):
        return [pr for pr in self._prs.values() if pr.state == state]

    def get_pr(self, pr_number: int):
        return self._prs.get(pr_number)

    def read_pr_diff(self, pr_number: int) -> PullRequestDiffRead:
        return PullRequestDiffRead.readable("diff --git a/a.py b/a.py\n+x\n")

    def get_issue(self, issue_number: int):
        return None

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return []

    def create_issue_key(self, issue_number: int) -> GitHubIssueKey:
        return GitHubIssueKey(repo=REPO, external_id=str(issue_number))


def _pr(number: int, head_sha: str) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title=f"candidate {number}",
        url=f"https://example.invalid/pull/{number}",
        branch=f"{LEAF_ISSUE}-candidate-{number}",
        head_sha=head_sha,
        labels=["code-reviewed"],
        state="open",
        body="",
    )


def _config(tmp_path: Path) -> Config:
    """A repository that HAS a publication contract, so a candidate can miss it.

    Without one, ``CandidatePublicationEvidence`` admits every candidate —
    correctly, because there is no gate a candidate could have cleared — and
    the prerequisite this file is about could never refuse.
    """
    config = Config(repo=REPO)
    config.tech_lead_review_agent = "agent:tech-lead"
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir(parents=True, exist_ok=True)
    return config


def _profiles() -> ValidationProfileRegistry:
    return ValidationProfileRegistry(
        ValidationConfig(
            quick=ValidationCommandConfig(cmd="make quick", timeout_seconds=111),
            publish=PublishValidationConfig(
                cmd=PUBLISH_COMMAND, timeout_seconds=222
            ),
        )
    )


def _key(head_sha: str, *, issue_number: int = LEAF_ISSUE) -> AttemptKey:
    return AttemptKey(
        GitHubIssueKey(repo=REPO, external_id=str(issue_number)), head_sha
    )


def _approve(attempts: SidecarAttemptStore, *, head_sha: str) -> None:
    """The reviewer half, always established here.

    Every test in this file is about the OTHER prerequisite, so the reviewer's
    is held constant: a refusal that could be either one proves neither.
    """
    AttemptReviewVerdictStore(attempts).record(
        _key(head_sha),
        BoundReviewVerdict(
            verdict=ReviewVerdictOutcome.APPROVED,
            reviewed_sha=head_sha,
            decided_at="2026-08-30T00:00:00+00:00",
            completed_rounds=1,
        ),
    )


def _file_validation(
    attempts: SidecarAttemptStore,
    *,
    head_sha: str,
    verdict: ValidationVerdict = ValidationVerdict.PASSED,
    command: str = PUBLISH_COMMAND,
    profile: str = PUBLISH_PROFILE,
) -> None:
    """File the orchestrator's own publication-gate receipt for one commit.

    Written through the same durable record the gate writes, so what these
    tests read back is what a real run would have left — never a fixture
    shortcut around the reader under test.
    """
    attempts.update(
        _key(head_sha),
        lambda attempt: replace(
            attempt,
            completed_evaluations=(
                *attempt.completed_evaluations,
                ValidationVerdictReceipt(
                    suite="publish_gate",
                    head_sha=head_sha,
                    verdict=verdict,
                    command=command,
                    profile=profile,
                ),
            ),
        ),
    )


def _launch(
    tmp_path: Path, host: _Host, attempts: SidecarAttemptStore
) -> tuple[TechLeadLaunchAuthority, Path]:
    """Run the real launch path and return the authority it recorded."""
    from issue_orchestrator.control.tech_lead_session_policy import (
        prepare_tech_lead_session_data,
    )

    config = _config(tmp_path)
    evidence = DurableCandidateEvidence(
        review_verdicts=AttemptReviewVerdictStore(attempts),
        execution_identities=SimpleNamespace(read=lambda key: None),
        publication_verdict=PublicationVerdictReader.over(
            UnrecordedRefusals.process_local(),
            attempts,
            _validation_attempt_key_factory(config),
        ),
        profiles=_profiles(),
    )
    worktree = tmp_path / "worktree"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run"
    run_dir.mkdir(parents=True)
    store = InMemoryTechLeadAuthorityStore()
    prepare_tech_lead_session_data(
        config=config,
        repository_host=host,
        manifest_downloader=TechLeadDownloader(
            repository_host=host, candidate_evidence=evidence
        ),
        tech_lead_authority=store,
        board_snapshot_provider=SimpleNamespace(
            snapshot=lambda focus, problems=(): BoardSnapshot(
                generated_at="2026-08-30T00:00:00Z", orchestrator_paused=False
            )
        ),
        working_copy=SimpleNamespace(get_head_sha=lambda worktree: "e" * 40),
        planning_command_guard=SimpleNamespace(),
        issue=SimpleNamespace(
            number=7,
            title="Tech lead batch",
            agent_type="agent:tech-lead",
            labels=[],
        ),
        ctx=SimpleNamespace(
            run=SimpleNamespace(
                run_dir=run_dir, run_id="run-1", session_name="issue-7"
            ),
            worktree_path=worktree,
            update_manifest=lambda entries: None,
        ),
        tech_lead_scope=None,
    )
    authority = store.load(run_id="run-1", session_name="issue-7")
    assert authority is not None
    return authority, run_dir / "tech-lead-data"


def _unmet(authority: TechLeadLaunchAuthority, pr_number: int):
    candidate = authority.candidate_for(pr_number)
    assert candidate is not None
    return {
        unmet.prerequisite: unmet.recorded_reason
        for unmet in authority.unmet_pass_prerequisites(candidate)
    }


class TestTheOrchestratorsVerdictReachesTheLaunchAuthority:
    """F3: validation is bound to the exact candidate the Tech Lead audits."""

    def _launch(self, tmp_path: Path, *, validated_sha: str = CANDIDATE):
        attempts = SidecarAttemptStore(tmp_path)
        _approve(attempts, head_sha=CANDIDATE)
        _file_validation(attempts, head_sha=validated_sha)
        return _launch(tmp_path, _Host(prs={101: _pr(101, CANDIDATE)}), attempts)

    def test_a_certified_commit_establishes_the_prerequisite(
        self, tmp_path: Path
    ) -> None:
        authority, _ = self._launch(tmp_path)

        assert authority.validated_candidates == (
            TechLeadCandidate(101, CANDIDATE),
        )
        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION not in _unmet(
            authority, 101
        )

    def test_the_manifest_the_session_reads_says_the_same_thing(
        self, tmp_path: Path
    ) -> None:
        """The session can see which prerequisite it is short of.

        It cannot establish this one — it does not run validation — so the
        staged answer is the only way the prompt's "do not pass a candidate
        with a gap" rule is actionable from inside the sandbox.
        """
        _, data_dir = self._launch(tmp_path)

        manifest = TechLeadManifest.read(data_dir / "manifest.json")
        [entry] = manifest.prs
        assert entry.validation_established is True
        assert entry.validation_gap == ""

    def test_a_receipt_for_a_different_commit_certifies_nothing(
        self, tmp_path: Path
    ) -> None:
        """Drift, in the direction that matters: validated, but not THIS one.

        The reviewer approved the candidate and a publication gate passed —
        on some other commit. Binding is what makes the difference between
        evidence and coincidence.
        """
        authority, _ = self._launch(tmp_path, validated_sha=OTHER)

        assert authority.validated_candidates == ()
        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION in _unmet(
            authority, 101
        )
        # And the reviewer is not blamed for it.
        assert CandidatePassPrerequisite.INDEPENDENT_REVIEW not in _unmet(
            authority, 101
        )


class TestEveryValidationFailureDirectionRefusesThePass:
    """F4: nonzero, absent, stale-contract, drifted — none may certify."""

    def _launch(self, tmp_path: Path, prepare) -> TechLeadLaunchAuthority:
        attempts = SidecarAttemptStore(tmp_path)
        _approve(attempts, head_sha=CANDIDATE)
        prepare(attempts)
        authority, _ = _launch(
            tmp_path, _Host(prs={101: _pr(101, CANDIDATE)}), attempts
        )
        return authority

    def test_no_validation_at_all_refuses(self, tmp_path: Path) -> None:
        authority = self._launch(tmp_path, lambda attempts: None)

        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION in _unmet(
            authority, 101
        )

    def test_a_failed_validation_refuses(self, tmp_path: Path) -> None:
        authority = self._launch(
            tmp_path,
            lambda attempts: _file_validation(
                attempts, head_sha=CANDIDATE, verdict=ValidationVerdict.FAILED
            ),
        )

        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION in _unmet(
            authority, 101
        )

    def test_a_receipt_from_a_contract_no_longer_required_refuses(
        self, tmp_path: Path
    ) -> None:
        """A validation that ran, passed, and proves nothing about today.

        The receipt names the command it executed. If that is not the command
        the publication contract now requires, the run certified a different
        contract — which is not the one a merge-facing PASS rests on.
        """
        authority = self._launch(
            tmp_path,
            lambda attempts: _file_validation(
                attempts, head_sha=CANDIDATE, command="make something-else"
            ),
        )

        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION in _unmet(
            authority, 101
        )

    def test_the_refusal_carries_the_reason_the_owner_recorded(
        self, tmp_path: Path
    ) -> None:
        """Not a fixed sentence: the operator's only instruction.

        Nothing in this codebase removes the terminal label the refusal
        applies, so the receipt has to name the condition the validation owner
        actually observed rather than the one the prerequisite is usually
        about.
        """
        authority = self._launch(tmp_path, lambda attempts: None)

        reason = _unmet(authority, 101)[
            CandidatePassPrerequisite.REPOSITORY_VALIDATION
        ]
        assert CANDIDATE[:12] in reason
        assert "publication-gate certification" in reason
        assert "executed by the orchestrator" in reason

    def test_a_refused_candidate_leaves_its_certified_sibling_alone(
        self, tmp_path: Path
    ) -> None:
        """Per candidate, like every other prerequisite."""
        attempts = SidecarAttemptStore(tmp_path)
        _approve(attempts, head_sha=CANDIDATE)
        _approve(attempts, head_sha=OTHER)
        _file_validation(attempts, head_sha=CANDIDATE)
        authority, _ = _launch(
            tmp_path,
            _Host(prs={101: _pr(101, CANDIDATE), 102: _pr(102, OTHER)}),
            attempts,
        )

        assert [c.pr_number for c in authority.validated_candidates] == [101]
        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION not in _unmet(
            authority, 101
        )
        assert CandidatePassPrerequisite.REPOSITORY_VALIDATION in _unmet(
            authority, 102
        )


class TestALegacyRowCertifiesNothing:
    """A run that never recorded the fact cannot have established it."""

    def test_an_authority_written_before_this_prerequisite_holds_none(self) -> None:
        legacy = TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=7,
            manifest_pr_numbers=(101,),
            manifest_candidates=(TechLeadCandidate(101, CANDIDATE),),
            reviewed_candidates=(TechLeadCandidate(101, CANDIDATE),),
            contracted_candidates=(TechLeadCandidate(101, CANDIDATE),),
            diffed_candidates=(TechLeadCandidate(101, CANDIDATE),),
        ).to_dict()
        del legacy["validated_candidates"]

        restored = TechLeadLaunchAuthority.from_dict(legacy)

        assert restored.validated_candidates == ()
        assert [
            unmet.prerequisite
            for unmet in restored.unmet_pass_prerequisites(
                TechLeadCandidate(101, CANDIDATE)
            )
        ] == [CandidatePassPrerequisite.REPOSITORY_VALIDATION]
