"""Tests for issue-scoped runtime lifecycle boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.review_exchange_lifecycle import (
    has_active_issue_runtime,
    has_live_issue_review_exchange,
    live_review_exchange_probe,
    terminate_issue_runtime,
)


class _FakeSessionManager:
    def __init__(self, running: set[str]) -> None:
        self.running = set(running)
        self.stopped: list[str] = []

    def exists(self, ref) -> bool:  # noqa: ANN001 - protocol-shaped fake
        return ref.name in self.running

    def stop(self, ref) -> None:  # noqa: ANN001 - protocol-shaped fake
        self.stopped.append(ref.name)
        self.running.discard(ref.name)


def _active_session(terminal_id: str):
    return SimpleNamespace(terminal_id=terminal_id)


class _FakePublishRetryAbandoner:
    def __init__(self) -> None:
        self.abandoned: list[int] = []

    def abandon_issue(self, issue_number: int) -> None:
        self.abandoned.append(issue_number)


def test_terminate_issue_runtime_abandons_publish_retry() -> None:
    """The shared boundary must also abandon in-flight publish retries."""
    publish_recovery = _FakePublishRetryAbandoner()

    terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        pair_registry=None,
        job_supervisor=None,
        publish_recovery=publish_recovery,
    )

    assert publish_recovery.abandoned == [230]


def test_terminate_issue_runtime_without_publish_recovery_is_noop() -> None:
    """Omitting the abandoner keeps the boundary working (backward compatible)."""
    result = terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        pair_registry=None,
        job_supervisor=None,
    )

    assert result.issue_number == 230


def test_terminate_issue_runtime_stops_issue_rework_and_hidden_exchange() -> None:
    pair_registry = Mock()
    job_supervisor = Mock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:230:coding-1"]
    session_manager = _FakeSessionManager({"issue-230", "rework-230", "issue-999"})
    active_sessions = [
        _active_session("issue-230"),
        _active_session("rework-230"),
        _active_session("review-77"),
        _active_session("issue-999"),
    ]

    result = terminate_issue_runtime(
        issue_number=230,
        reason="reset-retry",
        pair_registry=pair_registry,
        job_supervisor=job_supervisor,
        session_manager=session_manager,
        active_sessions=active_sessions,
    )

    pair_registry.release.assert_called_once_with(230, reason="reset-retry")
    job_supervisor.cancel_matching.assert_called_once()
    predicate = job_supervisor.cancel_matching.call_args.args[0]
    assert predicate("review-exchange:230:coding-1")
    assert not predicate("review-exchange:231:coding-1")
    assert session_manager.stopped == ["issue-230", "rework-230"]
    assert result.stopped_session_ids == ("issue-230", "rework-230")
    assert result.cleared_active_session_ids == ("issue-230", "rework-230")
    assert result.cancelled_job_ids == ("review-exchange:230:coding-1",)
    assert [session.terminal_id for session in active_sessions] == [
        "review-77",
        "issue-999",
    ]


def test_terminate_issue_runtime_clears_stale_active_session_records() -> None:
    session_manager = _FakeSessionManager(set())
    active_sessions = [_active_session("issue-230"), _active_session("issue-231")]

    result = terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        pair_registry=None,
        job_supervisor=None,
        session_manager=session_manager,
        active_sessions=active_sessions,
    )

    assert session_manager.stopped == []
    assert result.stopped_session_ids == ()
    assert result.cleared_active_session_ids == ("issue-230",)
    assert [session.terminal_id for session in active_sessions] == ["issue-231"]


def test_terminate_issue_runtime_requires_session_manager_for_active_records() -> None:
    pair_registry = Mock()

    with pytest.raises(RuntimeError, match="without a SessionManager"):
        terminate_issue_runtime(
            issue_number=230,
            reason="reset-retry",
            pair_registry=pair_registry,
            job_supervisor=None,
            session_manager=None,
            active_sessions=[_active_session("issue-230")],
        )

    pair_registry.release.assert_not_called()


class TestHasLiveIssueReviewExchange:
    """The activity counterpart of ``cancel_issue_review_exchange`` (#167).

    A caller that must decide whether cancelling would be a no-op reads this.
    It has to consult the exact two owners the cancellation terminates, or a
    "nothing live here" answer would authorise tearing down live work.
    """

    def test_nothing_wired_is_nothing_live(self) -> None:
        assert not has_live_issue_review_exchange(
            issue_number=230, pair_registry=None, job_supervisor=None
        )

    def test_a_cached_pair_is_live(self) -> None:
        pair_registry = Mock()
        pair_registry.has_active_pair.return_value = True

        assert has_live_issue_review_exchange(
            issue_number=230, pair_registry=pair_registry, job_supervisor=None
        )
        pair_registry.has_active_pair.assert_called_once_with(230)
        # Reading is not releasing: the counterpart must not terminate anything.
        pair_registry.release.assert_not_called()

    def test_a_running_exchange_job_for_this_issue_is_live(self) -> None:
        job_supervisor = Mock()
        job_supervisor.has_matching.return_value = True

        assert has_live_issue_review_exchange(
            issue_number=230, pair_registry=None, job_supervisor=job_supervisor
        )
        predicate = job_supervisor.has_matching.call_args.args[0]
        assert predicate("review-exchange:230:coding-1")
        assert not predicate("review-exchange:231:coding-1")
        job_supervisor.cancel_matching.assert_not_called()

    def test_an_owner_that_raises_reads_as_live(self) -> None:
        """Fail-safe: unverifiable state must not authorise a teardown."""
        pair_registry = Mock()
        pair_registry.has_active_pair.side_effect = RuntimeError("registry down")

        assert has_live_issue_review_exchange(
            issue_number=230, pair_registry=pair_registry, job_supervisor=None
        )

    def test_the_wider_runtime_predicate_agrees_about_the_exchange(self) -> None:
        """``has_active_issue_runtime`` asks this same question, not its own.

        The two must not drift on what a live exchange is: the wider predicate
        adds sessions and publish retry, and nothing else.
        """
        job_supervisor = Mock()
        job_supervisor.has_matching.return_value = True

        assert has_active_issue_runtime(
            issue_number=230, pair_registry=None, job_supervisor=job_supervisor
        )


class TestLiveReviewExchangeProbe:
    """The liveness question, handed out without the owners that answer it (#167).

    ``has_live_issue_review_exchange`` and ``cancel_issue_review_exchange`` take
    the same two owners, so a boundary that must never cancel cannot be given
    them — "ask, then don't cancel" is a discipline, and a discipline can be
    raced or edited away. This factory is what makes that boundary structural.
    """

    def test_it_answers_the_same_question_as_the_predicate(self) -> None:
        pair_registry = Mock()
        pair_registry.has_active_pair.return_value = True

        probe = live_review_exchange_probe(
            pair_registry=pair_registry, job_supervisor=None
        )

        assert probe(230) is True
        assert probe(230) == has_live_issue_review_exchange(
            issue_number=230, pair_registry=pair_registry, job_supervisor=None
        )

    def test_nothing_wired_is_nothing_live(self) -> None:
        probe = live_review_exchange_probe(pair_registry=None, job_supervisor=None)

        assert probe(230) is False

    def test_asking_never_tears_anything_down(self) -> None:
        pair_registry = Mock()
        pair_registry.has_active_pair.return_value = False
        job_supervisor = Mock()
        job_supervisor.has_matching.return_value = False

        probe = live_review_exchange_probe(
            pair_registry=pair_registry, job_supervisor=job_supervisor
        )
        probe(230)

        pair_registry.release.assert_not_called()
        job_supervisor.cancel_matching.assert_not_called()

    def test_an_owner_that_raises_reads_as_live(self) -> None:
        """The fail-safe reading survives being wrapped."""
        job_supervisor = Mock()
        job_supervisor.has_matching.side_effect = RuntimeError("supervisor down")

        probe = live_review_exchange_probe(
            pair_registry=None, job_supervisor=job_supervisor
        )

        assert probe(230) is True

    def test_the_owners_are_not_reachable_through_what_it_returns(self) -> None:
        """The point of the factory: a holder cannot get at the registry.

        Whatever the probe closes over, its public surface is one call taking an
        issue number and returning a bool — there is no attribute a caller could
        follow to ``release`` or ``cancel_matching``.
        """
        pair_registry = Mock()
        job_supervisor = Mock()

        probe = live_review_exchange_probe(
            pair_registry=pair_registry, job_supervisor=job_supervisor
        )

        public_attributes = [name for name in dir(probe) if not name.startswith("_")]
        assert public_attributes == []
