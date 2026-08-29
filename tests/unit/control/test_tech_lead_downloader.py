"""Unit tests for TechLeadDownloader."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from issue_orchestrator.execution.tech_lead_downloader import TechLeadDownloader
from issue_orchestrator.domain.tech_lead_candidate import CandidatePassPrerequisite
from issue_orchestrator.domain.tech_lead_manifest import (
    TechLeadManifest,
    PRToReview,
)


@dataclass
class MockPR:
    """Mock PR object for testing."""

    number: int
    title: str
    url: str
    branch: str
    labels: list[str]
    body: Optional[str] = None
    state: str = "open"
    head_sha: Optional[str] = None


@dataclass
class CommandResult:
    """Mock command result."""

    returncode: int
    stdout: str
    stderr: str


class MockRepositoryHost:
    """Mock RepositoryHost for testing."""

    def __init__(self, prs: dict[int, MockPR] | None = None):
        self._prs = prs or {}
        self.get_pr_calls: list[int] = []

    def get_pr(self, pr_number: int) -> Optional[MockPR]:
        self.get_pr_calls.append(pr_number)
        return self._prs.get(pr_number)


class MockCommandRunner:
    """Mock CommandRunner for testing."""

    def __init__(self, results: dict[str, CommandResult] | None = None):
        self._results = results or {}
        self.run_calls: list[list[str]] = []

    def run(self, args: list[str]) -> CommandResult:
        self.run_calls.append(args)
        # Match by PR number in args
        for arg in args:
            if arg in self._results:
                return self._results[arg]
        # Default success result
        return CommandResult(returncode=0, stdout="", stderr="")


def _sha(pr_number: int) -> str:
    """A distinct full-length candidate commit per PR."""
    return f"{pr_number:03d}".ljust(40, "a")


class TestTechLeadDownloader:
    """Tests for TechLeadDownloader."""

    def test_download_empty_manifest(self, tmp_path: Path):
        """Handles empty manifest gracefully."""
        host = MockRepositoryHost()
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(data_dir="tech-lead-data", prs=[])
        result = downloader.download(manifest, tmp_path)

        assert result.prs == []
        assert len(host.get_pr_calls) == 0
        assert len(runner.run_calls) == 0

    def test_download_requires_data_dir(self, tmp_path: Path):
        """Raises error if data_dir not set."""
        host = MockRepositoryHost()
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(data_dir="", prs=[
            PRToReview(number=1, title="PR", url="u", branch="b", head_sha=_sha(1)),
        ])

        try:
            downloader.download(manifest, tmp_path)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "data_dir" in str(e)

    def test_download_creates_diff_file(self, tmp_path: Path):
        """Downloads and writes diff for each PR."""
        host = MockRepositoryHost(prs={
            42: MockPR(number=42, title="Test PR", url="https://github.com/org/repo/pull/42", branch="test", labels=[], head_sha=_sha(42)),
        })
        runner = MockCommandRunner(results={
            "42": CommandResult(
                returncode=0,
                stdout="diff --git a/file.py b/file.py\n+added line",
                stderr="",
            ),
        })
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="tech-lead-data",
            prs=[PRToReview(number=42, title="Test PR", url="u", branch="b", head_sha=_sha(42))],
        )
        result = downloader.download(manifest, tmp_path)

        # Check diff file was created
        diff_path = tmp_path / "tech-lead-data" / "pr-42-042aaaaaaaaa-diff.txt"
        assert diff_path.exists()
        assert "diff --git" in diff_path.read_text()

        # Check manifest was updated
        assert result.prs[0].files.diff == "pr-42-042aaaaaaaaa-diff.txt"

    def test_download_creates_metadata_file(self, tmp_path: Path):
        """Downloads and writes metadata for each PR."""
        host = MockRepositoryHost(prs={
            42: MockPR(
                number=42,
                title="Test PR",
                url="https://github.com/org/repo/pull/42",
                branch="test-branch",
                labels=["bug", "priority"],
                body="PR description here",
                state="open",
                head_sha=_sha(42),
            ),
        })
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="tech-lead-data",
            prs=[PRToReview(number=42, title="Test PR", url="u", branch="b", head_sha=_sha(42))],
        )
        result = downloader.download(manifest, tmp_path)

        # Check metadata file was created
        meta_path = tmp_path / "tech-lead-data" / "pr-42-042aaaaaaaaa-meta.json"
        assert meta_path.exists()

        metadata = json.loads(meta_path.read_text())
        assert metadata["number"] == 42
        assert metadata["title"] == "Test PR"
        assert metadata["body"] == "PR description here"
        assert metadata["branch"] == "test-branch"
        assert "bug" in metadata["labels"]
        assert metadata["state"] == "open"

        # Check manifest was updated
        assert result.prs[0].files.metadata == "pr-42-042aaaaaaaaa-meta.json"

    def test_download_handles_diff_error(self, tmp_path: Path):
        """Writes error message when diff fetch fails."""
        host = MockRepositoryHost(prs={
            99: MockPR(number=99, title="PR", url="u", branch="b", labels=[], head_sha=_sha(99)),
        })
        runner = MockCommandRunner(results={
            "99": CommandResult(
                returncode=1,
                stdout="",
                stderr="gh: PR not found",
            ),
        })
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=99, title="PR", url="u", branch="b", head_sha=_sha(99))],
        )
        downloader.download(manifest, tmp_path)

        diff_path = tmp_path / "data" / "pr-99-099aaaaaaaaa-diff.txt"
        assert diff_path.exists()
        content = diff_path.read_text()
        assert "Error fetching diff" in content
        assert "PR not found" in content

    def test_download_handles_missing_pr(self, tmp_path: Path):
        """Writes error metadata when PR not found."""
        host = MockRepositoryHost(prs={})  # No PRs
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=999, title="Missing", url="u", branch="b", head_sha=_sha(999))],
        )
        downloader.download(manifest, tmp_path)

        meta_path = tmp_path / "data" / "pr-999-999aaaaaaaaa-meta.json"
        assert meta_path.exists()
        metadata = json.loads(meta_path.read_text())
        assert "error" in metadata
        assert "999" in metadata["error"]

    def test_download_multiple_prs(self, tmp_path: Path):
        """Downloads data for multiple PRs."""
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[], head_sha=_sha(1)),
            2: MockPR(number=2, title="PR 2", url="u2", branch="b2", labels=[], head_sha=_sha(2)),
            3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[], head_sha=_sha(3)),
        })
        runner = MockCommandRunner(results={
            "1": CommandResult(0, "diff1", ""),
            "2": CommandResult(0, "diff2", ""),
            "3": CommandResult(0, "diff3", ""),
        })
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[
                PRToReview(number=1, title="PR 1", url="u1", branch="b1", head_sha=_sha(1)),
                PRToReview(number=2, title="PR 2", url="u2", branch="b2", head_sha=_sha(2)),
                PRToReview(number=3, title="PR 3", url="u3", branch="b3", head_sha=_sha(3)),
            ],
        )
        result = downloader.download(manifest, tmp_path)

        # Check all files created
        assert (tmp_path / "data" / "pr-1-001aaaaaaaaa-diff.txt").exists()
        assert (tmp_path / "data" / "pr-2-002aaaaaaaaa-diff.txt").exists()
        assert (tmp_path / "data" / "pr-3-003aaaaaaaaa-diff.txt").exists()
        assert (tmp_path / "data" / "pr-1-001aaaaaaaaa-meta.json").exists()
        assert (tmp_path / "data" / "pr-2-002aaaaaaaaa-meta.json").exists()
        assert (tmp_path / "data" / "pr-3-003aaaaaaaaa-meta.json").exists()

        # Check manifest updated
        assert result.prs[0].files.diff == "pr-1-001aaaaaaaaa-diff.txt"
        assert result.prs[1].files.diff == "pr-2-002aaaaaaaaa-diff.txt"
        assert result.prs[2].files.diff == "pr-3-003aaaaaaaaa-diff.txt"

    def test_download_continues_on_pr_failure(self, tmp_path: Path):
        """Continues downloading other PRs even if one fails."""
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[], head_sha=_sha(1)),
            # PR 2 missing
            3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[], head_sha=_sha(3)),
        })
        runner = MockCommandRunner(results={
            "1": CommandResult(0, "diff1", ""),
            "2": CommandResult(1, "", "not found"),
            "3": CommandResult(0, "diff3", ""),
        })
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[
                PRToReview(number=1, title="PR 1", url="u1", branch="b1", head_sha=_sha(1)),
                PRToReview(number=2, title="PR 2", url="u2", branch="b2", head_sha=_sha(2)),
                PRToReview(number=3, title="PR 3", url="u3", branch="b3", head_sha=_sha(3)),
            ],
        )
        downloader.download(manifest, tmp_path)

        # PR 1 and 3 should have proper files
        assert (tmp_path / "data" / "pr-1-001aaaaaaaaa-diff.txt").exists()
        assert (tmp_path / "data" / "pr-3-003aaaaaaaaa-diff.txt").exists()
        assert "diff1" in (tmp_path / "data" / "pr-1-001aaaaaaaaa-diff.txt").read_text()
        assert "diff3" in (tmp_path / "data" / "pr-3-003aaaaaaaaa-diff.txt").read_text()

    def test_download_creates_data_directory(self, tmp_path: Path):
        """Creates data directory if it doesn't exist."""
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR", url="u", branch="b", labels=[], head_sha=_sha(1)),
        })
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="deep/nested/tech-lead-data",
            prs=[PRToReview(number=1, title="PR", url="u", branch="b", head_sha=_sha(1))],
        )
        downloader.download(manifest, tmp_path)

        assert (tmp_path / "deep" / "nested" / "tech-lead-data").exists()
        assert (tmp_path / "deep" / "nested" / "tech-lead-data" / "pr-1-001aaaaaaaaa-diff.txt").exists()

    def test_download_calls_gh_pr_diff(self, tmp_path: Path):
        """Calls gh pr diff with correct arguments."""
        host = MockRepositoryHost(prs={
            42: MockPR(number=42, title="PR", url="u", branch="b", labels=[], head_sha=_sha(42)),
        })
        runner = MockCommandRunner()
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=42, title="PR", url="u", branch="b", head_sha=_sha(42))],
        )
        downloader.download(manifest, tmp_path)

        assert len(runner.run_calls) == 1
        assert runner.run_calls[0] == ["gh", "pr", "diff", "42"]


class TestCandidateBinding:
    """Materialized content is bound to the manifest's candidate commit (#345).

    The diff transport names a pull request, not a commit, so the binding is
    proved by bracketing: the manifest recorded the head that selected the PR,
    and the metadata read that follows the fetch observes it again. When the two
    disagree, the bytes are about other work and are not filed under this
    candidate's name at all.
    """

    def test_a_head_that_moved_during_the_fetch_stages_no_diff(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7,
                title="PR",
                url="u",
                branch="b",
                labels=[],
                head_sha=_sha(8),  # moved away from the manifest's candidate
            ),
        })
        runner = MockCommandRunner(results={"7": CommandResult(0, "diff7", "")})
        downloader = TechLeadDownloader(host, runner)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        result = downloader.download(manifest, tmp_path)

        assert result.prs[0].files.diff == ""
        assert not list((tmp_path / "data").glob("*-diff.txt"))

    def test_the_metadata_records_why_the_content_could_not_be_bound(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(8)
            ),
        })
        downloader = TechLeadDownloader(host, MockCommandRunner())

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        downloader.download(manifest, tmp_path)

        metadata = json.loads(
            (tmp_path / "data" / f"pr-7-{_sha(7)[:12]}-meta.json").read_text()
        )
        assert metadata["candidate_bound"] is False
        assert metadata["candidate_sha"] == _sha(7)
        assert "moved" in metadata["candidate_binding_gap"]

    def test_a_still_current_head_binds_the_content_to_the_candidate(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(7)
            ),
        })
        downloader = TechLeadDownloader(host, MockCommandRunner())

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        downloader.download(manifest, tmp_path)

        metadata = json.loads(
            (tmp_path / "data" / f"pr-7-{_sha(7)[:12]}-meta.json").read_text()
        )
        assert metadata["candidate_bound"] is True
        assert "candidate_binding_gap" not in metadata

    def test_a_pull_request_selected_without_a_head_stages_no_diff(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(7)
            ),
        })
        downloader = TechLeadDownloader(host, MockCommandRunner())

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b")],
        )
        result = downloader.download(manifest, tmp_path)

        assert result.prs[0].files.diff == ""
        metadata = json.loads((tmp_path / "data" / "pr-7-unknown-meta.json").read_text())
        assert metadata["candidate_bound"] is False


class TestStagedReviewEvidence:
    """The reviewer's exact-commit verdict is staged, and its answer recorded.

    The agent may not fetch this context itself, and the orchestrator may not
    take the agent's word for it later — so the same staging pass writes the
    file the session reads AND the fact the launch authority carries (#345).
    """

    class _Source:
        def __init__(self, established: bool) -> None:
            self._established = established
            self.asked: list[int] = []

        def evidence_for(self, entry, *, repository_host):
            from issue_orchestrator.domain.tech_lead_candidate import (
                TechLeadCandidateEvidence,
            )

            self.asked.append(entry.number)
            return TechLeadCandidateEvidence(
                candidate=entry.candidate(),
                gap="" if self._established else "no verdict for this commit",
            )

    def _download(self, tmp_path: Path, source) -> TechLeadManifest:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(7)
            ),
        })
        downloader = TechLeadDownloader(host, MockCommandRunner(), source)
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        return downloader.download(manifest, tmp_path)

    def test_the_evidence_file_is_staged_beside_the_manifest(
        self, tmp_path: Path
    ) -> None:
        source = self._Source(established=True)

        self._download(tmp_path, source)

        staged = json.loads((tmp_path / "data" / "candidate-evidence.json").read_text())
        assert source.asked == [7]
        assert staged["candidates"][0]["candidate_sha"] == _sha(7)

    def test_an_established_approval_is_recorded_on_the_manifest_entry(
        self, tmp_path: Path
    ) -> None:
        manifest = self._download(tmp_path, self._Source(established=True))

        assert manifest.prs[0].review_established is True
        assert manifest.reviewed_candidates() == manifest.candidates()

    def test_a_gap_leaves_the_candidate_unreviewed(self, tmp_path: Path) -> None:
        manifest = self._download(tmp_path, self._Source(established=False))

        assert manifest.prs[0].review_established is False
        assert manifest.reviewed_candidates() == ()

    def test_the_reason_rides_along_with_the_refusal(self, tmp_path: Path) -> None:
        """The staged file dies with this worktree; the refusal outlives it.

        The receipt that has to explain the refusal is published from the
        completion lane, after cleanup has disposed of
        ``candidate-evidence.json`` — so the reason travels on the manifest
        entry into the launch authority, not only into the file.
        """
        manifest = self._download(tmp_path, self._Source(established=False))

        assert manifest.prs[0].review_gap == "no verdict for this commit"
        [gap] = manifest.prerequisite_gaps()
        assert gap.candidate == manifest.candidates()[0]
        assert gap.prerequisite is CandidatePassPrerequisite.INDEPENDENT_REVIEW
        assert gap.reason == "no verdict for this commit"

    def test_an_established_candidate_carries_no_reason_to_explain(
        self, tmp_path: Path
    ) -> None:
        """Nothing to explain, so nothing recorded: a met prerequisite has no gap."""
        manifest = self._download(tmp_path, self._Source(established=True))

        assert manifest.prs[0].review_gap == ""
        assert manifest.prerequisite_gaps() == ()

    def test_a_composition_with_no_source_stages_the_omission(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(7)
            ),
        })
        downloader = TechLeadDownloader(host, MockCommandRunner())
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )

        result = downloader.download(manifest, tmp_path)

        staged = json.loads((tmp_path / "data" / "candidate-evidence.json").read_text())
        assert "no exact-candidate review evidence source" in staged["candidates"][0]["gap"]
        assert result.prs[0].review_established is False
