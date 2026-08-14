"""Behavior of the publication-gate verdict owner (#45)."""

from unittest.mock import Mock

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publication_authority import (
    PublicationAuthority,
    UnrecordedRefusals,
    publication_gate_failed,
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
    return UnrecordedRefusals()


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
        unrecorded=UnrecordedRefusals(),
    )
    # The unprefixed spelling belongs to a differently-configured deployment
    # and must not be mistaken for this one's verdict.
    assert not publication_gate_failed(
        label_manager,
        ["validation-failed"],
        issue_number=40,
        unrecorded=UnrecordedRefusals(),
    )


def test_an_unrecorded_refusal_reads_as_a_failed_gate():
    config = Config()
    label_manager = LabelManager(config)
    unrecorded = UnrecordedRefusals()
    unrecorded.hold(40)

    assert publication_gate_failed(
        label_manager, ["agent:backend"], issue_number=40, unrecorded=unrecorded
    )
    assert not publication_gate_failed(
        label_manager, ["agent:backend"], issue_number=41, unrecorded=unrecorded
    )
