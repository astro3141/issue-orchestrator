"""Trusted-runtime promotion is bound to live-assurance PASS (#194).

Covers required failure-direction proof 5 (promoting without a live-assurance
PASS for that exact artifact fails), the store the gate reads it through, and
the operator-facing command that renders the refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.adapters.json_live_assurance_store import (
    LIVE_ASSURANCE_DIR,
    JsonLiveAssuranceStore,
)
from issue_orchestrator.control.trusted_runtime_promotion import (
    TrustedRuntimePromotion,
    TrustedRuntimePromotionRefused,
)
from issue_orchestrator.domain.live_assurance import (
    LIVE_ASSURANCE_SUITE,
    LiveAssuranceOutcome,
    LiveAssuranceRecord,
)
from issue_orchestrator.entrypoints.cli_tools import trusted_runtime_promote
from issue_orchestrator.ports.live_assurance_store import LiveAssuranceStore

ARTIFACT = "c" * 40
OTHER_ARTIFACT = "d" * 40


def _record(
    outcome: LiveAssuranceOutcome,
    *,
    head_sha: str = ARTIFACT,
    working_tree_dirty: bool = False,
    probes_executed: int = 3,
) -> LiveAssuranceRecord:
    return LiveAssuranceRecord(
        head_sha=head_sha,
        outcome=outcome,
        detail=f"lane said {outcome.value}",
        working_tree_dirty=working_tree_dirty,
        probes_executed=probes_executed,
    )


@pytest.fixture
def store(tmp_path: Path) -> JsonLiveAssuranceStore:
    return JsonLiveAssuranceStore(tmp_path)


class TestPromotionIsBound:
    """Proof 5: no PASS for this exact artifact, no promotion."""

    def test_an_artifact_the_lane_never_ran_on_is_refused(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        with pytest.raises(
            TrustedRuntimePromotionRefused, match="no live-assurance record"
        ):
            TrustedRuntimePromotion(store).admit(ARTIFACT)

    @pytest.mark.parametrize(
        "outcome",
        [LiveAssuranceOutcome.INCONCLUSIVE, LiveAssuranceOutcome.SECURITY_FAIL],
    )
    def test_a_non_pass_outcome_is_refused_and_named(
        self, store: JsonLiveAssuranceStore, outcome: LiveAssuranceOutcome
    ) -> None:
        store.record(_record(outcome))

        with pytest.raises(TrustedRuntimePromotionRefused, match=outcome.value):
            TrustedRuntimePromotion(store).admit(ARTIFACT)

    def test_another_artifacts_pass_does_not_admit_this_one(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        store.record(_record(LiveAssuranceOutcome.PASS, head_sha=OTHER_ARTIFACT))

        with pytest.raises(
            TrustedRuntimePromotionRefused, match="no live-assurance record"
        ):
            TrustedRuntimePromotion(store).admit(ARTIFACT)

    def test_a_pass_recorded_from_a_modified_tree_is_refused(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        """The probes ran on something this commit does not name."""
        store.record(_record(LiveAssuranceOutcome.PASS, working_tree_dirty=True))

        with pytest.raises(
            TrustedRuntimePromotionRefused, match="modified working tree"
        ):
            TrustedRuntimePromotion(store).admit(ARTIFACT)

    def test_a_pass_for_the_exact_artifact_admits(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        store.record(_record(LiveAssuranceOutcome.PASS))

        assert TrustedRuntimePromotion(store).admit(ARTIFACT) == ARTIFACT

    def test_the_admitted_artifact_is_the_key_the_record_is_filed_under(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        """So a caller reporting success cannot name a different spelling."""
        store.record(_record(LiveAssuranceOutcome.PASS))

        assert TrustedRuntimePromotion(store).admit(ARTIFACT.upper()) == ARTIFACT

    def test_an_abbreviated_sha_is_refused_before_any_lookup(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        with pytest.raises(ValueError, match="40-character hex SHA"):
            TrustedRuntimePromotion(store).admit(ARTIFACT[:12])

    def test_a_publication_receipt_cannot_be_planted_as_assurance_evidence(
        self, tmp_path: Path, store: JsonLiveAssuranceStore
    ) -> None:
        """Proof 6, at the gate: a publish_gate pass placed in the lane's
        directory is refused rather than admitting a promotion."""
        path = tmp_path / LIVE_ASSURANCE_DIR / f"{ARTIFACT}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "suite": "publish_gate",
                "head_sha": ARTIFACT,
                "outcome": "pass",
                "detail": "publication contract passed",
                "working_tree_dirty": False,
                "probes_executed": 3,
            }),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="must not be a validation suite"):
            TrustedRuntimePromotion(store).admit(ARTIFACT)


class TestTheStore:
    def test_it_satisfies_the_port(self, store: JsonLiveAssuranceStore) -> None:
        assert isinstance(store, LiveAssuranceStore)

    def test_an_artifact_with_no_record_reads_as_none(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        assert store.for_artifact(ARTIFACT) is None

    def test_a_record_round_trips_under_the_artifact_that_names_it(
        self, tmp_path: Path, store: JsonLiveAssuranceStore
    ) -> None:
        record = _record(LiveAssuranceOutcome.PASS)
        store.record(record)

        assert (tmp_path / LIVE_ASSURANCE_DIR / f"{ARTIFACT}.json").is_file()
        assert store.for_artifact(ARTIFACT) == record
        assert store.for_artifact(ARTIFACT.upper()) == record

    def test_it_writes_beside_validation_records_not_among_them(
        self, tmp_path: Path, store: JsonLiveAssuranceStore
    ) -> None:
        """Two lanes, two locations: the validation cache's glob must not
        reach an assurance record, nor this store a validation one."""
        store.record(_record(LiveAssuranceOutcome.PASS))

        assert not (tmp_path / ".issue-orchestrator" / "validation").exists()
        assert LIVE_ASSURANCE_DIR.parts[-1] == "live-assurance"

    def test_a_corrupt_record_raises_rather_than_reading_as_absent(
        self, tmp_path: Path, store: JsonLiveAssuranceStore
    ) -> None:
        path = tmp_path / LIVE_ASSURANCE_DIR / f"{ARTIFACT}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError, match="unreadable"):
            store.for_artifact(ARTIFACT)

    def test_a_record_filed_under_the_wrong_artifact_raises(
        self, tmp_path: Path, store: JsonLiveAssuranceStore
    ) -> None:
        path = tmp_path / LIVE_ASSURANCE_DIR / f"{ARTIFACT}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_record(LiveAssuranceOutcome.PASS, head_sha=OTHER_ARTIFACT).to_payload()),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="names a different artifact"):
            store.for_artifact(ARTIFACT)

    def test_the_record_it_writes_carries_the_lanes_own_suite(
        self, store: JsonLiveAssuranceStore
    ) -> None:
        store.record(_record(LiveAssuranceOutcome.PASS))

        stored = store.for_artifact(ARTIFACT)
        assert stored is not None
        assert stored.suite == LIVE_ASSURANCE_SUITE


class TestThePromotionCommand:
    def test_it_exits_nonzero_and_says_why_when_nothing_was_recorded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = trusted_runtime_promote.main(
            ["--head-sha", ARTIFACT, "--root", str(tmp_path)]
        )

        assert exit_code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_it_exits_nonzero_on_an_inconclusive_lane_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        JsonLiveAssuranceStore(tmp_path).record(
            _record(LiveAssuranceOutcome.INCONCLUSIVE)
        )

        exit_code = trusted_runtime_promote.main(
            ["--head-sha", ARTIFACT, "--root", str(tmp_path)]
        )

        assert exit_code == 1
        assert "inconclusive" in capsys.readouterr().err

    def test_it_exits_zero_on_a_pass_for_the_exact_artifact(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        JsonLiveAssuranceStore(tmp_path).record(_record(LiveAssuranceOutcome.PASS))

        exit_code = trusted_runtime_promote.main(
            ["--head-sha", ARTIFACT, "--root", str(tmp_path)]
        )

        assert exit_code == 0
        assert "ADMITTED" in capsys.readouterr().out

    def test_a_malformed_request_is_a_different_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad request must not read as a refusal, nor a refusal as a bug."""
        exit_code = trusted_runtime_promote.main(
            ["--head-sha", "not-a-sha", "--root", str(tmp_path)]
        )

        assert exit_code == 2
        assert "Error" in capsys.readouterr().err
