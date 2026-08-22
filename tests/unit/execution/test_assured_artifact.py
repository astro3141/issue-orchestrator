"""The writer side of exact-artifact identity (#194).

``artifact_under_assurance`` is what a live-assurance run is allowed to claim
about itself. It replaced ``--live-assurance-head-sha=$(git rev-parse HEAD)``
in a Makefile recipe, where the SHA came from ``make``'s cwd while the record
was written under a separately overridable root, so the two could describe
different checkouts and nothing downstream could tell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.execution.assured_artifact import (
    AssuredArtifactUnresolvable,
    artifact_under_assurance,
)


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.email", "artifact@example.invalid")
    _git(repo, "config", "user.name", "Artifact Harness")
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "--quiet", "-m", "artifact")
    return repo


class TestOneRootDecidesBoth:
    def test_a_clean_checkout_resolves_to_its_own_head(self, checkout: Path) -> None:
        artifact = artifact_under_assurance(checkout)

        assert artifact.head_sha == _git(checkout, "rev-parse", "HEAD")
        assert artifact.working_tree_dirty is False

    def test_the_sha_is_canonical_so_it_matches_the_stores_key(
        self, checkout: Path
    ) -> None:
        artifact = artifact_under_assurance(checkout)

        assert artifact.head_sha == artifact.head_sha.lower()
        assert len(artifact.head_sha) == 40

    def test_two_checkouts_cannot_be_conflated(self, tmp_path: Path) -> None:
        """The failure the recipe made reachable: root here, SHA from there."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        for repo in (first, second):
            repo.mkdir()
            _git(repo, "init", "--quiet", "--initial-branch=main")
            _git(repo, "config", "user.email", "artifact@example.invalid")
            _git(repo, "config", "user.name", "Artifact Harness")
            _git(repo, "commit", "--quiet", "--allow-empty", "-m", repo.name)

        assert (
            artifact_under_assurance(first).head_sha
            != artifact_under_assurance(second).head_sha
        )


class TestDirtinessIsObservedNotAssumed:
    def test_an_uncommitted_edit_to_a_tracked_file_is_dirty(
        self, checkout: Path
    ) -> None:
        (checkout / "tracked.txt").write_text("edited\n", encoding="utf-8")

        assert artifact_under_assurance(checkout).working_tree_dirty is True

    def test_an_untracked_file_is_dirty(self, checkout: Path) -> None:
        """The ordinary state of sandbox work in progress."""
        (checkout / "new_probe.py").write_text("# wip\n", encoding="utf-8")

        assert artifact_under_assurance(checkout).working_tree_dirty is True

    def test_a_staged_change_is_dirty(self, checkout: Path) -> None:
        (checkout / "tracked.txt").write_text("staged\n", encoding="utf-8")
        _git(checkout, "add", "tracked.txt")

        assert artifact_under_assurance(checkout).working_tree_dirty is True

    def test_the_head_is_unchanged_by_any_of_that(self, checkout: Path) -> None:
        """Which is the whole problem: the SHA alone cannot see the edits."""
        head = _git(checkout, "rev-parse", "HEAD")
        (checkout / "tracked.txt").write_text("edited\n", encoding="utf-8")

        assert artifact_under_assurance(checkout).head_sha == head


class TestARootThatNamesNoCommit:
    def test_a_directory_that_is_not_a_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(AssuredArtifactUnresolvable, match="not a checkout"):
            artifact_under_assurance(tmp_path)

    def test_a_repository_with_no_commit_is_refused(self, tmp_path: Path) -> None:
        """Fail fast rather than file evidence nobody can look up."""
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "--quiet", "--initial-branch=main")

        with pytest.raises(AssuredArtifactUnresolvable, match="not a checkout"):
            artifact_under_assurance(repo)
