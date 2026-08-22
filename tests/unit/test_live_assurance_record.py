"""The live-assurance record's three outcomes and its evidence identity (#194).

Covers required failure-direction proofs 4 (the three outcomes are distinct),
6 (no evidence crossover, in both directions) and 8 (nothing here has retry
semantics).
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.live_assurance import (
    LIVE_ASSURANCE_SCHEMA_VERSION,
    LIVE_ASSURANCE_SUITE,
    LiveAssuranceOutcome,
    LiveAssuranceRecord,
)
from issue_orchestrator.domain.validation_profile import ValidationGateKind
from issue_orchestrator.domain.validation_verdict_receipt import (
    ValidationVerdict,
    ValidationVerdictReceipt,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _record(
    outcome: LiveAssuranceOutcome = LiveAssuranceOutcome.PASS,
    *,
    head_sha: str = SHA_A,
    detail: str = "3 live-agent probe(s) passed",
    suite: str = LIVE_ASSURANCE_SUITE,
    working_tree_dirty: bool = False,
    probes_executed: int = 3,
) -> LiveAssuranceRecord:
    return LiveAssuranceRecord(
        head_sha=head_sha,
        outcome=outcome,
        detail=detail,
        working_tree_dirty=working_tree_dirty,
        probes_executed=probes_executed,
        suite=suite,
    )


class TestTheThreeOutcomesAreDistinct:
    """Proof 4: a probe that never ran, and one that breached, are not a PASS."""

    def test_a_probe_that_never_ran_is_inconclusive(self) -> None:
        assert (
            LiveAssuranceOutcome.observed(breached=False, incomplete=True)
            is LiveAssuranceOutcome.INCONCLUSIVE
        )

    def test_a_probe_that_ran_and_breached_is_security_fail(self) -> None:
        assert (
            LiveAssuranceOutcome.observed(breached=True, incomplete=False)
            is LiveAssuranceOutcome.SECURITY_FAIL
        )

    def test_a_breach_beside_an_incomplete_probe_is_still_security_fail(self) -> None:
        """A proven breach must not hide behind an unrelated provider hiccup."""
        assert (
            LiveAssuranceOutcome.observed(breached=True, incomplete=True)
            is LiveAssuranceOutcome.SECURITY_FAIL
        )

    def test_only_a_complete_unbreached_run_is_a_pass(self) -> None:
        assert (
            LiveAssuranceOutcome.observed(breached=False, incomplete=False)
            is LiveAssuranceOutcome.PASS
        )

    @pytest.mark.parametrize(
        "outcome",
        [LiveAssuranceOutcome.INCONCLUSIVE, LiveAssuranceOutcome.SECURITY_FAIL],
    )
    def test_a_non_pass_record_assures_nothing(
        self, outcome: LiveAssuranceOutcome
    ) -> None:
        assert _record(outcome).assures(SHA_A) is False

    def test_a_pass_assures_only_the_artifact_it_names(self) -> None:
        record = _record(LiveAssuranceOutcome.PASS)

        assert record.assures(SHA_A) is True
        assert record.assures(SHA_B) is False

    def test_a_pass_from_a_modified_tree_assures_nothing(self) -> None:
        """A SHA names a tree only when nothing uncommitted was in it.

        The lane is run mid-change on purpose — that is when sandbox work
        happens — so the record has to carry what it observed rather than file
        a proof of a commit the probes never exercised.
        """
        record = _record(LiveAssuranceOutcome.PASS, working_tree_dirty=True)

        assert record.outcome is LiveAssuranceOutcome.PASS
        assert record.assures(SHA_A) is False
        assert "modified working tree" in str(record.why_not_assuring(SHA_A))

    def test_a_breach_from_a_modified_tree_still_reports_the_breach(self) -> None:
        """Bookkeeping must not shadow a security result in the refusal."""
        record = _record(LiveAssuranceOutcome.SECURITY_FAIL, working_tree_dirty=True)

        assert record.why_not_assuring(SHA_A) == (
            "the lane recorded security_fail, not pass"
        )

    def test_a_record_that_assures_gives_no_reason_not_to(self) -> None:
        assert _record(LiveAssuranceOutcome.PASS).why_not_assuring(SHA_A) is None

    def test_the_dirty_flag_is_neither_optional_nor_coerced(self) -> None:
        """A forgotten or truthy-string argument must not read as "clean"."""
        with pytest.raises(TypeError):
            LiveAssuranceRecord(  # type: ignore[call-arg]
                head_sha=SHA_A, outcome=LiveAssuranceOutcome.PASS, detail="ran"
            )
        with pytest.raises(TypeError, match="working_tree_dirty must be bool"):
            LiveAssuranceRecord(
                head_sha=SHA_A,
                outcome=LiveAssuranceOutcome.PASS,
                detail="ran",
                working_tree_dirty="no",  # type: ignore[arg-type]
                probes_executed=1,
            )

    def test_a_pass_that_executed_nothing_is_refused(self) -> None:
        """The vacuous pass, closed at the record and not only at the lane.

        The lane already reduces an empty selection to ``INCONCLUSIVE``, but a
        record is also written and read as a file. A ``PASS`` naming zero
        probes proves nothing whatever produced it.
        """
        with pytest.raises(ValueError, match="must have executed at least one probe"):
            _record(LiveAssuranceOutcome.PASS, probes_executed=0)

    @pytest.mark.parametrize(
        "outcome",
        [LiveAssuranceOutcome.INCONCLUSIVE, LiveAssuranceOutcome.SECURITY_FAIL],
    )
    def test_a_non_pass_may_legitimately_have_executed_nothing(
        self, outcome: LiveAssuranceOutcome
    ) -> None:
        """An empty selection and an unavailable provider are exactly that."""
        assert _record(outcome, probes_executed=0).probes_executed == 0

    def test_the_executed_count_is_neither_a_bool_nor_negative(self) -> None:
        with pytest.raises(TypeError, match="probes_executed must be int"):
            _record(probes_executed=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="probes_executed must not be negative"):
            _record(probes_executed=-1)

    def test_there_is_no_fourth_outcome(self) -> None:
        """"Never ran" is the absence of a record, not a member inside one."""
        assert {member.value for member in LiveAssuranceOutcome} == {
            "pass",
            "security_fail",
            "inconclusive",
        }


class TestNoEvidenceCrossover:
    """Proof 6: neither lane's evidence can be read as the other's."""

    @pytest.mark.parametrize("suite", ["publish_gate", "quick_gate", "agent_gate"])
    def test_the_validation_vocabulary_owns_its_own_suites(self, suite: str) -> None:
        """Otherwise the guard below would pass by recognising nothing at all."""
        assert ValidationGateKind.defines(suite) is True

    def test_the_assurance_suite_is_not_a_validation_suite(self) -> None:
        assert ValidationGateKind.defines(LIVE_ASSURANCE_SUITE) is False

    @pytest.mark.parametrize("suite", ["publish_gate", "quick_gate", "agent_gate"])
    def test_a_record_may_not_claim_a_validation_suite(self, suite: str) -> None:
        with pytest.raises(ValueError, match="must not be a validation suite"):
            _record(suite=suite)

    @pytest.mark.parametrize("suite", ["publish_gate", "quick_gate", "agent_gate"])
    def test_a_stored_payload_claiming_a_validation_suite_is_refused(
        self, suite: str
    ) -> None:
        """The parse door is closed too, not only the constructor door."""
        payload = _record().to_payload() | {"suite": suite}

        with pytest.raises(ValueError, match="must not be a validation suite"):
            LiveAssuranceRecord.from_payload(payload)

    def test_a_record_may_not_invent_a_third_suite_name(self) -> None:
        with pytest.raises(ValueError, match="live-assurance suite must be"):
            _record(suite="made_up_gate")

    def test_an_assurance_suite_never_certifies_publication(self) -> None:
        """A verdict receipt wearing the assurance label certifies nothing."""
        receipt = ValidationVerdictReceipt(
            suite=LIVE_ASSURANCE_SUITE,
            head_sha=SHA_A,
            verdict=ValidationVerdict.PASSED,
            command="make test-live-assurance",
            profile="default",
        )

        assert receipt.from_publication_contract is False
        assert receipt.certifies_publication(SHA_A) is False

    def test_a_publication_verdict_is_not_a_live_assurance_outcome(self) -> None:
        """Proof 8's half: an INCONCLUSIVE cannot be read as a gate verdict."""
        verdicts = {member.value for member in ValidationVerdict}
        outcomes = {member.value for member in LiveAssuranceOutcome}

        assert verdicts.isdisjoint(outcomes)


class TestTheRecordRefusesWhatItCannotReadExactly:
    def test_an_abbreviated_sha_is_refused(self) -> None:
        with pytest.raises(ValueError, match="40-character hex SHA"):
            _record(head_sha="abc1234")

    def test_a_blank_detail_is_refused(self) -> None:
        """An INCONCLUSIVE whose reason was dropped preserves no observation."""
        with pytest.raises(ValueError, match="detail must be a non-empty str"):
            _record(detail="   ")

    def test_an_unknown_schema_version_fails_closed(self) -> None:
        payload = _record().to_payload() | {"schema_version": 99}

        with pytest.raises(ValueError, match="schema_version must be"):
            LiveAssuranceRecord.from_payload(payload)

    def test_an_unknown_outcome_is_refused_rather_than_defaulted(self) -> None:
        payload = _record().to_payload() | {"outcome": "probably_fine"}

        with pytest.raises(ValueError, match="unknown live-assurance outcome"):
            LiveAssuranceRecord.from_payload(payload)

    @pytest.mark.parametrize(
        "field_name",
        ["suite", "head_sha", "detail", "working_tree_dirty", "probes_executed"],
    )
    def test_a_missing_field_names_itself(self, field_name: str) -> None:
        payload = _record().to_payload()
        del payload[field_name]

        with pytest.raises(ValueError, match=f"requires {field_name}"):
            LiveAssuranceRecord.from_payload(payload)

    def test_a_non_bool_dirty_flag_is_refused_rather_than_read_as_clean(self) -> None:
        payload = _record().to_payload() | {"working_tree_dirty": "false"}

        with pytest.raises(ValueError, match="working_tree_dirty must be bool"):
            LiveAssuranceRecord.from_payload(payload)

    def test_a_stored_executed_count_that_is_not_an_int_is_refused(self) -> None:
        """Including ``true``, which would otherwise read back as one probe."""
        for stored in ("3", True, 2.0, None):
            payload = _record().to_payload() | {"probes_executed": stored}

            with pytest.raises(ValueError, match="probes_executed must be int"):
                LiveAssuranceRecord.from_payload(payload)

    @pytest.mark.parametrize("working_tree_dirty", [False, True])
    def test_a_round_trip_preserves_every_meaning(
        self, working_tree_dirty: bool
    ) -> None:
        record = _record(
            LiveAssuranceOutcome.SECURITY_FAIL,
            detail="breach at probe 2",
            working_tree_dirty=working_tree_dirty,
            probes_executed=7,
        )

        assert LiveAssuranceRecord.from_payload(record.to_payload()) == record
        assert record.schema_version == LIVE_ASSURANCE_SCHEMA_VERSION
        assert LiveAssuranceRecord.from_payload(record.to_payload()).probes_executed == 7
