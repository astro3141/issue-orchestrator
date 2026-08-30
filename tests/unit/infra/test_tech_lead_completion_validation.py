"""The trusted owner runs the check the model may not run (#385).

The measured defect was concrete: a bounded Tech Lead reached its completion
protocol, was required to run ``prepush-check --dirty-only -v``, and died with
``Operation not permitted`` writing
``<git-common-dir>/issue-orchestrator/validate-timings.jsonl``. The repair is an
ownership move, so the proofs here are on EFFECTS the owner now produces —
the shared-git-dir timing line, the durable verdict file outside every session
write root — and on the fail-closed direction of every way the check can end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.tech_lead_completion_validation import (
    TechLeadCompletionValidation,
    TechLeadCompletionValidationStatus,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.infra.tech_lead_completion_validation import (
    TECH_LEAD_COMPLETION_VALIDATION_DIRNAME,
    TECH_LEAD_COMPLETION_VALIDATION_TIMING_KIND,
    TrustedTechLeadCompletionValidator,
)

RUN_ID = "20260830T101112000000Z"
SESSION = "issue-385"
HEAD = "a" * 40


class FakeWorkingCopy:
    """The two reads the guard needs, plus every way they can go wrong."""

    def __init__(
        self,
        *,
        head: str | None = HEAD,
        dirty: list[str] | None = None,
        unenumerable: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._head = head
        self._dirty = list(dirty or ())
        self._unenumerable = unenumerable
        self._raises = raises
        self.dirty_modes: list[str] = []

    def get_head_sha(self, worktree: Path) -> str | None:
        return self._head

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        self.dirty_modes.append(mode)
        if self._raises is not None:
            raise self._raises
        return None if self._unenumerable else self._dirty


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A checkout whose git dir is resolvable, so the timing write has a home."""
    root = tmp_path / "scratch-tech-lead"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "primary"
    root.mkdir()
    return root


def _validator(
    repo_root: Path, working_copy: FakeWorkingCopy
) -> TrustedTechLeadCompletionValidator:
    return TrustedTechLeadCompletionValidator(
        working_copy=working_copy, repo_root=repo_root
    )


def _validate(
    repo_root: Path,
    worktree: Path,
    working_copy: FakeWorkingCopy,
    *,
    candidate_head_sha: str = HEAD,
) -> TechLeadCompletionValidation:
    return _validator(repo_root, working_copy).validate_completion(
        run_id=RUN_ID,
        session_name=SESSION,
        worktree=worktree,
        candidate_head_sha=candidate_head_sha,
    )


def _timing_records(worktree: Path) -> list[dict]:
    path = worktree / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _evidence_files(repo_root: Path) -> list[Path]:
    directory = state_dir(repo_root) / TECH_LEAD_COMPLETION_VALIDATION_DIRNAME
    return sorted(directory.glob("*.json")) if directory.exists() else []


class TestACleanCheckoutPasses:
    def test_the_verdict_is_a_pass_bound_to_the_candidate(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(repo_root, worktree, FakeWorkingCopy())

        assert validation.status is TechLeadCompletionValidationStatus.PASSED
        assert validation.binds_to(
            run_id=RUN_ID, session_name=SESSION, candidate_head_sha=HEAD
        )

    def test_the_tracked_dirt_question_is_the_one_that_was_asked(
        self, repo_root: Path, worktree: Path
    ) -> None:
        working_copy = FakeWorkingCopy()

        _validate(repo_root, worktree, working_copy)

        assert working_copy.dirty_modes == ["tracked"]


class TestTheHostEffectTheModelCouldNotMake:
    def test_the_timing_record_lands_in_the_shared_git_dir(
        self, repo_root: Path, worktree: Path
    ) -> None:
        """F3: the very write ``prepush-check`` failed to make, made here."""
        _validate(repo_root, worktree, FakeWorkingCopy())

        records = _timing_records(worktree)
        assert [record["kind"] for record in records] == [
            TECH_LEAD_COMPLETION_VALIDATION_TIMING_KIND
        ]
        assert records[0]["run_id"] == RUN_ID
        assert records[0]["session_name"] == SESSION
        assert records[0]["head_sha"] == HEAD
        assert records[0]["status"] == "passed"

    def test_a_failed_check_is_recorded_too(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _validate(repo_root, worktree, FakeWorkingCopy(dirty=["src/a.py"]))

        assert _timing_records(worktree)[0]["status"] == "failed"

    def test_a_timing_write_that_cannot_land_refuses_the_completion(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared-dir write is part of the contract this owner took over."""

        def _denied(*args: object, **kwargs: object) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(
            "issue_orchestrator.infra.tech_lead_completion_validation."
            "append_validation_timing",
            _denied,
        )

        validation = _validate(repo_root, worktree, FakeWorkingCopy())

        assert validation.status is TechLeadCompletionValidationStatus.UNAVAILABLE
        assert "shared git dir" in validation.detail


class TestTheVerdictIsDurableAndOutsideTheSession:
    def test_it_is_filed_under_the_primary_checkouts_state_dir(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _validate(repo_root, worktree, FakeWorkingCopy())

        files = _evidence_files(repo_root)
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["run_id"] == RUN_ID
        assert payload["candidate_head_sha"] == HEAD
        # Nothing is written into the session's own checkout.
        assert not (worktree / ".issue-orchestrator" / "state").exists()

    def test_a_second_call_reuses_the_filed_verdict(
        self, repo_root: Path, worktree: Path
    ) -> None:
        first = _validate(repo_root, worktree, FakeWorkingCopy())
        second_copy = FakeWorkingCopy(dirty=["src/a.py"])

        second = _validate(repo_root, worktree, second_copy)

        assert second == first
        # The re-read short-circuits before the guard runs again.
        assert second_copy.dirty_modes == []
        assert len(_timing_records(worktree)) == 1

    def test_a_new_candidate_gets_its_own_verdict(
        self, repo_root: Path, worktree: Path
    ) -> None:
        other = "b" * 40
        _validate(repo_root, worktree, FakeWorkingCopy())

        second = _validate(
            repo_root,
            worktree,
            FakeWorkingCopy(head=other),
            candidate_head_sha=other,
        )

        assert second.candidate_head_sha == other
        assert len(_evidence_files(repo_root)) == 2

    def test_an_unfilable_verdict_is_unavailable_not_a_pass(
        self, repo_root: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied(*args: object, **kwargs: object) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(
            "issue_orchestrator.infra.tech_lead_completion_validation."
            "atomic_create_bytes",
            _denied,
        )

        validation = _validate(repo_root, worktree, FakeWorkingCopy())

        assert validation.status is TechLeadCompletionValidationStatus.UNAVAILABLE
        assert "durably" in validation.detail

    def test_corrupt_filed_evidence_is_re_validated_not_trusted(
        self, repo_root: Path, worktree: Path
    ) -> None:
        _validate(repo_root, worktree, FakeWorkingCopy())
        filed = _evidence_files(repo_root)[0]
        filed.write_text("{not json")

        # A create-once file that is already there cannot be replaced, so the
        # re-validation ends UNAVAILABLE rather than silently passing on
        # unreadable evidence.
        again = _validate(repo_root, worktree, FakeWorkingCopy())

        assert again.status is TechLeadCompletionValidationStatus.UNAVAILABLE


class TestEveryFailingDirection:
    def test_dirty_tracked_content_fails(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(
            repo_root, worktree, FakeWorkingCopy(dirty=["src/a.py", "src/b.py"])
        )

        assert validation.status is TechLeadCompletionValidationStatus.FAILED
        assert "src/a.py" in validation.detail

    def test_an_unreadable_head_is_unavailable(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(repo_root, worktree, FakeWorkingCopy(head=None))

        assert validation.status is TechLeadCompletionValidationStatus.UNAVAILABLE

    def test_a_head_that_moved_under_the_check_fails(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(
            repo_root, worktree, FakeWorkingCopy(head="c" * 40)
        )

        assert validation.status is TechLeadCompletionValidationStatus.FAILED
        assert "moved" in validation.detail
        # Still bound to what the caller asked about, so the caller's own
        # binding check cannot be satisfied by a verdict about another commit.
        assert validation.candidate_head_sha == HEAD

    def test_an_unenumerable_checkout_fails_closed(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(
            repo_root, worktree, FakeWorkingCopy(unenumerable=True)
        )

        assert validation.status is TechLeadCompletionValidationStatus.FAILED
        assert "could not be enumerated" in validation.detail

    def test_a_timeout_is_its_own_status(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(
            repo_root,
            worktree,
            FakeWorkingCopy(raises=TimeoutError("git status took too long")),
        )

        assert validation.status is TechLeadCompletionValidationStatus.TIMED_OUT

    def test_an_os_error_is_unavailable(
        self, repo_root: Path, worktree: Path
    ) -> None:
        validation = _validate(
            repo_root, worktree, FakeWorkingCopy(raises=OSError("git is gone"))
        )

        assert validation.status is TechLeadCompletionValidationStatus.UNAVAILABLE
