"""Behavior of the publication-gate verdict owner (#45, #51)."""

from unittest.mock import Mock

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publication_authority import (
    PublicationAuthority,
    UnrecordedRefusals,
    publication_gate_failed,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    STORE_FILENAME,
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.config import Config


@pytest.fixture
def labels():
    adapter = Mock()
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    return adapter


@pytest.fixture
def unrecorded():
    return UnrecordedRefusals.process_local()


def _authority(labels, unrecorded) -> PublicationAuthority:
    return PublicationAuthority(labels, "validation-failed", unrecorded)


def test_revoke_records_the_refusal_on_the_issue(labels, unrecorded):
    authority = _authority(labels, unrecorded)

    authority.revoke(41, reason="make validate-pr-raw exited 2")

    labels.add_label.assert_called_once_with(41, "validation-failed")
    assert authority.label == "validation-failed"
    # The label proves it; nothing needs holding in this process.
    assert not unrecorded.holds(41)


def test_grant_releases_the_refusal(labels, unrecorded):
    authority = _authority(labels, unrecorded)

    authority.grant(41)

    labels.remove_label.assert_called_once_with(41, "validation-failed")


def test_grant_that_cannot_write_leaves_review_blocked(labels, unrecorded, caplog):
    """A failed release fails closed: the marker stays, review stays blocked."""
    labels.remove_label.side_effect = RuntimeError("GitHub said no")
    authority = _authority(labels, unrecorded)

    with caplog.at_level("WARNING"):
        authority.grant(41)

    assert "review stays blocked" in caplog.text


def test_grant_that_cannot_write_keeps_an_unrecorded_refusal(labels, unrecorded):
    """The in-process half fails closed on the same terms as the label.

    A release that did not commit has settled nothing, so a refusal held
    because its own write failed must survive it.
    """
    labels.add_label.side_effect = RuntimeError("GitHub said no")
    labels.remove_label.side_effect = RuntimeError("GitHub said no")
    authority = _authority(labels, unrecorded)
    authority.revoke(41, reason="gate failed")

    authority.grant(41)

    assert unrecorded.holds(41)


def test_revoke_that_cannot_write_withholds_review_without_raising(
    labels, unrecorded, caplog
):
    """Fail-closed on a lost write (#45).

    Two obligations, both owed: the caller must still be able to report the
    gate failure through its own result and issue comment (so no exception),
    AND the candidate the gate refused must not stay review-eligible just
    because one label write did not commit.
    """
    labels.add_label.side_effect = RuntimeError("GitHub said no")
    authority = _authority(labels, unrecorded)

    with caplog.at_level("ERROR"):
        authority.revoke(41, reason="gate failed")

    assert unrecorded.holds(41)
    assert "review stays withheld" in caplog.text


def test_a_later_passing_candidate_clears_an_unrecorded_refusal(labels, unrecorded):
    """The refusal belonged to the candidate the gate judged, not the issue."""
    labels.add_label.side_effect = RuntimeError("GitHub said no")
    authority = _authority(labels, unrecorded)
    authority.revoke(41, reason="gate failed")

    authority.grant(41)

    assert not unrecorded.holds(41)


def test_a_later_recorded_refusal_ends_the_in_process_hold(labels, unrecorded):
    """Once the label lands, the durable record is the whole verdict again."""
    labels.add_label.side_effect = [RuntimeError("GitHub said no"), None]
    authority = _authority(labels, unrecorded)
    authority.revoke(41, reason="gate failed")

    authority.revoke(41, reason="gate failed again")

    assert not unrecorded.holds(41)


def test_marker_is_read_through_the_registry_under_a_prefix():
    config = Config()
    config.label_prefix = "bot"
    label_manager = LabelManager(config)

    assert publication_gate_failed(
        label_manager,
        ["agent:backend", "bot:validation-failed"],
        issue_number=40,
        unrecorded=UnrecordedRefusals.process_local(),
    )
    # The unprefixed spelling belongs to a differently-configured deployment
    # and must not be mistaken for this one's verdict.
    assert not publication_gate_failed(
        label_manager,
        ["validation-failed"],
        issue_number=40,
        unrecorded=UnrecordedRefusals.process_local(),
    )


class TestRefusalsSurviveARestart:
    """#51: a refusal the label write lost must outlive the process.

    Every test here restarts the orchestrator by building a SECOND
    ``UnrecordedRefusals`` over the same ledger file, exactly as a new process
    would. An in-process-only hold passes none of them.
    """

    @staticmethod
    def _ledger(tmp_path):
        """A fresh handle on the repository's ledger, as a new process gets."""
        return SqlitePendingWorkClaimStore(tmp_path / STORE_FILENAME)

    @staticmethod
    def _failing_labels():
        adapter = Mock()
        adapter.add_label = Mock(side_effect=RuntimeError("GitHub said no"))
        adapter.remove_label = Mock()
        return adapter

    def test_a_lost_refusal_still_withholds_review_after_a_restart(self, tmp_path):
        labels = self._failing_labels()
        before = UnrecordedRefusals(self._ledger(tmp_path))
        PublicationAuthority(labels, "validation-failed", before).revoke(
            41, reason="gate failed"
        )

        after = UnrecordedRefusals(self._ledger(tmp_path))

        assert after.holds(41)
        assert publication_gate_failed(
            LabelManager(Config()),
            ["agent:backend"],
            issue_number=41,
            unrecorded=after,
        )

    def test_a_later_passing_candidate_releases_it_across_a_restart(self, tmp_path):
        """The latch must not become the permanent block ``grant`` prevents."""
        labels = self._failing_labels()
        before = UnrecordedRefusals(self._ledger(tmp_path))
        authority = PublicationAuthority(labels, "validation-failed", before)
        authority.revoke(41, reason="gate failed")

        authority.grant(41)
        after = UnrecordedRefusals(self._ledger(tmp_path))

        assert not before.holds(41)
        assert not after.holds(41)

    def test_a_refusal_the_label_recorded_latches_nothing(self, tmp_path):
        """The ordinary path is unchanged: the label is the whole record."""
        labels = Mock()
        labels.add_label = Mock()
        labels.remove_label = Mock()
        ledger = self._ledger(tmp_path)
        PublicationAuthority(labels, "validation-failed", UnrecordedRefusals(ledger)).revoke(
            41, reason="gate failed"
        )

        assert ledger.latched_publication_refusals() == frozenset()
        assert not UnrecordedRefusals(self._ledger(tmp_path)).holds(41)

    def test_a_later_recorded_refusal_clears_the_latch(self, tmp_path):
        """Once the label lands it proves the refusal on its own."""
        labels = Mock()
        labels.add_label = Mock(side_effect=[RuntimeError("GitHub said no"), None])
        labels.remove_label = Mock()
        authority = PublicationAuthority(
            labels, "validation-failed", UnrecordedRefusals(self._ledger(tmp_path))
        )
        authority.revoke(41, reason="gate failed")

        authority.revoke(41, reason="gate failed again")

        assert not UnrecordedRefusals(self._ledger(tmp_path)).holds(41)

    def test_refusing_the_same_issue_twice_latches_one_row(self, tmp_path):
        labels = self._failing_labels()
        ledger = self._ledger(tmp_path)
        authority = PublicationAuthority(
            labels, "validation-failed", UnrecordedRefusals(ledger)
        )

        authority.revoke(41, reason="gate failed")
        authority.revoke(41, reason="gate failed again")

        assert ledger.latched_publication_refusals() == frozenset({41})

    def test_a_latch_write_failure_still_withholds_review_in_process(self, caplog):
        """Degrades to the pre-#51 cover loudly, never to "not refused"."""
        latch = Mock()
        latch.latched_publication_refusals = Mock(return_value=frozenset())
        latch.latch_publication_refusal = Mock(side_effect=OSError("disk full"))
        unrecorded = UnrecordedRefusals(latch)

        with caplog.at_level("ERROR"):
            PublicationAuthority(
                self._failing_labels(), "validation-failed", unrecorded
            ).revoke(41, reason="gate failed")

        assert unrecorded.holds(41)
        assert "a restart before a later candidate clears the gate will lose it" in (
            caplog.text
        )

    def test_a_latch_release_failure_keeps_review_withheld(self, caplog):
        """A release that did not commit has settled nothing."""
        latch = Mock()
        latch.latched_publication_refusals = Mock(return_value=frozenset({41}))
        latch.release_publication_refusal = Mock(side_effect=OSError("disk full"))
        unrecorded = UnrecordedRefusals(latch)

        with caplog.at_level("WARNING"):
            unrecorded.release(41)

        assert unrecorded.holds(41)
        assert "review stays withheld" in caplog.text


def test_an_unrecorded_refusal_reads_as_a_failed_gate():
    config = Config()
    label_manager = LabelManager(config)
    unrecorded = UnrecordedRefusals.process_local()
    unrecorded.hold(40)

    assert publication_gate_failed(
        label_manager, ["agent:backend"], issue_number=40, unrecorded=unrecorded
    )
    assert not publication_gate_failed(
        label_manager, ["agent:backend"], issue_number=41, unrecorded=unrecorded
    )
