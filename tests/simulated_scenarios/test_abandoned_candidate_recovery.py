"""One engine process reaches the next attempt after validation_failed (#195).

The central proof for the D7 recovery leaf, driven end-to-end through the real
tick loop rather than at a seam. Before the fix the shape below was identical
in all five natural reproductions (#146, #173, #178, #193, #194):

    validation_failed -> immediate cleanup -> worktree removed
      -> active=0 / no same-process reprocessing -> restart required

So the assertions are written to FAIL if that shape returns: a second attempt
must appear in the SAME orchestrator object, with no ``startup()`` in between,
and the issue must not be left sitting outside the queue with a stale
``in-progress`` label forever.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventName

from .conftest import build_config, build_orchestrator, run_until, run_until_event
from .scenario_dsl import script

ISSUE = 1
IN_PROGRESS = "in-progress"


def _issue(*labels: str) -> Issue:
    return Issue(
        number=ISSUE,
        title="Candidate whose publication gate refused it",
        labels=["simulated-scenario", "agent:coder", *labels],
    )


def _config(repo_root: Path, *, validation_cmd: str | None):
    return build_config(
        repo_root,
        coder_command=script("coder_dual_mode.sh"),
        reviewer_command=script("reviewer_ok.sh", prompt=True),
        validation_cmd=validation_cmd,
        max_validation_retries=0,
        review_exchange_mode="via-local-loop",
    )


def _sessions_started(events, issue_number: int = ISSUE) -> int:
    return sum(
        1
        for event in events.events
        if event.name == EventName.SESSION_STARTED
        and event.data.get("issue_number") == issue_number
    )


def _drive_to_validation_failure(scenario_repo: Path):
    """Run one engine until its candidate is refused by the validation gate."""
    config = _config(scenario_repo, validation_cmd=script("validate_fail.sh"))
    orch, repo_host, events, timeline = build_orchestrator(
        scenario_repo, [_issue()], config
    )
    run_until_event(orch, events, EventName.SESSION_VALIDATION_FAILED, max_ticks=6)
    return orch, repo_host, events, timeline, config


def test_the_same_engine_reaches_the_next_attempt_without_a_restart(
    scenario_repo: Path,
) -> None:
    """The leaf's central proof.

    One process, one orchestrator object, no ``startup()`` after the failure:
    a later tick must launch the next attempt. Under the pre-fix behaviour the
    candidate left ``cached_queue_issues`` on the tick it failed and nothing in
    this process could ever put it back, so ``_sessions_started`` stayed at 1
    for every remaining tick and this fails.
    """
    orch, repo_host, events, _timeline, _config_ = _drive_to_validation_failure(
        scenario_repo
    )

    assert _sessions_started(events) == 1, "expected exactly one attempt so far"

    run_until(orch, lambda: _sessions_started(events) >= 2, max_ticks=6)

    assert _sessions_started(events) >= 2
    assert [session.issue.number for session in orch.state.active_sessions] == [ISSUE]


def test_the_stranded_shape_does_not_survive_a_single_tick(
    scenario_repo: Path,
) -> None:
    """The chain's two links, checked where they used to hold forever.

    The stale ``in-progress`` label is shed, and the run gives its
    duplicate-launch claim back. Both were permanent before: the label because
    the detector was only ever handed ``cached_queue_issues``, the claim
    because ``session_history`` is per-process and only a restart dropped it.

    The failed session's RECORD is not part of that: it stays in history with
    its status and reason, because it is what the next attempt is judged
    against.
    """
    orch, repo_host, events, _timeline, _config_ = _drive_to_validation_failure(
        scenario_repo
    )

    run_until(
        orch,
        lambda: any(
            entry.issue_number == ISSUE and entry.claim_released
            for entry in orch.state.session_history
        ),
        max_ticks=4,
    )

    assert (ISSUE, IN_PROGRESS) in repo_host.remove_label_calls
    released = [
        entry for entry in orch.state.session_history if entry.issue_number == ISSUE
    ]
    assert released, "the failed session's record must survive the release"
    assert released[0].status == "validation_failed"
    assert released[0].claim_released is True


def test_restarting_after_the_fix_creates_no_extra_attempt(
    scenario_repo: Path,
) -> None:
    """Direction 5: restart stays idempotent, it is just no longer required.

    Restart used to be the ONLY way through this transition; now it is a no-op
    on top of it. The restarted engine must not find leftover stale state to
    recover, and must not open a second concurrent attempt on the issue.
    """
    orch, repo_host, events, _timeline, config = _drive_to_validation_failure(
        scenario_repo
    )
    run_until(orch, lambda: _sessions_started(events) >= 2, max_ticks=6)
    attempts_before_restart = _sessions_started(events)
    orch.request_shutdown()

    restarted, _repo_host, restarted_events, _timeline2 = build_orchestrator(
        scenario_repo, list(repo_host.issues), config, repo_host=repo_host
    )
    asyncio.run(restarted.startup())

    # Startup rehydrated the same one issue, and opened nothing of its own.
    assert _sessions_started(restarted_events) == 0
    assert attempts_before_restart >= 2
    assert [i.number for i in restarted.state.cached_queue_issues] == [ISSUE]
    assert len(restarted.state.active_sessions) <= 1


def test_a_blocked_completion_is_still_left_alone(scenario_repo: Path) -> None:
    """Direction 7: a non-``validation_failed`` completion path is unchanged.

    ``blocked`` plants a blocking label and keeps its own ownership story, so
    the release must never name it. The issue stays parked exactly as before —
    one attempt, and no relaunch however long the engine runs.
    """
    config = _config(scenario_repo, validation_cmd=None)
    config.agents["agent:coder"].command = script("coder_blocked.sh")
    orch, repo_host, events, _timeline = build_orchestrator(
        scenario_repo, [_issue()], config
    )

    run_until_event(orch, events, EventName.ISSUE_BLOCKED, max_ticks=6)
    for _ in range(4):
        orch.tick()

    assert _sessions_started(events) == 1
    assert any(
        label == "blocked" or label.startswith(("blocked-", "blocked:"))
        for label in repo_host.issues[0].labels
    ), repo_host.issues[0].labels
