"""Unit tests for TechLeadDownloader."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from issue_orchestrator.execution.tech_lead_downloader import (
    CandidateMaterialization,
    TechLeadDownloader,
)
from issue_orchestrator.domain.tech_lead_candidate import CandidatePassPrerequisite
from issue_orchestrator.domain.tech_lead_manifest import (
    TechLeadManifest,
    PRFiles,
    PRToReview,
)
from issue_orchestrator.ports.pull_request_tracker import PullRequestDiffRead


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


class MockRepositoryHost:
    """Mock RepositoryHost for testing.

    ``diffs`` maps a PR number to the typed outcome its diff read produces.
    A PR with no entry reads as an ordinary, readable diff — the happy path
    most of these tests are not about.
    """

    def __init__(
        self,
        prs: dict[int, MockPR] | None = None,
        diffs: dict[int, PullRequestDiffRead] | None = None,
    ):
        self._prs = prs or {}
        self._diffs = diffs or {}
        self.get_pr_calls: list[int] = []
        self.diff_calls: list[int] = []

    def get_pr(self, pr_number: int) -> Optional[MockPR]:
        self.get_pr_calls.append(pr_number)
        return self._prs.get(pr_number)

    def read_pr_diff(self, pr_number: int) -> PullRequestDiffRead:
        self.diff_calls.append(pr_number)
        if pr_number in self._diffs:
            return self._diffs[pr_number]
        return PullRequestDiffRead.readable(
            f"diff --git a/pr{pr_number}.py b/pr{pr_number}.py\n+line\n"
        )


def _diff_gaps(manifest: TechLeadManifest):
    """Only the recorded CANDIDATE_DIFF reasons.

    These fixtures wire no reviewer-evidence source, so every candidate also
    carries the INDEPENDENT_REVIEW gap that omission records (#345). Filtering
    keeps each assertion about the prerequisite it is actually testing.
    """
    return [
        gap
        for gap in manifest.prerequisite_gaps()
        if gap.prerequisite is CandidatePassPrerequisite.CANDIDATE_DIFF
    ]


def _sha(pr_number: int) -> str:
    """A distinct full-length candidate commit per PR."""
    return f"{pr_number:03d}".ljust(40, "a")


class TestTechLeadDownloader:
    """Tests for TechLeadDownloader."""

    def test_download_empty_manifest(self, tmp_path: Path):
        """Handles empty manifest gracefully."""
        host = MockRepositoryHost()
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(data_dir="tech-lead-data", prs=[])
        result = downloader.download(manifest, tmp_path)

        assert result.prs == []
        assert len(host.get_pr_calls) == 0
        assert host.diff_calls == []

    def test_download_requires_data_dir(self, tmp_path: Path):
        """Raises error if data_dir not set."""
        host = MockRepositoryHost()
        downloader = TechLeadDownloader(host)

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
        host = MockRepositoryHost(
            prs={
                42: MockPR(number=42, title="Test PR", url="https://github.com/org/repo/pull/42", branch="test", labels=[], head_sha=_sha(42)),
            },
            diffs={
                42: PullRequestDiffRead.readable(
                    "diff --git a/file.py b/file.py\n+added line"
                ),
            },
        )
        downloader = TechLeadDownloader(host)

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
        downloader = TechLeadDownloader(host)

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

    def test_download_handles_missing_pr(self, tmp_path: Path):
        """Writes error metadata when PR not found."""
        host = MockRepositoryHost(prs={})  # No PRs
        downloader = TechLeadDownloader(host)

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
        host = MockRepositoryHost(
            prs={
                1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[], head_sha=_sha(1)),
                2: MockPR(number=2, title="PR 2", url="u2", branch="b2", labels=[], head_sha=_sha(2)),
                3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[], head_sha=_sha(3)),
            },
            diffs={
                1: PullRequestDiffRead.readable("diff1"),
                2: PullRequestDiffRead.readable("diff2"),
                3: PullRequestDiffRead.readable("diff3"),
            },
        )
        downloader = TechLeadDownloader(host)

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

    def test_download_creates_data_directory(self, tmp_path: Path):
        """Creates data directory if it doesn't exist."""
        host = MockRepositoryHost(prs={
            1: MockPR(number=1, title="PR", url="u", branch="b", labels=[], head_sha=_sha(1)),
        })
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="deep/nested/tech-lead-data",
            prs=[PRToReview(number=1, title="PR", url="u", branch="b", head_sha=_sha(1))],
        )
        downloader.download(manifest, tmp_path)

        assert (tmp_path / "deep" / "nested" / "tech-lead-data").exists()
        assert (tmp_path / "deep" / "nested" / "tech-lead-data" / "pr-1-001aaaaaaaaa-diff.txt").exists()

    def test_download_reads_the_diff_through_the_repository_host(
        self, tmp_path: Path
    ):
        """The candidate diff comes from the supported transport (#359).

        The mutation direction of the R29 production defect: this downloader
        used to run ``gh pr diff`` through a CommandRunner. It now has no
        command runner at all, so restoring that call cannot even be wired —
        and the read it does make is the repository host's own.
        """
        host = MockRepositoryHost(prs={
            42: MockPR(number=42, title="PR", url="u", branch="b", labels=[], head_sha=_sha(42)),
        })
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=42, title="PR", url="u", branch="b", head_sha=_sha(42))],
        )
        downloader.download(manifest, tmp_path)

        assert host.diff_calls == [42]
        assert not hasattr(downloader, "_runner")


class TestNoDirectGhSeamSurvives:
    """The candidate-diff path holds no subprocess seam at all (#359 G).

    A behavioural test cannot observe the absence of a call that is never
    made, so this reads the module: restoring ``gh pr diff`` — or merely
    re-accepting a ``CommandRunner`` to run it with — fails here.
    """

    @staticmethod
    def _module_ast():
        import ast
        import inspect

        from issue_orchestrator.execution import tech_lead_downloader

        return ast.parse(inspect.getsource(tech_lead_downloader))

    def test_the_module_names_no_gh_executable(self) -> None:
        """Parsed, not grepped: the prose may DISCUSS the removed call."""
        import ast

        literals = {
            node.value
            for node in ast.walk(self._module_ast())
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        assert "gh" not in literals

    def test_the_module_imports_no_command_runner(self) -> None:
        import ast

        imported = {
            alias.name
            for node in ast.walk(self._module_ast())
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        assert "CommandRunner" not in imported

    def test_the_downloader_accepts_no_command_runner(self) -> None:
        import inspect

        parameters = inspect.signature(TechLeadDownloader.__init__).parameters

        assert "command_runner" not in parameters


class TestUnreadableDiffIsUnavailableNotContent:
    """A failed diff read is an explicit gap, never bytes on disk (#359).

    The R29 live batch proved the fail-open seam: the direct-``gh`` guard
    refused the invocation, the refusal text was written into
    ``pr-<n>-<sha12>-diff.txt``, and the manifest advertised that file as the
    candidate's diff. Nothing here may write a transport failure under the
    candidate's name.
    """

    def _download(
        self, tmp_path: Path, diff: PullRequestDiffRead
    ) -> TechLeadManifest:
        host = MockRepositoryHost(
            prs={
                7: MockPR(
                    number=7, title="PR", url="u", branch="b", labels=[],
                    head_sha=_sha(7),
                ),
            },
            diffs={7: diff},
        )
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        return TechLeadDownloader(host).download(manifest, tmp_path)

    def test_a_transport_failure_writes_no_diff_file(self, tmp_path: Path) -> None:
        self._download(
            tmp_path,
            PullRequestDiffRead.unavailable(
                "the GitHub diff read for PR #7 failed: 502 Bad Gateway"
            ),
        )

        assert not list((tmp_path / "data").glob("*-diff.txt"))

    def test_the_manifest_advertises_no_diff_filename(self, tmp_path: Path) -> None:
        manifest = self._download(
            tmp_path,
            PullRequestDiffRead.unavailable(
                "the GitHub diff read for PR #7 failed: 502 Bad Gateway"
            ),
        )

        assert manifest.prs[0].files.diff == ""
        # The metadata is still staged: the candidate is audited, it simply has
        # no code to audit.
        assert manifest.prs[0].files.metadata == "pr-7-007aaaaaaaaa-meta.json"

    def test_the_observed_reason_is_recorded_on_the_candidate(
        self, tmp_path: Path
    ) -> None:
        manifest = self._download(
            tmp_path,
            PullRequestDiffRead.unavailable(
                "the GitHub diff read for PR #7 failed: 502 Bad Gateway"
            ),
        )

        assert manifest.prs[0].diff_established is False
        assert "502 Bad Gateway" in manifest.prs[0].diff_gap
        assert manifest.diffed_candidates() == ()
        [gap] = _diff_gaps(manifest)
        assert gap.candidate == manifest.candidates()[0]
        assert "502 Bad Gateway" in gap.reason

    def test_the_metadata_tells_the_agent_the_diff_is_missing(
        self, tmp_path: Path
    ) -> None:
        """The agent reads metadata, not the manifest builder's intentions."""
        self._download(
            tmp_path,
            PullRequestDiffRead.unavailable("GitHub refused: 403 Forbidden"),
        )

        metadata = json.loads(
            (tmp_path / "data" / "pr-7-007aaaaaaaaa-meta.json").read_text()
        )
        assert metadata["diff_staged"] is False
        assert "403 Forbidden" in metadata["diff_gap"]

    def test_error_shaped_bytes_never_become_a_diff(self, tmp_path: Path) -> None:
        """Success is a typed outcome, never a property of the bytes (#359 D).

        An error body that itself contains ``diff --git`` must still follow the
        failure path. Nothing in this pipeline sniffs content to decide whether
        a read succeeded, so plausible-looking patch text in an error response
        buys it nothing.
        """
        manifest = self._download(
            tmp_path,
            PullRequestDiffRead.unavailable(
                "GitHub returned an error page containing"
                " diff --git a/spoof.py b/spoof.py"
            ),
        )

        assert manifest.prs[0].files.diff == ""
        assert manifest.prs[0].diff_established is False
        assert not list((tmp_path / "data").glob("*-diff.txt"))

    def test_the_superseded_error_body_file_is_gone(self, tmp_path: Path) -> None:
        """Falsification: the exact R29 artifact must be unwritable.

        Restoring the old behaviour would put ``# Error fetching diff: ...``
        into ``pr-7-007aaaaaaaaa-diff.txt`` and name it in the manifest. Both
        halves are asserted, because either one alone is the defect.
        """
        manifest = self._download(
            tmp_path,
            PullRequestDiffRead.unavailable(
                "Direct gh invocation is forbidden; use"
                " GitHubHttpClient/GitHubAdapter"
            ),
        )

        assert not (tmp_path / "data" / "pr-7-007aaaaaaaaa-diff.txt").exists()
        assert manifest.prs[0].files.diff == ""
        assert manifest.prs[0].diff_established is False

    def test_an_unexpected_staging_failure_is_still_an_explicit_gap(
        self, tmp_path: Path
    ) -> None:
        """A raising host is a refusal with a reason, not a silent skip."""

        class _Exploding(MockRepositoryHost):
            def read_pr_diff(self, pr_number: int) -> PullRequestDiffRead:
                raise RuntimeError("socket exploded")

        host = _Exploding(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(7)
            ),
        })
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )

        result = TechLeadDownloader(host).download(manifest, tmp_path)

        assert result.prs[0].files.diff == ""
        assert result.prs[0].diff_established is False
        assert "socket exploded" in result.prs[0].diff_gap


class TestCandidateMaterializationRecord:
    """The staging record cannot say "no diff" and "no reason" at once."""

    def test_a_staged_diff_carries_no_gap(self) -> None:
        staged = CandidateMaterialization(files=PRFiles(diff="d.txt", metadata="m"))

        assert staged.establishes_candidate_diff is True

    def test_a_gap_means_the_prerequisite_is_unmet(self) -> None:
        staged = CandidateMaterialization(files=PRFiles(metadata="m"), gap="boom")

        assert staged.establishes_candidate_diff is False

    def test_an_unexplained_absence_is_unrepresentable(self) -> None:
        with pytest.raises(ValueError, match="recorded why"):
            CandidateMaterialization(files=PRFiles(metadata="m"))

    def test_a_staged_diff_with_a_reason_is_unrepresentable(self) -> None:
        with pytest.raises(ValueError, match="recorded why"):
            CandidateMaterialization(files=PRFiles(diff="d.txt"), gap="boom")


class TestMultiCandidateIsolation:
    """One unreadable candidate must not touch its siblings (#359 F)."""

    def test_a_failed_sibling_leaves_the_others_staged(self, tmp_path: Path) -> None:
        host = MockRepositoryHost(
            prs={
                1: MockPR(number=1, title="PR 1", url="u1", branch="b1", labels=[], head_sha=_sha(1)),
                2: MockPR(number=2, title="PR 2", url="u2", branch="b2", labels=[], head_sha=_sha(2)),
                3: MockPR(number=3, title="PR 3", url="u3", branch="b3", labels=[], head_sha=_sha(3)),
            },
            diffs={
                1: PullRequestDiffRead.readable("diff1"),
                2: PullRequestDiffRead.unavailable("404 Not Found"),
                3: PullRequestDiffRead.readable("diff3"),
            },
        )
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[
                PRToReview(number=1, title="PR 1", url="u1", branch="b1", head_sha=_sha(1)),
                PRToReview(number=2, title="PR 2", url="u2", branch="b2", head_sha=_sha(2)),
                PRToReview(number=3, title="PR 3", url="u3", branch="b3", head_sha=_sha(3)),
            ],
        )

        result = TechLeadDownloader(host).download(manifest, tmp_path)

        assert "diff1" in (tmp_path / "data" / "pr-1-001aaaaaaaaa-diff.txt").read_text()
        assert "diff3" in (tmp_path / "data" / "pr-3-003aaaaaaaaa-diff.txt").read_text()
        assert not (tmp_path / "data" / "pr-2-002aaaaaaaaa-diff.txt").exists()

        assert [pr.diff_established for pr in result.prs] == [True, False, True]
        assert result.diffed_candidates() == (
            result.candidates()[0],
            result.candidates()[2],
        )
        [gap] = _diff_gaps(result)
        assert gap.candidate.pr_number == 2


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
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        result = downloader.download(manifest, tmp_path)

        assert result.prs[0].files.diff == ""
        assert not list((tmp_path / "data").glob("*-diff.txt"))

    def test_a_moved_candidate_holds_no_diff_prerequisite(
        self, tmp_path: Path
    ) -> None:
        """#359 direction C: no diff is advertised, and the gap says why.

        The bytes the transport returned WERE readable — they are simply about
        the commit the pull request moved to. Nothing about them may reach A's
        record, and A inherits nothing from B.
        """
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(8)
            ),
        })
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )

        result = TechLeadDownloader(host).download(manifest, tmp_path)

        assert result.prs[0].diff_established is False
        assert "moved" in result.prs[0].diff_gap
        assert result.diffed_candidates() == ()
        [gap] = _diff_gaps(result)
        assert gap.candidate.head_sha == _sha(7)

    def test_the_metadata_records_why_the_content_could_not_be_bound(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(8)
            ),
        })
        downloader = TechLeadDownloader(host)

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
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )
        result = downloader.download(manifest, tmp_path)

        metadata = json.loads(
            (tmp_path / "data" / f"pr-7-{_sha(7)[:12]}-meta.json").read_text()
        )
        assert metadata["candidate_bound"] is True
        assert "candidate_binding_gap" not in metadata
        assert metadata["diff_staged"] is True
        # #359 direction A: the exact returned bytes, written once, named by
        # the manifest, and the prerequisite established for that candidate.
        assert result.prs[0].files.diff == f"pr-7-{_sha(7)[:12]}-diff.txt"
        assert result.prs[0].diff_established is True
        assert result.prs[0].diff_gap == ""
        assert result.diffed_candidates() == result.candidates()

    def test_a_pull_request_selected_without_a_head_stages_no_diff(
        self, tmp_path: Path
    ) -> None:
        host = MockRepositoryHost(prs={
            7: MockPR(
                number=7, title="PR", url="u", branch="b", labels=[], head_sha=_sha(7)
            ),
        })
        downloader = TechLeadDownloader(host)

        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b")],
        )
        result = downloader.download(manifest, tmp_path)

        assert result.prs[0].files.diff == ""
        assert result.prs[0].diff_established is False
        metadata = json.loads((tmp_path / "data" / "pr-7-unknown-meta.json").read_text())
        assert metadata["candidate_bound"] is False

    def test_both_failures_are_reported_together(self, tmp_path: Path) -> None:
        """An operator needs every condition observed, not just the first."""
        host = MockRepositoryHost(
            prs={
                7: MockPR(
                    number=7, title="PR", url="u", branch="b", labels=[],
                    head_sha=_sha(8),
                ),
            },
            diffs={7: PullRequestDiffRead.unavailable("503 Service Unavailable")},
        )
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )

        result = TechLeadDownloader(host).download(manifest, tmp_path)

        assert "503 Service Unavailable" in result.prs[0].diff_gap
        assert "moved" in result.prs[0].diff_gap


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
        downloader = TechLeadDownloader(host, source)
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
        downloader = TechLeadDownloader(host)
        manifest = TechLeadManifest(
            data_dir="data",
            prs=[PRToReview(number=7, title="PR", url="u", branch="b", head_sha=_sha(7))],
        )

        result = downloader.download(manifest, tmp_path)

        staged = json.loads((tmp_path / "data" / "candidate-evidence.json").read_text())
        assert "no exact-candidate review evidence source" in staged["candidates"][0]["gap"]
        assert result.prs[0].review_established is False
