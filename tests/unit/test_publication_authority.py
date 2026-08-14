"""Behavior of the publication-gate verdict owner (#45)."""

from unittest.mock import Mock

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.publication_authority import (
    PublicationAuthority,
    publication_gate_failed,
)
from issue_orchestrator.infra.config import Config


@pytest.fixture
def labels():
    adapter = Mock()
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    return adapter


def test_revoke_records_the_refusal_on_the_issue(labels):
    authority = PublicationAuthority(labels, "validation-failed")

    authority.revoke(41, reason="make validate-pr-raw exited 2")

    labels.add_label.assert_called_once_with(41, "validation-failed")
    assert authority.label == "validation-failed"


def test_grant_releases_the_refusal(labels):
    authority = PublicationAuthority(labels, "validation-failed")

    authority.grant(41)

    labels.remove_label.assert_called_once_with(41, "validation-failed")


def test_grant_that_cannot_write_leaves_review_blocked(labels, caplog):
    """A failed release fails closed: the marker stays, review stays blocked."""
    labels.remove_label.side_effect = RuntimeError("GitHub said no")
    authority = PublicationAuthority(labels, "validation-failed")

    with caplog.at_level("WARNING"):
        authority.grant(41)

    assert "review stays blocked" in caplog.text


def test_revoke_that_cannot_write_does_not_raise(labels):
    """The caller still reports the gate failure through its own result."""
    labels.add_label.side_effect = RuntimeError("GitHub said no")
    authority = PublicationAuthority(labels, "validation-failed")

    authority.revoke(41, reason="gate failed")


def test_marker_is_read_through_the_registry_under_a_prefix():
    config = Config()
    config.label_prefix = "bot"
    label_manager = LabelManager(config)

    assert publication_gate_failed(label_manager, ["agent:backend", "bot:validation-failed"])
    # The unprefixed spelling belongs to a differently-configured deployment
    # and must not be mistaken for this one's verdict.
    assert not publication_gate_failed(label_manager, ["validation-failed"])
