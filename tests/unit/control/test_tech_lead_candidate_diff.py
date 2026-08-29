"""The candidate diff is a merge prerequisite owned outside the agent (#359).

The R29 live batch proved the production fail-open seam end to end: the
downloader ran ``gh pr diff``, the repository's direct-``gh`` guard refused the
invocation, and the refusal text — ``# Error fetching diff: Direct gh
invocation is forbidden`` — was written into ``pr-357-802d06d9f03a-diff.txt``
and advertised by the manifest as the candidate's diff. The live Tech Lead
declined to ``pass`` on it, correctly; nothing in the product would have
stopped it.

The per-seam behaviour is proved next to each seam (transport in
``tests/unit/test_github_http.py``, outcome mapping in
``tests/unit/test_github_adapter.py``, staging in
``test_tech_lead_downloader.py``, refusal in
``test_tech_lead_candidate_disposition.py``). What is proved HERE is the seam
between them: that a diff read failing at the transport travels all the way to
the orchestrator-owned launch authority as a refusal with the reason attached,
without passing through anything the agent can write.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from issue_orchestrator.domain.board_snapshot import BoardSnapshot
from issue_orchestrator.domain.tech_lead_candidate import CandidatePassPrerequisite
from issue_orchestrator.domain.tech_lead_manifest import TechLeadManifest
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.execution.tech_lead_downloader import TechLeadDownloader
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PullRequestDiffRead
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)

READABLE = "aaaaaaaaaaaa".ljust(40, "a")
UNREADABLE = "bbbbbbbbbbbb".ljust(40, "b")


class _Host:
    """A repository host answering exactly the reads this launch path makes."""

    def __init__(self, prs: dict[int, SimpleNamespace], diffs) -> None:
        self._prs = prs
        self._diffs = diffs

    # -- pull requests -----------------------------------------------------
    def get_prs_with_label(self, label: str, state: str = "open"):
        return [pr for pr in self._prs.values() if pr.state == state]

    def get_pr(self, pr_number: int):
        return self._prs.get(pr_number)

    def read_pr_diff(self, pr_number: int) -> PullRequestDiffRead:
        return self._diffs[pr_number]

    # -- issues (leaf-contract staging) ------------------------------------
    def get_issue(self, issue_number: int):
        return None

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return []


def _pr(number: int, head_sha: str) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title=f"candidate {number}",
        url=f"https://example.invalid/pull/{number}",
        branch=f"359-candidate-{number}",
        head_sha=head_sha,
        labels=["code-reviewed"],
        state="open",
        body="",
    )


def _config(tmp_path: Path) -> Config:
    config = Config(repo="owner/repo")
    config.tech_lead_review_agent = "agent:tech-lead"
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir(parents=True, exist_ok=True)
    return config


def _launch(tmp_path: Path, host: _Host) -> tuple[TechLeadLaunchAuthority, Path]:
    """Run the real launch path and return the authority it recorded."""
    from issue_orchestrator.control.tech_lead_session_policy import (
        prepare_tech_lead_session_data,
    )

    worktree = tmp_path / "worktree"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run"
    run_dir.mkdir(parents=True)
    store = InMemoryTechLeadAuthorityStore()
    prepare_tech_lead_session_data(
        config=_config(tmp_path),
        repository_host=host,
        manifest_downloader=TechLeadDownloader(repository_host=host),
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
            run=SimpleNamespace(run_dir=run_dir, run_id="run-1", session_name="issue-7"),
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


class TestATransportFailureReachesTheLaunchAuthority:
    """Direction B, end to end: unavailable, never content."""

    @staticmethod
    def _host() -> _Host:
        return _Host(
            prs={101: _pr(101, UNREADABLE)},
            diffs={
                101: PullRequestDiffRead.unavailable(
                    "the GitHub diff read for PR #101 failed: 502 Bad Gateway"
                )
            },
        )

    def test_no_diff_file_is_written_for_the_candidate(self, tmp_path: Path) -> None:
        _, data_dir = _launch(tmp_path, self._host())

        assert list(data_dir.glob("*-diff.txt")) == []

    def test_the_manifest_the_agent_reads_advertises_no_diff(
        self, tmp_path: Path
    ) -> None:
        _, data_dir = _launch(tmp_path, self._host())

        manifest = TechLeadManifest.read(data_dir / "manifest.json")
        [entry] = manifest.prs
        assert entry.files.diff == ""
        assert entry.diff_established is False
        assert "502 Bad Gateway" in entry.diff_gap

    def test_the_authority_refuses_a_pass_and_names_the_transport_failure(
        self, tmp_path: Path
    ) -> None:
        """The record the agent cannot touch, carrying the reason it will need.

        The manifest above lives in a worktree cleanup deletes; this row is
        what the completion lane reads afterwards to refuse the ``pass`` and
        write the receipt an operator acts on.
        """
        authority, _ = _launch(tmp_path, self._host())

        unmet = _unmet(authority, 101)
        assert CandidatePassPrerequisite.CANDIDATE_DIFF in unmet
        assert "502 Bad Gateway" in unmet[CandidatePassPrerequisite.CANDIDATE_DIFF]
        assert authority.diffed_candidates == ()

    def test_the_candidate_is_still_audited(self, tmp_path: Path) -> None:
        """The batch set and the threshold set may not diverge (#6768, #352).

        An unreadable candidate is REFUSED, not dropped: it stays in the
        manifest the session audits so the run still settles its watch-set
        membership.
        """
        authority, data_dir = _launch(tmp_path, self._host())

        assert authority.manifest_pr_numbers == (101,)
        assert (data_dir / "pr-101-bbbbbbbbbbbb-meta.json").exists()


class TestASuccessfulReadEstablishesThePrerequisite:
    """Direction A, end to end: exact bytes, once, named, and bound."""

    DIFF = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n"

    def _launch(self, tmp_path: Path):
        host = _Host(
            prs={101: _pr(101, READABLE)},
            diffs={101: PullRequestDiffRead.readable(self.DIFF)},
        )
        return _launch(tmp_path, host)

    def test_the_exact_bytes_are_written_once_under_the_candidate_name(
        self, tmp_path: Path
    ) -> None:
        _, data_dir = self._launch(tmp_path)

        [staged] = list(data_dir.glob("*-diff.txt"))
        assert staged.name == "pr-101-aaaaaaaaaaaa-diff.txt"
        assert staged.read_text() == self.DIFF

    def test_the_manifest_names_that_file_and_records_the_binding(
        self, tmp_path: Path
    ) -> None:
        _, data_dir = self._launch(tmp_path)

        manifest = TechLeadManifest.read(data_dir / "manifest.json")
        [entry] = manifest.prs
        assert entry.files.diff == "pr-101-aaaaaaaaaaaa-diff.txt"
        assert entry.diff_established is True
        assert entry.diff_gap == ""

    def test_the_diff_prerequisite_is_established_for_that_candidate(
        self, tmp_path: Path
    ) -> None:
        authority, _ = self._launch(tmp_path)

        assert authority.diffed_candidates == authority.manifest_candidates
        assert CandidatePassPrerequisite.CANDIDATE_DIFF not in _unmet(authority, 101)


class TestOneUnreadableCandidateIsolatedFromItsSiblings:
    """Direction F, end to end: no manufactured evidence, no erased evidence."""

    def _launch(self, tmp_path: Path):
        host = _Host(
            prs={101: _pr(101, READABLE), 102: _pr(102, UNREADABLE)},
            diffs={
                101: PullRequestDiffRead.readable("diff --git a/a.py b/a.py\n+x\n"),
                102: PullRequestDiffRead.unavailable(
                    "the GitHub diff read for PR #102 failed: 404 Not Found"
                ),
            },
        )
        return _launch(tmp_path, host)

    def test_the_readable_sibling_keeps_its_staged_evidence(
        self, tmp_path: Path
    ) -> None:
        _, data_dir = self._launch(tmp_path)

        assert (data_dir / "pr-101-aaaaaaaaaaaa-diff.txt").exists()
        assert not (data_dir / "pr-102-bbbbbbbbbbbb-diff.txt").exists()

    def test_only_the_readable_sibling_holds_the_prerequisite(
        self, tmp_path: Path
    ) -> None:
        authority, _ = self._launch(tmp_path)

        assert [c.pr_number for c in authority.diffed_candidates] == [101]
        assert CandidatePassPrerequisite.CANDIDATE_DIFF not in _unmet(authority, 101)
        assert "404 Not Found" in _unmet(authority, 102)[
            CandidatePassPrerequisite.CANDIDATE_DIFF
        ]

    def test_the_flavor_is_still_an_ordinary_batch_review(
        self, tmp_path: Path
    ) -> None:
        """A materialization gap changes no lane: it refuses one candidate."""
        authority, _ = self._launch(tmp_path)

        assert authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW
        assert authority.manifest_pr_numbers == (101, 102)
