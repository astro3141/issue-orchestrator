"""``coding-done completed`` for a planning_investigation run (#293).

#289 made the code-candidate validation gate technically unreachable from
inside a planning session. The next live run showed that was only half the
blocker: `coding-done completed` ran the same gate *itself*, failed on the same
sandbox class, and wrote no completion record — so the planning lane could not
be completed by an agent that followed its contract perfectly.

These tests hold the fix to its exact width. The gate command used here writes
a marker file *outside* the repository, so "no candidate quick-validation
process starts" is proven by the absence of a file the shell would have created,
not by an assertion about how the code is arranged.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from issue_orchestrator.control.tech_lead_completion import (
    resolve_tech_lead_launch_authority,
)
from issue_orchestrator.control.tech_lead_zero_code import (
    settle_zero_code_planning_completion,
)
from issue_orchestrator.domain.models import (
    COMPLETION_RECORD_PATH,
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.entrypoints.cli_tools import agent_done, coding_done
from issue_orchestrator.entrypoints.cli_tools.agent_done import QuickValidationSelection
from issue_orchestrator.entrypoints.cli_tools.coding_done import main as coding_done_main
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

SESSION_ID = "issue-293"
FOCUS_ISSUE = 293
PLANNING = TechLeadSessionFlavor.PLANNING_INVESTIGATION


class Repo:
    """One provisioned checkout plus the run contract the orchestrator injects.

    ``gate_marker`` lives outside the checkout on purpose: a quick gate that
    ran leaves it behind, and a quick gate that never started cannot, whatever
    the working tree is doing.
    """

    def __init__(self, root: Path, gate_marker: Path, assets: SessionRunAssets) -> None:
        self.root = root
        self.gate_marker = gate_marker
        self.assets = assets

    @property
    def run_dir(self) -> Path:
        return self.assets.run_dir

    @property
    def gate_ran(self) -> bool:
        return self.gate_marker.exists()

    def head_sha(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def assign(self, flavor: TechLeadSessionFlavor) -> None:
        """Write the agent-visible launch assignment this run would carry."""
        TechLeadAssignment(
            flavor=flavor,
            focus_issue_number=FOCUS_ISSUE if flavor.is_issue_focused else None,
        ).write(self.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME)

    def completion_record(self) -> CompletionRecord:
        return CompletionRecord.from_dict(
            json.loads((self.root / COMPLETION_RECORD_PATH).read_text())
        )

    def completion_record_exists(self) -> bool:
        return (self.root / COMPLETION_RECORD_PATH).exists()

    def run_manifest(self) -> dict[str, object]:
        return json.loads((self.run_dir / "manifest.json").read_text())


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "checkout"
    root.mkdir()
    marker = tmp_path / "quick-gate-ran"
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=root, capture_output=True, check=True)
    (root / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"], cwd=root, capture_output=True, check=True
    )

    config_dir = root / ".issue-orchestrator" / "config"
    config_dir.mkdir(parents=True)
    # Fails the way the live planning run's gate failed, and records that it
    # started at all.
    (config_dir / "default.yaml").write_text(
        "validation:\n"
        "  quick:\n"
        f"    cmd: \"touch '{marker}' && exit 1\"\n"
        "    timeout_seconds: 30\n"
    )

    assets = FileSystemSessionOutput().start_run(root, SESSION_ID)
    return Repo(root, marker, assets)


@contextmanager
def completing(repo: Repo, *, status: str = "completed") -> Iterator[None]:
    """Run one managed ``coding-done`` invocation from inside the checkout."""
    env = {
        f"{ENV_PREFIX}SESSION_ID": SESSION_ID,
        "ORCHESTRATOR_SESSION_ID": SESSION_ID,
        f"{ENV_PREFIX}RUN_DIR": str(repo.run_dir),
    }
    argv = [
        "coding-done",
        status,
        "--implementation",
        "Prepared the tactical leaf",
        "--problems",
        "None",
    ]
    original_cwd = Path.cwd()
    os.chdir(repo.root)
    try:
        with patch.dict(os.environ, env), patch("sys.argv", argv):
            yield
    finally:
        os.chdir(original_cwd)
        os.environ.pop("ORCHESTRATOR_SESSION_ID", None)


@contextmanager
def candidate_config_refused() -> Iterator[None]:
    """Fail the test if the candidate quick-validation configuration is read.

    Both module bindings of the loader are covered. ``coding_done`` imported it
    by value, and ``run_validation`` reaches it through ``agent_done``, so
    patching only one would leave the other route open and the tripwire would
    prove nothing.
    """

    def refuse(worktree: Path) -> QuickValidationSelection:
        raise AssertionError(
            "the candidate quick-validation configuration was read for a "
            f"completion that offers no candidate ({worktree})"
        )

    with (
        patch.object(coding_done, "load_validation_cmd", refuse),
        patch.object(agent_done, "load_validation_cmd", refuse),
    ):
        yield


@contextmanager
def candidate_config_reads() -> Iterator[list[Path]]:
    """Record every read of the candidate quick-validation configuration."""
    reads: list[Path] = []
    real = agent_done.load_validation_cmd

    def counting(worktree: Path) -> QuickValidationSelection:
        reads.append(worktree)
        return real(worktree)

    with (
        patch.object(coding_done, "load_validation_cmd", counting),
        patch.object(agent_done, "load_validation_cmd", counting),
    ):
        yield reads


class TestRoutingIsDecidedBeforeTheCandidateConfigIsRead:
    """A1. The planning lane does not depend on candidate gate configuration.

    Dropping the gate late — after reading the config the gate would have
    used — would still leave a planning completion answerable to a candidate
    contract it has no candidate for: a config read that dies (`die` on a
    missing config file) takes the completion record with it. So the ordering
    is the behaviour, and it is proven by refusing the read outright rather
    than by asserting on how the function is written.
    """

    def test_a_planning_completion_never_reads_the_candidate_config(
        self, repo: Repo
    ) -> None:
        repo.assign(PLANNING)

        with completing(repo), candidate_config_refused():
            coding_done_main()

        assert repo.completion_record().outcome is CompletionOutcome.COMPLETED
        assert repo.gate_ran is False

    def test_an_ordinary_actor_completion_still_reads_it(self, repo: Repo) -> None:
        """The same tripwire in the other direction: here the read must happen.

        Without this, a change that stopped reading the config for *every*
        completion would satisfy the planning test above while silently
        removing the gate from the lane that needs it.
        """
        with completing(repo), candidate_config_reads() as reads:
            with pytest.raises(SystemExit) as exit_info:
                coding_done_main()

        assert reads == [repo.root]
        assert repo.gate_ran is True
        assert exit_info.value.code == 1

    @pytest.mark.parametrize(
        "flavor",
        [flavor for flavor in TechLeadSessionFlavor if flavor is not PLANNING],
        ids=lambda flavor: flavor.value,
    )
    def test_every_non_planning_flavor_still_reads_it(
        self, repo: Repo, flavor: TechLeadSessionFlavor
    ) -> None:
        repo.assign(flavor)

        with completing(repo), candidate_config_reads() as reads:
            with pytest.raises(SystemExit):
                coding_done_main()

        assert reads == [repo.root]
        assert repo.gate_ran is True


class TestTheExactPlanningCompletion:
    """A. A managed planning run on a clean, unchanged checkout completes."""

    def test_no_candidate_quick_validation_process_starts(self, repo: Repo) -> None:
        repo.assign(PLANNING)

        with completing(repo):
            coding_done_main()

        assert repo.gate_ran is False

    def test_the_completion_record_is_written(self, repo: Repo) -> None:
        repo.assign(PLANNING)

        with completing(repo):
            coding_done_main()

        record = repo.completion_record()
        assert record.outcome is CompletionOutcome.COMPLETED
        assert record.session_id == SESSION_ID
        assert record.implementation == "Prepared the tactical leaf"

    def test_no_validation_evidence_is_fabricated(self, repo: Repo) -> None:
        """A gate that did not run leaves no record pretending it did."""
        repo.assign(PLANNING)

        with completing(repo):
            coding_done_main()

        assert repo.completion_record().validation_record_path is None
        manifest = repo.run_manifest()
        assert "validation_record_path" not in manifest
        assert "validation_status" not in manifest
        assert not list(repo.run_dir.glob("validation-*"))

    def test_the_operator_is_told_why_the_gate_did_not_run(
        self, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo.assign(PLANNING)

        with completing(repo):
            coding_done_main()

        out = capsys.readouterr().out
        assert "no quick validation for this completion" in out
        assert "planning_investigation" in out

    def test_the_record_still_settles_through_the_zero_code_lane(
        self, repo: Repo
    ) -> None:
        """The record stays consumable by the normal Tech Lead lane (#202/#261).

        The publication intent ``coding-done`` gives every completion is
        dropped by the trusted settler on an unchanged HEAD, so what remains is
        the bounded planning outcome — exactly as before this change, since the
        record's shape is untouched.
        """
        repo.assign(PLANNING)

        with completing(repo):
            coding_done_main()

        record = repo.completion_record()
        settlement = settle_zero_code_planning_completion(
            authority=TechLeadLaunchAuthority(
                flavor=PLANNING,
                anchor_issue_number=FOCUS_ISSUE,
                focus_issue_number=FOCUS_ISSUE,
                launch_base_sha=repo.head_sha(),
            ),
            requested_actions=tuple(record.requested_actions),
            worktree=repo.root,
            worktree_reader=UnchangedCheckout(repo.head_sha()),
        )

        assert settlement.zero_code is True
        assert RequestedAction.PUSH_BRANCH not in settlement.requested_actions
        assert RequestedAction.CREATE_PR not in settlement.requested_actions


class TestThePreCompletionDirtyCheckStillRuns:
    """A2. Planning is zero-code; a dirty planning worktree is not success."""

    def test_a_dirty_planning_worktree_is_refused(self, repo: Repo) -> None:
        repo.assign(PLANNING)
        (repo.root / "README.md").write_text("the planning run wrote code")

        with completing(repo):
            with pytest.raises(SystemExit) as exit_info:
                coding_done_main()

        assert exit_info.value.code == 1
        assert repo.completion_record_exists() is False

    def test_the_refusal_precedes_the_gate_decision(
        self, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo.assign(PLANNING)
        (repo.root / "README.md").write_text("the planning run wrote code")

        with completing(repo):
            with pytest.raises(SystemExit):
                coding_done_main()

        assert "WORKING TREE IS DIRTY" in capsys.readouterr().out
        assert repo.gate_ran is False


class TestTheOrdinaryActorRegression:
    """B. Removing the discrimination must make this test fail, not pass."""

    def test_an_ordinary_actor_completion_still_runs_the_gate(
        self, repo: Repo
    ) -> None:
        """No assignment file at all — the shape every coding agent has."""
        with completing(repo):
            with pytest.raises(SystemExit) as exit_info:
                coding_done_main()

        assert repo.gate_ran is True
        assert exit_info.value.code == 1
        assert repo.completion_record_exists() is False

    def test_the_failing_gate_still_blocks_the_completion_record(
        self, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with completing(repo):
            with pytest.raises(SystemExit):
                coding_done_main()

        assert "VALIDATION FAILED" in capsys.readouterr().out


class TestTheOtherTechLeadFlavors:
    """C. "Tech Lead" does not generalise into "skip validation"."""

    @pytest.mark.parametrize(
        "flavor",
        [
            flavor
            for flavor in TechLeadSessionFlavor
            if flavor is not PLANNING
        ],
        ids=lambda flavor: flavor.value,
    )
    def test_every_non_planning_flavor_still_runs_the_gate(
        self, repo: Repo, flavor: TechLeadSessionFlavor
    ) -> None:
        repo.assign(flavor)

        with completing(repo):
            with pytest.raises(SystemExit) as exit_info:
                coding_done_main()

        assert repo.gate_ran is True
        assert exit_info.value.code == 1


class TestMalformedRoutingEvidence:
    """D. Missing or malformed identity is not an implicit planning skip."""

    def test_a_malformed_assignment_still_runs_the_gate(self, repo: Repo) -> None:
        path = repo.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ truncated", encoding="utf-8")

        with completing(repo):
            with pytest.raises(SystemExit):
                coding_done_main()

        assert repo.gate_ran is True

    def test_an_out_of_run_assignment_is_not_consulted(self, repo: Repo) -> None:
        """Only the run directory the orchestrator injected is read.

        A planning assignment dropped anywhere else in the worktree — including
        the sessions root a sibling run would use — buys nothing, because the
        routing question is asked of the proven run contract rather than of a
        path search.
        """
        stray = repo.root / ".issue-orchestrator" / "sessions"
        TechLeadAssignment(flavor=PLANNING, focus_issue_number=FOCUS_ISSUE).write(
            stray / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        )

        with completing(repo):
            with pytest.raises(SystemExit):
                coding_done_main()

        assert repo.gate_ran is True


class UnchangedCheckout:
    """The orchestrator's own two reads of a checkout that did not move."""

    def __init__(self, head: str) -> None:
        self._head = head

    def get_head_sha(self, worktree: Path) -> str | None:
        return self._head

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        return []


class TestTheHintIsNotAuthority:
    """E. What a spoofed assignment buys, and what it does not."""

    def test_the_skip_leaves_no_claim_in_the_completion_record(
        self, repo: Repo
    ) -> None:
        """Nothing downstream could key off the routing signal even if it tried.

        The record an ordinary Actor writes and the record a planning run
        writes differ in no field: the routing answer is consumed entirely
        inside the command and never travels.
        """
        repo.assign(PLANNING)

        with completing(repo):
            coding_done_main()

        payload = repo.completion_record().to_dict()
        assert "flavor" not in payload
        assert "planning" not in json.dumps(payload).lower()

    def test_a_spoofed_assignment_does_not_survive_the_authority_check(
        self, repo: Repo
    ) -> None:
        """The orchestrator-owned record is what decides, and it disagrees.

        A code-bearing session that writes itself a planning assignment loses
        its own quick feedback — and gains nothing: the trusted launch
        authority still says what this run was, and the divergence is reported
        as tamper evidence rather than accepted.
        """
        repo.assign(PLANNING)
        with completing(repo):
            coding_done_main()

        store = InMemoryTechLeadAuthorityStore()
        store.record(
            run_id=repo.assets.run_id,
            session_name=SESSION_ID,
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.BATCH_REVIEW,
                anchor_issue_number=FOCUS_ISSUE,
                launch_base_sha=repo.head_sha(),
            ),
        )

        authority, error = resolve_tech_lead_launch_authority(
            store,
            run_dir=repo.run_dir,
            run_id=repo.assets.run_id,
            session_name=SESSION_ID,
        )

        assert authority is not None
        assert authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW
        assert error is not None
        assert "does not match the launch authority" in error

    def test_the_zero_code_owner_reads_the_authority_not_the_worktree(
        self, repo: Repo
    ) -> None:
        """Publication intent survives a worktree that claims to be planning.

        Same checkout, same worktree assignment the routing hint accepted; the
        settler is handed a non-planning authority and keeps every publication
        action, because it never asks the worktree what flavor this was.
        """
        repo.assign(PLANNING)

        settlement = settle_zero_code_planning_completion(
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                anchor_issue_number=FOCUS_ISSUE,
                focus_issue_number=FOCUS_ISSUE,
                launch_base_sha=repo.head_sha(),
            ),
            requested_actions=(RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR),
            worktree=repo.root,
            worktree_reader=UnchangedCheckout(repo.head_sha()),
        )

        assert settlement.zero_code is False
        assert settlement.requested_actions == (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.CREATE_PR,
        )
