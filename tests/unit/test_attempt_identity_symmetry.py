"""An attempt sidecar the product writes is one the product can read (#378).

R31 measured the asymmetry directly. The orchestrator persisted

    .issue-orchestrator/attempts/--376--7980af61....json
    {"schema_version": 3, "issue_key_type": "github",
     "issue_key": "376", "issue_scope": ""}

and then rejected its own record on the next read, because
``Attempt.from_dict`` refuses a blank ``issue_scope`` while nothing on the
write side refused it. The ``ValueError`` escaped the candidate operation and
aborted the whole planning iteration, repeatedly, over a record that must not
be repaired by hand.

Two halves, and both are tested here:

* **Symmetry.** ``AttemptKey`` is the one owner of durable attempt identity
  validity. Every sidecar PATH is derived from a key and every sidecar PAYLOAD
  serializes one, so an identity the reader would reject cannot reach either —
  the refusal lands before a file is created or replaced.
* **Containment.** A record already damaged is not made readable by any of
  this, so encountering one must refuse THAT candidate and leave the pass it
  was found in able to continue. Never absence, never PASS, never a
  synthesized identity, and never a crash.

The R31 sidecar itself is evidence. Nothing here — and no procedure this
change introduces — deletes, edits, renames, or infers a value into an
existing damaged record; the fixtures below prove the bytes survive the
refusal untouched.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.control.gate_failure_diagnostics import GateFailureDiagnostics
from issue_orchestrator.control.publication_gate import (
    PublicationGate,
    RunValidationContracts,
)
from issue_orchestrator.domain.attempt import (
    Attempt,
    AttemptIdentityError,
    AttemptKey,
    CorruptAttemptEvidence,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from tests.unit.test_publication_gate import (
    PUBLISH_SENTINEL,
    RecordingCommandRunner,
    StubAttemptKeys,
    StubWorkingCopy,
    sentinel_registry,
)

REPO = "astro3141/issue-orchestrator"
ISSUE = "376"
# The R31 artefact's own commit, kept as the fixture SHA so the shape under
# test is the shape that was measured.
CANDIDATE = "7980af614dcb0bc8c873dcdfad4afbacf5a15d0f"
SIBLING_CANDIDATE = "d" * 40


def attempts_dir(root: Path) -> Path:
    return root / ".issue-orchestrator" / "attempts"


def scoped_key(issue: str = ISSUE, sha: str = CANDIDATE) -> AttemptKey:
    return AttemptKey(GitHubIssueKey(repo=REPO, external_id=issue), sha)


def seed_r31_shaped_sidecar(root: Path, *, key: AttemptKey) -> Path:
    """A current-schema sidecar with a blank ``issue_scope``, as R31 left one.

    Written at the persistence boundary the store reads from, and filed under
    the path the FIXED writer would use, so the read under test is the keyed
    read production performs — not a directory scan that happens to find it.
    """
    directory = attempts_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{REPO.replace('/', '-')}--{key.issue_stable_id}--{key.head_sha}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "issue_key_type": "github",
                "issue_key": key.issue_stable_id,
                "issue_scope": "",
                "head_sha": key.head_sha,
                "reroute_budget_used": 0,
                "completed_evaluations": [],
            }
        ),
        encoding="utf-8",
    )
    return path


class TestAnUnnameableIdentityIsNeverPersisted:
    """F1: the write side refuses what the read side would refuse."""

    @pytest.mark.parametrize("scope", ["", "   ", "\t\n"])
    def test_a_blank_scope_identity_emits_no_sidecar(
        self, tmp_path: Path, scope: str
    ) -> None:
        """Drive the production writer; prove nothing reached the disk.

        Whitespace is included because ``strip()`` is what the reader applies:
        a scope of ``"  "`` round-trips into a record ``from_dict`` rejects
        exactly as ``""`` does, so the writer must refuse both or the asymmetry
        survives in a shape the measured one did not happen to take.
        """
        store = SidecarAttemptStore(tmp_path)

        with pytest.raises(AttemptIdentityError, match="no repository scope"):
            store.update(
                AttemptKey(GitHubIssueKey(repo=scope, external_id=ISSUE), CANDIDATE),
                lambda attempt: attempt,
            )

        assert not attempts_dir(tmp_path).exists()

    def test_a_blank_stable_id_identity_emits_no_sidecar(self, tmp_path: Path) -> None:
        """The other half of the identity, refused by the same owner."""
        store = SidecarAttemptStore(tmp_path)

        with pytest.raises(AttemptIdentityError, match="no stable issue id"):
            store.update(
                AttemptKey(GitHubIssueKey(repo=REPO, external_id="  "), CANDIDATE),
                lambda attempt: attempt,
            )

        assert not attempts_dir(tmp_path).exists()

    def test_an_unnameable_identity_cannot_replace_a_healthy_record(
        self, tmp_path: Path
    ) -> None:
        """"Fail before the file is emitted OR REPLACED" is the whole rule.

        A write that refuses only after truncating its target would leave a
        half-valid sidecar behind, which is strictly worse than the record it
        replaced.
        """
        store = SidecarAttemptStore(tmp_path)
        healthy = scoped_key()
        store.update(healthy, lambda attempt: replace(attempt, reroute_budget_used=2))
        before = {
            path: path.read_bytes() for path in attempts_dir(tmp_path).glob("*.json")
        }

        with pytest.raises(AttemptIdentityError):
            store.update(
                AttemptKey(GitHubIssueKey(repo="", external_id=ISSUE), CANDIDATE),
                lambda attempt: replace(attempt, reroute_budget_used=9),
            )

        after = {
            path: path.read_bytes() for path in attempts_dir(tmp_path).glob("*.json")
        }
        assert after == before
        assert store.for_key(healthy) is not None

    def test_the_reader_and_the_writer_refuse_by_one_predicate(self) -> None:
        """Falsification: the two ends must not be able to disagree.

        A payload the reader rejects for its scope is refused with the same
        exception type the writer's identity check raises. Two spellings of
        "non-empty" is how R31's asymmetry existed at all.
        """
        payload = {
            "schema_version": 3,
            "issue_key_type": "github",
            "issue_key": ISSUE,
            "issue_scope": "",
            "head_sha": CANDIDATE,
        }

        with pytest.raises(AttemptIdentityError, match="missing issue_scope"):
            Attempt.from_dict(payload)


class TestAScopedCandidateRoundTrips:
    """F2: what the writer emits is what the reader reconstructs."""

    def test_write_then_read_yields_the_same_scope_stable_id_and_head(
        self, tmp_path: Path
    ) -> None:
        store = SidecarAttemptStore(tmp_path)
        key = scoped_key()

        store.update(key, lambda attempt: replace(attempt, reroute_budget_used=1))
        reloaded = store.for_key(key)

        assert reloaded is not None
        assert reloaded.key.issue_scope == REPO
        assert reloaded.key.issue_stable_id == ISSUE
        assert reloaded.key.head_sha == CANDIDATE
        assert reloaded.reroute_budget_used == 1

    def test_the_emitted_payload_and_path_both_name_the_repository(
        self, tmp_path: Path
    ) -> None:
        """The measured artefact was a filename, so the proof reaches one."""
        store = SidecarAttemptStore(tmp_path)
        key = scoped_key()

        store.update(key, lambda attempt: attempt)

        (sidecar,) = attempts_dir(tmp_path).glob("*.json")
        assert sidecar.name == f"astro3141-issue-orchestrator--{ISSUE}--{CANDIDATE}.json"
        assert not sidecar.name.startswith("--")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["issue_scope"] == REPO
        assert payload["issue_key"] == ISSUE
        # And the bytes on disk satisfy the reader with no store in between.
        assert Attempt.from_dict(payload).key.issue_scope == REPO


class TestCorruptEvidenceIsCandidateLocal:
    """F4: one damaged record refuses one candidate, not the whole pass."""

    def _gate(self, worktree: Path, runner: RecordingCommandRunner) -> PublicationGate:
        return PublicationGate(
            contracts=RunValidationContracts(
                FileSystemSessionOutput(), sentinel_registry()
            ),
            command_runner=runner,
            working_copy=StubWorkingCopy(CANDIDATE),
            # The primary checkout, outside the worktree, exactly as production
            # composes it: what has to survive cleanup cannot live in the thing
            # being cleaned up.
            attempts=SidecarAttemptStore(worktree.parent),
            attempt_keys=StubAttemptKeys(),
            diagnostics=GateFailureDiagnostics(worktree.parent),
        )

    def _run(self, worktree: Path) -> object:
        return FileSystemSessionOutput().start_run(
            worktree, "issue-376", issue_number=376, validation_profile="default"
        )

    @pytest.fixture
    def worktree(self, tmp_path: Path) -> Path:
        path = tmp_path / "worktree"
        path.mkdir()
        return path

    def test_the_affected_candidate_cannot_obtain_validation_authority(
        self, worktree: Path
    ) -> None:
        """Refused, explicitly, and never by running the contract instead.

        A corruption downgraded to a cache miss would execute the publish
        command and then PASS — the single most dangerous reading, because the
        durable receipt it tried to file could not be written either.
        """
        sidecar = seed_r31_shaped_sidecar(worktree.parent, key=scoped_key())
        runner = RecordingCommandRunner()

        outcome = self._gate(worktree, runner).check(
            worktree=worktree,
            run_assets=self._run(worktree),
            issue_key=GitHubIssueKey(repo=REPO, external_id=ISSUE),
        )

        assert outcome.allowed is False
        assert runner.commands == []
        assert PUBLISH_SENTINEL not in runner.commands
        assert outcome.record is None
        # Attributable: the file, the attempt, and what was wrong with it.
        assert str(sidecar) in outcome.reason
        assert f"{REPO}:{ISSUE}@{CANDIDATE}" in outcome.reason
        assert "issue_scope" in outcome.reason

    def test_an_unrelated_candidate_still_executes_in_the_same_pass(
        self, worktree: Path, tmp_path: Path
    ) -> None:
        """The blast-radius half: siblings must keep making progress.

        Both candidates are decided through ONE gate over ONE store, in one
        control pass, because the R31 failure was precisely that the second
        never got a turn.
        """
        seed_r31_shaped_sidecar(worktree.parent, key=scoped_key())
        sibling_worktree = tmp_path / "worktree" / "sibling"
        sibling_worktree.mkdir(parents=True)
        runner = RecordingCommandRunner()
        gate = self._gate(worktree, runner)

        refused = gate.check(
            worktree=worktree,
            run_assets=self._run(worktree),
            issue_key=GitHubIssueKey(repo=REPO, external_id=ISSUE),
        )
        sibling = gate.check(
            worktree=worktree,
            run_assets=self._run(worktree),
            issue_key=GitHubIssueKey(repo=REPO, external_id="377"),
        )

        assert refused.allowed is False
        assert sibling.allowed is True
        assert runner.commands == [PUBLISH_SENTINEL]

    def test_repeating_the_pass_refuses_again_without_raising(
        self, worktree: Path
    ) -> None:
        """No uncaught exception loop, and no drift toward permissiveness.

        The orchestrator re-observes an unsettled candidate every tick, so the
        second and third encounters have to reach the same refusal rather than
        an escape or an eventual pass.
        """
        sidecar = seed_r31_shaped_sidecar(worktree.parent, key=scoped_key())
        before = sidecar.read_bytes()
        runner = RecordingCommandRunner()
        gate = self._gate(worktree, runner)

        outcomes = [
            gate.check(
                worktree=worktree,
                run_assets=self._run(worktree),
                issue_key=GitHubIssueKey(repo=REPO, external_id=ISSUE),
            )
            for _ in range(3)
        ]

        assert [outcome.allowed for outcome in outcomes] == [False, False, False]
        assert runner.commands == []
        # F5: the damaged record is evidence. Refusing must not repair it.
        assert sidecar.read_bytes() == before

    def test_the_store_names_the_damage_rather_than_reporting_absence(
        self, tmp_path: Path
    ) -> None:
        """The refusal the gate converts, asked of the store directly.

        ``for_key`` returning ``None`` here would make the gate's containment
        indistinguishable from "never gated", which is the reading #378
        forbids.
        """
        key = scoped_key()
        sidecar = seed_r31_shaped_sidecar(tmp_path, key=key)
        store = SidecarAttemptStore(tmp_path)

        with pytest.raises(CorruptAttemptEvidence) as raised:
            store.for_key(key)

        assert raised.value.path == sidecar
        assert raised.value.attempt_ref == f"{REPO}:{ISSUE}@{CANDIDATE}"
        assert "issue_scope" in raised.value.reason

    def test_a_damaged_record_is_not_replaced_by_the_write_that_found_it(
        self, tmp_path: Path
    ) -> None:
        """F5 at the write boundary: no raw repair, not even an implicit one.

        ``update`` reads before it writes, so a store that swallowed the read
        failure would overwrite the evidence with a freshly-defaulted record
        and destroy the only account of what went wrong.
        """
        key = scoped_key()
        sidecar = seed_r31_shaped_sidecar(tmp_path, key=key)
        before = sidecar.read_bytes()
        store = SidecarAttemptStore(tmp_path)

        with pytest.raises(CorruptAttemptEvidence):
            store.update(key, lambda attempt: replace(attempt, reroute_budget_used=1))

        assert sidecar.read_bytes() == before


class TestTheGateRefusalIsShapedLikeAnyOtherRefusal:
    """The containment must arrive through the ordinary gate-failure door.

    A refusal the completion pipeline cannot recognise is a crash wearing a
    different hat: it would still leave the candidate unsettled and the tick
    re-deriving it forever.
    """

    def test_the_outcome_carries_evidence_paths_like_every_other_refusal(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        seed_r31_shaped_sidecar(worktree.parent, key=scoped_key())
        run = FileSystemSessionOutput().start_run(
            worktree, "issue-376", issue_number=376, validation_profile="default"
        )
        gate = PublicationGate(
            contracts=RunValidationContracts(
                FileSystemSessionOutput(), sentinel_registry()
            ),
            command_runner=RecordingCommandRunner(),
            working_copy=StubWorkingCopy(CANDIDATE),
            attempts=SidecarAttemptStore(worktree.parent),
            attempt_keys=StubAttemptKeys(),
            diagnostics=GateFailureDiagnostics(worktree.parent),
        )

        outcome = gate.check(
            worktree=worktree,
            run_assets=run,
            issue_key=GitHubIssueKey(repo=REPO, external_id=ISSUE),
        )

        assert outcome.allowed is False
        assert outcome.cache_hit is False
        assert outcome.evidence.paths.record_path.parent == (
            run.run_dir / "publish-gate"
        )
