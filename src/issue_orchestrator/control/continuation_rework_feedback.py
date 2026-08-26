"""The correction context a continuation's rework carries with it (#297).

Split from :mod:`.continuation_rework_handoff`, which decides *whether* a
candidate may take a rework cycle. This module decides what the agent that takes
it is told, and the two are separate concerns joined at one call: the handoff
resolves the durable records, this composes the prompt out of them and knows
nothing about budgets, PR reads or refusals.

Everything here is COPIED from a durable record, never re-derived. That is the
whole rule of the module, and it is why it can be a pure function over values
the caller already holds: the candidate SHA is the attempt's own key, the
publish command and verdict come from the receipt the gate filed for that exact
commit, and the failing output comes from #94's durable bundle for that same
commit and suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .gate_failure_diagnostics import (
    DIAGNOSTIC_FILE_NAME,
    FAILURE_LOG_TAIL_BYTES,
    STDERR_FILE_NAME,
    STDOUT_FILE_NAME,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.attempt import Attempt
    from ..ports.pull_request_tracker import PRInfo
    from .gate_failure_diagnostics import DurableGateFailure


def build_continuation_rework_feedback(
    *,
    pr: "PRInfo",
    attempt: "Attempt",
    phase_reason: str,
    failure: "DurableGateFailure | None",
) -> str:
    """The correction context that travels with an admitted rework.

    Everything here is copied from a durable record, never re-derived: the
    candidate SHA is the attempt's own key, the publish command and verdict come
    from the receipt the gate filed for that exact commit, the failing output
    and its location come from #94's durable bundle for that same commit and
    suite, and the intent is the descriptor copied from the agent's completion
    record. The reviewer's own comments on the PR are NOT repeated here — the
    rework launcher already fetches and appends them for every cycle, and a
    second copy would drift from the first.

    ``failure`` is ``None`` only for an exit whose publication was never
    refused — a candidate handed back after a reviewer asked for changes on a
    commit that passed. A publication failure with no resolvable output never
    reaches here at all: its handoff is refused upstream, because "publication
    failed, go and find out why" is a prompt that needs a human to answer it.

    A missing part is named as missing rather than omitted. An agent told
    "publish validation failed" with no command would go looking for one; an
    agent told no verdict was recorded knows not to.
    """
    receipt = attempt.latest_publication_evaluation
    lines = [
        "The control continuation for this candidate has ended without "
        f"publishing it (phase: {phase_reason}). The work is yours to correct "
        "on this same pull request; nothing has been pushed or merged on your "
        "behalf.",
        "",
        f"- PR: #{pr.number} {pr.url}".rstrip(),
        f"- Branch: {pr.branch}",
        f"- Failed candidate commit: {attempt.key.head_sha}",
    ]
    if receipt is not None:
        lines.extend(
            [
                f"- Publication gate command: {receipt.command}",
                f"- Publication gate verdict: {receipt.verdict.value} "
                f"(suite {receipt.suite}, profile {receipt.profile})",
            ]
        )
    else:
        lines.append(
            "- Publication gate: no verdict was recorded for this commit."
        )
    if failure is not None:
        lines.extend(_durable_failure_lines(failure))
    descriptor = attempt.continuation_descriptor
    if descriptor is not None:
        lines.extend(
            [
                "",
                "What the previous agent recorded for this candidate:",
                "",
                f"Implementation: {descriptor.implementation}",
                f"Problems: {descriptor.problems}",
            ]
        )
    lines.extend(
        [
            "",
            "Fix the cause of the publication failure on this branch, then "
            "complete through the ordinary rework contract. Do not treat the "
            "failed commit above as validated.",
        ]
    )
    return "\n".join(lines)


def _durable_failure_lines(failure: "DurableGateFailure") -> list[str]:
    """The failing run's own output, plus where the whole of it still lives.

    Both, deliberately. The excerpt is what makes the prompt actionable without
    a second lookup; the directory is what makes it checkable and gets an agent
    to the rest of a log the excerpt is a tail of. The path is in the PRIMARY
    checkout, not in any worktree, so it resolves from wherever the rework runs.
    """
    lines = [
        "",
        "The publication gate's own output for that commit was kept before the "
        "candidate's worktree was removed, and is readable now at:",
        "",
        f"    {failure.directory}",
        "",
        f"({DIAGNOSTIC_FILE_NAME}, {STDOUT_FILE_NAME}, {STDERR_FILE_NAME}. "
        f"Exit code: {failure.exit_code}"
        f"{'; the run timed out' if failure.timed_out else ''}.)",
    ]
    for name, log in (("stdout", failure.stdout), ("stderr", failure.stderr)):
        if not log.has_output:
            continue
        heading = f"Publication gate {name}"
        if log.truncated:
            heading += (
                f" (last {FAILURE_LOG_TAIL_BYTES} bytes of {log.path.name}; "
                "read the file above for the rest)"
            )
        lines.extend(["", f"{heading}:", "", "```", log.tail.rstrip("\n"), "```"])
    return lines


__all__ = ["build_continuation_rework_feedback"]
