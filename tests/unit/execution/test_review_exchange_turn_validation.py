"""Who files a review-exchange round's validation evidence (#388).

The exchange judges every round on the same fact — a passing
``validation-record.json`` naming the coder worktree's current HEAD — and until
#388 only the coder's own ``coding-done`` could produce one. A Tech Lead
occupying the coder SIDE cannot: the gate that writes it needs the same
host/shared-repository effects #364 measured a bounded Tech Lead dying on, and
#370/#385 already moved that ownership on the primary lane.

These tests pin the repair from both directions:

* the trusted lane really asks the trusted owner, binds its answer to the exact
  exchange run/session/commit, and publishes only a verdict that passed (F3);
* every way the answer can fail to be a pass — failed, timed out, unavailable,
  a raising owner, evidence naming another candidate, an unobservable HEAD —
  refuses the round and leaves no evidence behind (F4);
* the Actor lane is untouched (F5), and a ``needs_human`` turn that offers no
  change for review still reaches its own terminal rather than a validation
  refusal (F7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.review_exchange_coder_principal import (
    ReviewExchangeCoderPrincipal,
)
from issue_orchestrator.domain.tech_lead_completion_validation import (
    TechLeadCompletionValidation,
    TechLeadCompletionValidationStatus,
)
from issue_orchestrator.execution import (
    review_exchange_validation_mirror as validation_mirror,
)
from issue_orchestrator.execution.review_exchange_coder_turn import (
    CoderTurnRead,
    read_coder_turn,
)
from issue_orchestrator.execution.review_exchange_turn_validation import (
    TRUSTED_TURN_VALIDATION_SUITE,
    CoderFiledTurnEvidence,
    TrustedTurnEvidence,
    build_turn_evidence,
)
from issue_orchestrator.execution.review_exchange_validation_mirror import (
    PairValidationMirror,
)
from issue_orchestrator.control.tech_lead_completion_validation import (
    UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR,
)

RUN_ID = "20260901-000000Z"
SESSION_NAME = "review-exchange-388"
HEAD = "a" * 40


@dataclass
class RecordingValidator:
    """A trusted owner that answers exactly what the test tells it to."""

    status: TechLeadCompletionValidationStatus = (
        TechLeadCompletionValidationStatus.PASSED
    )
    detail: str = "publishable tree"
    override_key: tuple[str, str, str] | None = None
    raises: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)

    def validate_completion(
        self,
        *,
        run_id: str,
        session_name: str,
        worktree: Path,
        candidate_head_sha: str,
    ) -> TechLeadCompletionValidation:
        self.calls.append(
            {
                "run_id": run_id,
                "session_name": session_name,
                "worktree": worktree,
                "candidate_head_sha": candidate_head_sha,
            }
        )
        if self.raises:
            raise RuntimeError("the owner is broken")
        key = self.override_key or (run_id, session_name, candidate_head_sha)
        return TechLeadCompletionValidation(
            run_id=key[0],
            session_name=key[1],
            candidate_head_sha=key[2],
            status=self.status,
            detail=self.detail,
            recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PairValidationMirror:
    coder_wt = tmp_path / "coder-wt"
    pair_dir = coder_wt / ".issue-orchestrator" / "persistent-pairs" / "issue-388"
    run_record = tmp_path / "run" / "validation-record.json"
    monkeypatch.setattr(validation_mirror, "get_repo_head_sha", lambda _: HEAD)
    return PairValidationMirror(
        pair_dir=pair_dir,
        record_path=pair_dir / "validation-record.json",
        coder_worktree_path=coder_wt,
        run_record_path=run_record,
    )


def _trusted(
    mirror: PairValidationMirror, validator: object
) -> TrustedTurnEvidence:
    return TrustedTurnEvidence(
        mirror=mirror,
        validator=validator,  # type: ignore[arg-type]
        run_id=RUN_ID,
        session_name=SESSION_NAME,
        coder_worktree=mirror.coder_worktree_path,
        validation_profile="default",
    )


class TestTheTrustedOwnerProducesTheEvidence:
    """F3: the trusted owner executes, and its answer binds to this candidate."""

    def test_it_is_asked_about_this_exchange_run_and_the_current_head(
        self, mirror: PairValidationMirror
    ) -> None:
        validator = RecordingValidator()

        assert _trusted(mirror, validator).file_for_turn({}) is None

        assert validator.calls == [
            {
                "run_id": RUN_ID,
                "session_name": SESSION_NAME,
                "worktree": mirror.coder_worktree_path,
                "candidate_head_sha": HEAD,
            }
        ]

    def test_a_passing_verdict_becomes_the_pairs_current_evidence(
        self, mirror: PairValidationMirror
    ) -> None:
        _trusted(mirror, RecordingValidator()).file_for_turn({})

        record = json.loads(mirror.record_path.read_text(encoding="utf-8"))
        assert record["passed"] is True
        assert record["head_sha"] == HEAD
        assert mirror.current_validation_error() is None

    def test_the_record_names_the_contract_that_actually_ran(
        self, mirror: PairValidationMirror
    ) -> None:
        """Not ``agent_gate``: a different owner ran a different contract."""
        _trusted(mirror, RecordingValidator()).file_for_turn({})

        record = json.loads(mirror.record_path.read_text(encoding="utf-8"))
        assert record["suite"] == TRUSTED_TURN_VALIDATION_SUITE
        assert record["suite"] not in {"agent_gate", "publish_gate"}

    def test_the_evidence_reaches_the_exchange_runs_record_too(
        self, mirror: PairValidationMirror
    ) -> None:
        _trusted(mirror, RecordingValidator()).file_for_turn({})

        assert mirror.run_record_path is not None
        assert json.loads(mirror.run_record_path.read_text(encoding="utf-8"))[
            "head_sha"
        ] == HEAD

    def test_the_model_cannot_point_the_lane_at_its_own_record(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        """The completion payload is not a source of evidence on this lane."""
        planted = mirror.coder_worktree_path / "planted.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(
            json.dumps({"passed": True, "head_sha": HEAD, "suite": "agent_gate"}),
            encoding="utf-8",
        )
        validator = RecordingValidator(
            status=TechLeadCompletionValidationStatus.FAILED, detail="dirty tree"
        )

        refusal = _trusted(mirror, validator).file_for_turn(
            {"validation_record_path": str(planted)}
        )

        assert refusal is not None
        assert not mirror.record_path.exists()


class TestEveryFailureDirectionRefuses:
    """F4: nothing but a bound, passing verdict settles the round."""

    @pytest.mark.parametrize(
        "status",
        [
            TechLeadCompletionValidationStatus.FAILED,
            TechLeadCompletionValidationStatus.TIMED_OUT,
            TechLeadCompletionValidationStatus.UNAVAILABLE,
        ],
    )
    def test_a_non_passing_verdict_is_refused_and_clears_the_record(
        self,
        mirror: PairValidationMirror,
        status: TechLeadCompletionValidationStatus,
    ) -> None:
        _trusted(mirror, RecordingValidator()).file_for_turn({})
        assert mirror.record_path.exists()

        refusal = _trusted(
            mirror, RecordingValidator(status=status, detail="because")
        ).file_for_turn({})

        assert refusal is not None
        assert f"validation_{status.value}" in refusal
        assert "because" in refusal
        assert not mirror.record_path.exists()

    def test_an_owner_that_raises_is_refused_rather_than_believed(
        self, mirror: PairValidationMirror
    ) -> None:
        refusal = _trusted(mirror, RecordingValidator(raises=True)).file_for_turn({})

        assert refusal is not None
        assert "validation_unavailable" in refusal
        assert "failed to produce a verdict" in refusal
        assert not mirror.record_path.exists()

    def test_an_unwired_owner_refuses_instead_of_passing_unvalidated(
        self, mirror: PairValidationMirror
    ) -> None:
        refusal = _trusted(
            mirror, UNWIRED_TECH_LEAD_COMPLETION_VALIDATOR
        ).file_for_turn({})

        assert refusal is not None
        assert (
            f"validation_{TechLeadCompletionValidationStatus.UNAVAILABLE.value}"
        ) in refusal

    @pytest.mark.parametrize(
        "override_key",
        [
            ("another-run", SESSION_NAME, HEAD),
            (RUN_ID, "another-session", HEAD),
            (RUN_ID, SESSION_NAME, "b" * 40),
        ],
        ids=["run", "session", "commit"],
    )
    def test_a_verdict_about_another_candidate_is_not_evidence_about_this_one(
        self, mirror: PairValidationMirror, override_key: tuple[str, str, str]
    ) -> None:
        refusal = _trusted(
            mirror, RecordingValidator(override_key=override_key)
        ).file_for_turn({})

        assert refusal is not None
        assert "candidate_drift" in refusal
        assert not mirror.record_path.exists()

    def test_an_unobservable_head_has_nothing_to_bind_to(
        self, mirror: PairValidationMirror, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(validation_mirror, "get_repo_head_sha", lambda _: None)
        validator = RecordingValidator()

        refusal = _trusted(mirror, validator).file_for_turn({})

        assert refusal is not None
        assert "no candidate to bind to" in refusal
        assert validator.calls == []


class TestTheExchangeOpensOnTrustedEvidence:
    """The reviewer moves first, so the opening evidence is the owner's too."""

    def test_the_trusted_lane_seeds_from_the_owner_not_the_caller(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        stale = tmp_path / "caller-record.json"
        stale.write_text(
            json.dumps({"passed": True, "head_sha": "c" * 40}), encoding="utf-8"
        )
        validator = RecordingValidator()

        _trusted(mirror, validator).seed(stale)

        assert json.loads(mirror.record_path.read_text(encoding="utf-8"))[
            "head_sha"
        ] == HEAD
        assert validator.calls

    def test_a_refused_seed_leaves_no_evidence_and_does_not_raise(
        self, mirror: PairValidationMirror
    ) -> None:
        _trusted(
            mirror,
            RecordingValidator(status=TechLeadCompletionValidationStatus.FAILED),
        ).seed(None)

        assert not mirror.record_path.exists()

    def test_the_actor_lane_still_seeds_from_its_own_completion(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        """F5: nothing about the Actor's opening evidence moves."""
        source = tmp_path / "caller-record.json"
        payload = {"passed": True, "head_sha": HEAD}
        source.write_text(json.dumps(payload), encoding="utf-8")

        CoderFiledTurnEvidence(
            mirror=mirror, run_validation_record_path=tmp_path / "absent.json"
        ).seed(source)

        assert json.loads(mirror.record_path.read_text(encoding="utf-8")) == payload


class TestTheOwnerIsSelectedByThePrincipal:
    def test_an_actor_files_its_own_turn_validation(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        owner = build_turn_evidence(
            ReviewExchangeCoderPrincipal.ACTOR,
            mirror=mirror,
            run_validation_record_path=tmp_path / "run-record.json",
            validator=RecordingValidator(),  # type: ignore[arg-type]
            run_id=RUN_ID,
            session_name=SESSION_NAME,
            coder_worktree=mirror.coder_worktree_path,
            validation_profile="default",
        )

        assert isinstance(owner, CoderFiledTurnEvidence)

    def test_a_tech_lead_does_not(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        owner = build_turn_evidence(
            ReviewExchangeCoderPrincipal.TECH_LEAD,
            mirror=mirror,
            run_validation_record_path=tmp_path / "run-record.json",
            validator=RecordingValidator(),  # type: ignore[arg-type]
            run_id=RUN_ID,
            session_name=SESSION_NAME,
            coder_worktree=mirror.coder_worktree_path,
            validation_profile="default",
        )

        assert isinstance(owner, TrustedTurnEvidence)


def _write_completion(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestTheRoundGateReadsTheTrustedOwnersAnswer:
    def test_a_refused_verdict_becomes_the_turns_protocol_error(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        completion = _write_completion(
            tmp_path / "completion-coder.json",
            {"outcome": "completed", "requested_actions": ["create_pr"]},
        )

        disposition = read_coder_turn(
            CoderTurnRead(
                completion_path=completion,
                pair_validation=mirror,
                turn_evidence=_trusted(
                    mirror,
                    RecordingValidator(
                        status=TechLeadCompletionValidationStatus.FAILED,
                        detail="dirty tree",
                    ),
                ),
                require_validation=True,
                issue_number=388,
                session_name=SESSION_NAME,
                round_index=1,
            )
        )

        assert disposition.escalation is None
        assert disposition.protocol_error is not None
        assert "dirty tree" in disposition.protocol_error

    def test_a_passing_verdict_lets_the_turn_continue(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        completion = _write_completion(
            tmp_path / "completion-coder.json",
            {"outcome": "completed", "requested_actions": ["create_pr"]},
        )

        disposition = read_coder_turn(
            CoderTurnRead(
                completion_path=completion,
                pair_validation=mirror,
                turn_evidence=_trusted(mirror, RecordingValidator()),
                require_validation=True,
                issue_number=388,
                session_name=SESSION_NAME,
                round_index=1,
            )
        )

        assert disposition.protocol_error is None
        assert disposition.escalation is None

    def test_an_escalation_that_offers_no_change_still_reaches_its_terminal(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        """F7: #386's terminal is not swallowed by a validation refusal.

        A ``needs_human`` turn asks only that its branch be preserved, so it
        owes no publication evidence — and a trusted owner that refuses does
        not turn the question into a failure.
        """
        completion = _write_completion(
            tmp_path / "completion-coder.json",
            {
                "outcome": "needs_human",
                "requested_actions": ["push_branch"],
                "question": "who owns this decision?",
            },
        )

        disposition = read_coder_turn(
            CoderTurnRead(
                completion_path=completion,
                pair_validation=mirror,
                turn_evidence=_trusted(
                    mirror,
                    RecordingValidator(
                        status=TechLeadCompletionValidationStatus.UNAVAILABLE
                    ),
                ),
                require_validation=True,
                issue_number=388,
                session_name=SESSION_NAME,
                round_index=1,
            )
        )

        assert disposition.protocol_error is None
        assert disposition.escalation is not None
        assert disposition.escalation.head_sha == HEAD
        assert disposition.escalation.offered_a_change_for_review is False

    def test_an_escalation_that_also_asks_to_publish_keeps_every_prerequisite(
        self, mirror: PairValidationMirror, tmp_path: Path
    ) -> None:
        completion = _write_completion(
            tmp_path / "completion-coder.json",
            {
                "outcome": "needs_human",
                "requested_actions": ["push_branch", "create_pr"],
                "question": "who owns this decision?",
            },
        )

        disposition = read_coder_turn(
            CoderTurnRead(
                completion_path=completion,
                pair_validation=mirror,
                turn_evidence=_trusted(
                    mirror,
                    RecordingValidator(
                        status=TechLeadCompletionValidationStatus.FAILED,
                        detail="dirty tree",
                    ),
                ),
                require_validation=True,
                issue_number=388,
                session_name=SESSION_NAME,
                round_index=1,
            )
        )

        assert disposition.escalation is None
        assert disposition.protocol_error is not None
