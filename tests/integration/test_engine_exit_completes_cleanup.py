"""A real process must finish its cleanup before it dies (#330).

The #328 lane engine retired with exit code 0, a free flock, an empty
inventory and no signal of any kind — and Control still refused it a
clean lifecycle PASS, because of what the target itself printed:

    process exit code: 0
    Lock released:     ABSENT
    Cleanup complete:  ABSENT
    Exiting with code 0   ... twice

"The lock is free" and "the shutdown manager released the lock" are not
the same fact. The first is also what you get when a process is killed
mid-cleanup, which is exactly what happened: a second ``exit()`` was
admitted as a fresh exit owner during the first one's cleanup and
reached ``os._exit`` before it finished.

So this proof is deliberately not in-process. ``os._exit`` cannot be
recorded here — it is taken, by a disposable child that runs the real
shutdown manager against a real repository lock with both of the
deployed termination pressures aimed at the same window. What the parent
reads back is what Control read: the exit code, the log tail, and
whether the lock file was *released* rather than merely abandoned.

Reading that log tail is a deliberate, named exception to the root
guide's "tests must not parse logs". There is no event bus across a
process boundary, the child is gone by the time the parent looks, and
Control's own #328 PASS criterion is that same tail — so here the log is
the artifact under test, not a substitute for one. The lock file, read
from the filesystem, is the corroborating non-log fact.

The child is ``disposable_engine_exit.py``; it owns nothing but a
throwaway ``tmp_path`` and its own lifetime.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import issue_orchestrator
from issue_orchestrator.control import shutdown_manager as shutdown_manager_module
from issue_orchestrator.infra.repo_identity import lock_file
from tests.integration.disposable_engine_exit import CODE_UNDER_TEST_PREFIX

CHILD_PROGRAM = Path(__file__).parent / "disposable_engine_exit.py"
CHILD_TIMEOUT_SECONDS = 60.0

# The child must run *this* checkout, not whatever the ambient
# ``PYTHONPATH`` resolves ``issue_orchestrator`` to — a trusted runtime
# is normally ahead of a candidate on an agent's path, and a proof that
# ran the runtime's shutdown manager would look exactly like a passing
# one. Pinned from the module the test process itself imported, and
# checked against the checkout below: agreement between parent and child
# is only worth something once the parent is known to have imported the
# code under review rather than an installed copy of it.
CHECKOUT_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
CANDIDATE_SRC_ROOT = Path(issue_orchestrator.__file__).resolve().parents[1]
CODE_UNDER_TEST = Path(shutdown_manager_module.__file__).resolve()

TERMINAL_EXIT_LINE = "Exiting with code 0"
CLEANUP_STARTED_LINE = "Running cleanup..."
RUNTIME_OWNERS_LINE = "[SHUTDOWN] Agent runtime owners terminated"
LOCK_RELEASED_LINE = "Lock released for"
CLEANUP_COMPLETE_LINE = "Cleanup complete"


@dataclass(frozen=True)
class EngineRetirement:
    """Everything the operator gets to see once the process is gone."""

    returncode: int
    log: str

    def count(self, line: str) -> int:
        return sum(1 for entry in self.log.splitlines() if line in entry)

    def index_of(self, line: str) -> int:
        entries = self.log.splitlines()
        for index, entry in enumerate(entries):
            if line in entry:
                return index
        raise AssertionError(f"{line!r} never appeared:\n{self.log}")

    def code_under_test(self) -> Path:
        """Which shutdown manager the child reported having loaded.

        Resolved, because the child reports its raw ``__file__`` and a
        checkout reached through a symlink (anything under macOS
        ``/tmp`` -> ``/private/tmp``, say) spells the same file two ways.
        The guard is about which file ran, not which spelling of it.
        """
        for entry in self.log.splitlines():
            if CODE_UNDER_TEST_PREFIX in entry:
                _, _, reported = entry.partition(CODE_UNDER_TEST_PREFIX)
                return Path(reported.strip()).resolve()
        raise AssertionError(
            f"the disposable engine never reported its shutdown manager:\n{self.log}"
        )


@pytest.fixture
def retirement(tmp_path: Path) -> EngineRetirement:
    """Run the disposable engine to completion and collect its receipt."""
    assert CANDIDATE_SRC_ROOT == CHECKOUT_SRC_ROOT, (
        "this test process imported issue_orchestrator from "
        f"{CANDIDATE_SRC_ROOT}, not from the checkout under review "
        f"({CHECKOUT_SRC_ROOT}); pinning the child to it would only prove "
        "that both halves ran the same wrong code"
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, str(CHILD_PROGRAM), str(repo_root)],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
        env={**os.environ, "PYTHONPATH": str(CANDIDATE_SRC_ROOT)},
    )
    retirement = EngineRetirement(
        returncode=completed.returncode, log=completed.stderr
    )
    assert retirement.code_under_test() == CODE_UNDER_TEST, (
        "the disposable engine did not run this checkout's shutdown manager:\n"
        f"{retirement.log}"
    )
    return retirement


@pytest.mark.integration
def test_one_owner_runs_the_whole_cleanup_before_the_process_dies(
    retirement: EngineRetirement,
) -> None:
    """The clean target sequence #328 could not produce.

    One announcement, one cleanup, and the cleanup's own tail present —
    which is only possible if nothing terminated the process while the
    exit owner still had work to do.
    """
    assert retirement.returncode == 0, retirement.log
    assert retirement.count(TERMINAL_EXIT_LINE) == 1, (
        "the process announced its terminal exit more than once — a second "
        f"caller was admitted as a fresh exit owner:\n{retirement.log}"
    )
    assert retirement.count(CLEANUP_STARTED_LINE) == 1, retirement.log
    assert retirement.count(LOCK_RELEASED_LINE) == 1, (
        f"the shutdown manager never released the lock:\n{retirement.log}"
    )
    assert retirement.count(CLEANUP_COMPLETE_LINE) == 1, (
        f"cleanup never ran to completion:\n{retirement.log}"
    )
    assert (
        retirement.index_of(TERMINAL_EXIT_LINE)
        < retirement.index_of(CLEANUP_STARTED_LINE)
        < retirement.index_of(RUNTIME_OWNERS_LINE)
        < retirement.index_of(LOCK_RELEASED_LINE)
        < retirement.index_of(CLEANUP_COMPLETE_LINE)
    ), f"the exit sequence ran out of order:\n{retirement.log}"


@pytest.mark.integration
def test_the_lock_is_released_rather_than_abandoned(
    retirement: EngineRetirement, tmp_path: Path
) -> None:
    """A free lock has to be the manager's doing, not the kernel's.

    #328 measured ``flock after exit: FREE`` from a process that never
    reached its own lock release. Here the release is in the log *and*
    the advertisement is gone, so the two facts agree.
    """
    assert retirement.count(LOCK_RELEASED_LINE) == 1, retirement.log
    assert lock_file(tmp_path / "repo").exists() is False
