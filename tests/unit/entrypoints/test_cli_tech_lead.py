"""Unit tests for the on-demand tech_lead CLIs (entrypoints/cli_tech_lead.py).

These pin the command wiring — repo-lock hold, main-loop logging, background-E2E
kill switch, the ``--advise-only`` authority dial, and the truthful timeout exit
code — without standing up a real in-process orchestrator (the driver logic is
covered in ``tests/unit/control/test_tech_lead_trigger.py``).
"""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
from unittest.mock import Mock, patch

from issue_orchestrator.control.tech_lead_trigger import (
    HealthReviewResult,
    TechLeadOutcomeStatus,
)
from issue_orchestrator.entrypoints import cli_tech_lead
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_lock import AlreadyRunning


def _args(**overrides) -> argparse.Namespace:
    base = {"advise_only": False, "timeout": 1800.0, "config": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _patches(config: Config):
    """Patch every external boundary cmd_health_review touches except the lock."""
    return (
        patch.object(cli_tech_lead, "load_config", return_value=config),
        patch(
            "issue_orchestrator.infra.logging_config.setup_logging",
            return_value="/tmp/health-review.log",
        ),
    )


def _lock_held_ok():
    """Patch the repo-lock owner to a no-op held lock (context manager)."""
    return patch(
        "issue_orchestrator.infra.repo_lock.held_repo_lock",
        return_value=contextlib.nullcontext(),
    )


class TestCmdHealthReview:
    def test_lock_conflict_returns_1_without_building_orchestrator(self) -> None:
        config = Config()
        load_p, log_p = _patches(config)
        # F6: the command now ACQUIRES + HOLDS the lock; a live holder makes
        # held_repo_lock raise AlreadyRunning before anything is built.
        with load_p, log_p, patch(
            "issue_orchestrator.infra.repo_lock.held_repo_lock",
            side_effect=AlreadyRunning(pid=4321, repo_root=Path("/repo"), port=8080),
        ), patch.object(
            cli_tech_lead, "_build_orchestrator"
        ) as build, patch(
            "issue_orchestrator.control.tech_lead_trigger.run_health_review"
        ) as run:
            rc = cli_tech_lead.cmd_health_review(_args())

        assert rc == 1
        build.assert_not_called()  # never built an orchestrator behind the lock
        run.assert_not_called()

    def test_advise_only_dials_authority_and_disables_e2e(self) -> None:
        config = Config()
        config.e2e.enabled = True  # prove the command turns it off
        orchestrator = Mock()
        load_p, log_p = _patches(config)
        with load_p, log_p, _lock_held_ok(), patch.object(
            cli_tech_lead, "_build_orchestrator", return_value=orchestrator
        ), patch(
            "issue_orchestrator.control.tech_lead_trigger.run_health_review",
            return_value=HealthReviewResult(
                200, status=TechLeadOutcomeStatus.COMPLETED, detail="done"
            ),
        ) as run:
            rc = cli_tech_lead.cmd_health_review(_args(advise_only=True, timeout=42.0))

        assert rc == 0
        # A one-shot run must not fire the heavy background E2E suite.
        assert config.e2e.enabled is False
        # --advise-only dials every tech_lead authority to propose.
        authority = config.tech_lead.authority
        assert authority.post_comment == "propose"
        assert authority.create_issue == "propose"
        assert authority.reset_retry == "propose"
        assert authority.kill_hung_session == "propose"
        # The driver ran against the built orchestrator with the CLI timeout...
        run.assert_called_once()
        assert run.call_args.args[0] is orchestrator
        assert run.call_args.kwargs["timeout_s"] == 42.0
        # ...and the one-shot orchestrator was torn down.
        orchestrator.close.assert_called_once()

    def test_timeout_returns_nonzero_and_still_disables_e2e(self) -> None:
        from issue_orchestrator.control.tech_lead_trigger import TechLeadTerminationOutcome

        config = Config()
        config.e2e.enabled = True
        default_authority = config.tech_lead.authority
        orchestrator = Mock()
        load_p, log_p = _patches(config)
        with load_p, log_p, _lock_held_ok(), patch.object(
            cli_tech_lead, "_build_orchestrator", return_value=orchestrator
        ), patch(
            "issue_orchestrator.control.tech_lead_trigger.run_health_review",
            # A TIMED_OUT outcome now REQUIRES its termination (the invalid
            # launched-but-incomplete-with-no-termination state is gone) — a
            # clean one here, so the command reports a plain terminated timeout.
            return_value=HealthReviewResult(
                200, status=TechLeadOutcomeStatus.TIMED_OUT, detail="timed out",
                termination=TechLeadTerminationOutcome(),
            ),
        ):
            rc = cli_tech_lead.cmd_health_review(_args(advise_only=False))

        # F7: launched-but-not-completed is a TIMEOUT, not success — exit nonzero.
        assert rc == 1
        assert config.e2e.enabled is False
        assert config.tech_lead.authority is default_authority  # not replaced
        orchestrator.close.assert_called_once()

    def test_incomplete_termination_is_surfaced_at_the_command_surface(self) -> None:
        # R7 (#6824): when the timeout termination could NOT clean up (e.g. the
        # scratch worktree leaked), the COMMAND must say so prominently and name
        # the leaked path for operator action — not print "session terminated".
        from issue_orchestrator.control.tech_lead_trigger import TechLeadTerminationOutcome

        config = Config()
        orchestrator = Mock()
        load_p, log_p = _patches(config)
        printed: list[str] = []
        with load_p, log_p, _lock_held_ok(), patch.object(
            cli_tech_lead, "_build_orchestrator", return_value=orchestrator
        ), patch(
            "issue_orchestrator.control.tech_lead_trigger.run_health_review",
            return_value=HealthReviewResult(
                200, status=TechLeadOutcomeStatus.TIMED_OUT, detail="timed out",
                termination=TechLeadTerminationOutcome(
                    terminal_stopped=False, worktree_removed=False,
                    leaked_worktree="/wt/repo-tech-lead-200-abc",
                ),
            ),
        ), patch.object(
            cli_tech_lead.console, "print",
            side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        ):
            rc = cli_tech_lead.cmd_health_review(_args())

        assert rc == 1
        out = "\n".join(printed)
        assert "TERMINATION INCOMPLETE" in out  # not a bare "session terminated"
        assert "/wt/repo-tech-lead-200-abc" in out  # the exact leaked path
        assert "remove it manually" in out


class TestTheOneShotAnswersTheCallbackEndpointQuestion:
    """A one-shot run mode owes the same answer the three ``start`` modes owe.

    Every session launch is gated on the agent-callback endpoint having
    been resolved either way. These commands published no port and
    declared no unavailability, so the gate refused every attempt before
    the launcher reached its first log line and the operator saw only
    "launch declined" (#193).
    """

    def _dispatch(self, command, args, *, on_dispatch):
        """Run ``command`` with a real endpoint, observing it AT dispatch time.

        ``on_dispatch`` runs where the driver would launch a session, which
        is the only moment the assertion is about: answering the question
        after the launch attempt would be no answer at all.
        """
        config = Config()
        orchestrator = Mock()
        orchestrator.deps.agent_callback_endpoint = RuntimeAgentCallbackEndpoint()
        load_p, log_p = _patches(config)
        driver = {
            cli_tech_lead.cmd_tech_lead: "run_targeted_investigations",
            cli_tech_lead.cmd_health_review: "run_health_review",
        }[command]
        with load_p, log_p, _lock_held_ok(), patch.object(
            cli_tech_lead, "_build_orchestrator", return_value=orchestrator
        ), patch(
            f"issue_orchestrator.control.tech_lead_trigger.{driver}",
            side_effect=lambda *a, **k: on_dispatch(orchestrator),
        ) as run:
            command(args)
        run.assert_called_once()
        return orchestrator

    def test_targeted_dispatch_finds_the_endpoint_resolved(self) -> None:
        """The live-proof path: a targeted ``tech_lead`` run (#193)."""
        seen: list[bool] = []

        self._dispatch(
            cli_tech_lead.cmd_tech_lead,
            _args(issues=[23], flavor="planning_investigation"),
            on_dispatch=lambda orch: seen.append(
                orch.deps.agent_callback_endpoint.is_ready()
            )
            or [],
        )

        assert seen == [True], "the launch gate would refuse every attempt"

    def test_health_review_shares_the_same_answer(self) -> None:
        """Same owner, so the whole-board command cannot drift from it.

        This pins the shared wiring only; it is not a claim that the
        one-shot ``health-review`` path is proven end to end.
        """
        seen: list[bool] = []

        self._dispatch(
            cli_tech_lead.cmd_health_review,
            _args(),
            on_dispatch=lambda orch: seen.append(
                orch.deps.agent_callback_endpoint.is_ready()
            )
            or HealthReviewResult(
                200, status=TechLeadOutcomeStatus.COMPLETED, detail="done"
            ),
        )

        assert seen == [True]

    def test_agents_are_told_there_is_no_endpoint_rather_than_a_dead_one(
        self,
    ) -> None:
        """Ready is not enough: the resolved port must not be a lie.

        Nothing binds a Control API here, so resolving the CONFIGURED
        port would hand every spawned agent an address with nothing
        listening on it — the dead endpoint of #6913 / #6924. Declared
        unavailability wins over the configured fallback, so the agent
        environment omits the variable instead.
        """
        orchestrator = self._dispatch(
            cli_tech_lead.cmd_tech_lead,
            _args(issues=[23], flavor="planning_investigation"),
            on_dispatch=lambda _orch: [],
        )

        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.resolve_port(19080) is None
        assert endpoint.resolve_port(0) is None
