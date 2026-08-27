"""A disposable process that shuts down the way the engine shuts down (#330).

Run as a script, never imported by a test. It is the target half of
``test_engine_exit_completes_cleanup.py``: the real
:mod:`issue_orchestrator.control.shutdown_manager` singleton, a real
repository lock, and the two termination pressures the deployed engine
puts on itself — the ``/api/shutdown`` timer
(``entrypoints.web_operator_routes._tear_down_dashboard_server``) and the
web entrypoint's own post-server exit (``entrypoints.web``) — arriving at
``exit()`` while cleanup is in flight.

Nothing here is a stand-in for the code under test. What is simulated is
only the *engine work* cleanup does: a callback that takes a moment, the
way ``Orchestrator.close`` terminating agent runtime owners does.

Usage: ``python disposable_engine_exit.py <repo_root>``. Exits through
``shutdown_manager``, so the exit code, the log tail and the state of the
lock file are the observations the parent reads.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from issue_orchestrator.control import shutdown_manager as shutdown_manager_module
from issue_orchestrator.control.shutdown_manager import shutdown_manager
from issue_orchestrator.infra.repo_lock import acquire_lock

CODE_UNDER_TEST_PREFIX = "[proof] shutdown manager: "

# Long enough that a second caller admitted as a fresh exit owner
# terminates the process inside it; short enough to keep the child's
# whole life under a second.
RESIDUAL_CLEANUP_SECONDS = 0.2
# Deadlock guard only. The cleanup callback is released by the second
# exit being issued, not by the passage of time.
SECOND_EXIT_TIMEOUT_SECONDS = 30.0
# The deployed value from ``web_operator_routes._PROCESS_EXIT_DELAY_SECONDS``.
API_SHUTDOWN_EXIT_DELAY_SECONDS = 0.2

cleanup_started = threading.Event()
second_exit_issued = threading.Event()


def terminate_runtime_owners() -> None:
    """Stand in for ``Orchestrator.close``: cleanup that takes a moment."""
    logging.info("[SHUTDOWN] Terminating agent runtime owners")
    cleanup_started.set()
    second_exit_issued.wait(SECOND_EXIT_TIMEOUT_SECONDS)
    # Residual work, still owed, with the second exit already in flight.
    time.sleep(RESIDUAL_CLEANUP_SECONDS)
    logging.info("[SHUTDOWN] Agent runtime owners terminated")


def main(repo_root: Path) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stderr
    )
    # Which shutdown manager this process actually loaded. The parent
    # asserts on it: a proof that ran some other checkout's code would
    # otherwise look exactly like a passing one.
    logging.info("%s%s", CODE_UNDER_TEST_PREFIX, shutdown_manager_module.__file__)
    acquire_lock(repo_root)
    shutdown_manager.initialize(repo_root)
    shutdown_manager.add_cleanup_callback(terminate_runtime_owners)

    # Pressure one: POST /api/shutdown, whose deferred teardown schedules
    # the process exit after acknowledging on the wire (#277).
    shutdown_manager.request_shutdown(reason="API /api/shutdown")
    timer = threading.Timer(API_SHUTDOWN_EXIT_DELAY_SECONDS, shutdown_manager.exit)
    timer.daemon = False
    timer.start()

    # Pressure two: the dashboard server stops, and the web entrypoint's
    # own finally-block exits the process. In deployment this lands
    # whenever the server finishes coming down; here it is aimed exactly
    # at the window that failed in #328 — mid-cleanup.
    cleanup_started.wait(SECOND_EXIT_TIMEOUT_SECONDS)
    logging.info("[web] Shutdown complete, exiting via shutdown_manager")
    shutdown_manager.request_shutdown(reason="web server stopped")
    second_exit_issued.set()
    shutdown_manager.exit()

    # Unreachable: shutdown_manager.exit() terminates the process.
    raise AssertionError("shutdown_manager.exit() returned")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
