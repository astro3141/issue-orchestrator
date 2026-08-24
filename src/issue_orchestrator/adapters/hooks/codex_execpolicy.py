"""How a ``codex execpolicy check`` result is classified (#252).

Codex answers two different questions with two different shapes, and the
difference is load-bearing::

    {"matchedRules": [{...}], "decision": "forbidden"}   a rule matched, and denied
    {"matchedRules": []}                                 no rule matched at all

The hook verifier used to read "no decision I recognize" as "not allowed", so
the safe negative sample ``git push origin main`` -- which the shipped rules
deliberately list as ``not_match`` for the forbidden ``git push --no-verify``
rule -- came back as ``execpolicy_wrongly_blocks`` and every Codex launch
failed its hook gate before an agent session existed.

The distinction drawn here is ``NO_MATCH != FORBIDDEN``. It is emphatically
*not* "anything other than forbidden is allowed": ``prompt``, an unrecognized
decision, a decision without a matched rule, a shape that is not the documented
no-match shape, unparseable output, and a nonzero CLI exit all raise
:class:`ExecPolicyResultError` so the caller fails closed.

Only ``decision`` is read. An execpolicy result that reports its verdict under
some other key is unrecognized output, and unrecognized output fails closed
rather than being guessed at -- the guess is what this module exists to remove.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

MATCHED_RULES_KEY = "matchedRules"
DECISION_KEY = "decision"

_FORBIDDEN_DECISION = "forbidden"
_ALLOW_DECISIONS = frozenset({"allow", "allowed"})


class ExecPolicyOutcome(Enum):
    """The three results a policy check can legitimately reach."""

    FORBIDDEN = "forbidden"
    """A matched rule denies the command."""

    ALLOWED = "allowed"
    """A matched rule permits the command."""

    NO_MATCH = "no_match"
    """No rule matched the command. Not a verdict, and not a block."""


class ExecPolicyResultError(RuntimeError):
    """An execpolicy result that cannot be classified.

    Raised rather than resolved to an outcome so callers fail closed: an
    unreadable answer is not evidence that a command is permitted, nor that it
    is denied.
    """


def classify_execpolicy_result(stdout: str) -> ExecPolicyOutcome:
    """Classify one ``codex execpolicy check`` stdout payload.

    Raises:
        ExecPolicyResultError: the payload is not one of the documented,
            recognizable shapes.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ExecPolicyResultError(f"unparseable execpolicy JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ExecPolicyResultError(
            f"execpolicy result is not a JSON object: {type(data).__name__}"
        )

    matched_rules = data.get(MATCHED_RULES_KEY)
    if not isinstance(matched_rules, list):
        raise ExecPolicyResultError(
            f"execpolicy result has no {MATCHED_RULES_KEY!r} list"
        )

    decision = data.get(DECISION_KEY)
    if decision is None:
        if matched_rules:
            raise ExecPolicyResultError(
                "execpolicy reported matched rules without a decision"
            )
        return ExecPolicyOutcome.NO_MATCH

    if not isinstance(decision, str):
        raise ExecPolicyResultError(
            f"execpolicy decision is not a string: {decision!r}"
        )
    if not matched_rules:
        raise ExecPolicyResultError(
            f"execpolicy reported decision {decision!r} with no matched rule"
        )

    normalized = decision.strip().lower()
    if normalized == _FORBIDDEN_DECISION:
        return ExecPolicyOutcome.FORBIDDEN
    if normalized in _ALLOW_DECISIONS:
        return ExecPolicyOutcome.ALLOWED
    raise ExecPolicyResultError(f"unrecognized execpolicy decision: {decision!r}")


class ExecPolicyChecker(Protocol):
    """Answers how a policy classifies one command."""

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        """Classify *command* against *rules_file*.

        Raises:
            ExecPolicyResultError: the policy gave no classifiable answer.
        """
        ...


@dataclass(frozen=True)
class CodexCliExecPolicy:
    """Asks the installed Codex CLI, which is the only authority on its rules."""

    timeout_seconds: int = 120

    def check(self, rules_file: Path, command: Sequence[str]) -> ExecPolicyOutcome:
        """Classify *command* by running ``codex execpolicy check``.

        Every way this adapter can fail to produce a classifiable answer leaves
        through one door, so the caller has a single failure channel to catch:
        a CLI that is absent or not executable, a run that outlives the
        timeout, a nonzero exit, and output that cannot be classified all raise
        :class:`ExecPolicyResultError`.

        Raises:
            ExecPolicyResultError: the policy gave no classifiable answer.
        """
        argv = [
            "codex",
            "execpolicy",
            "check",
            "--rules",
            str(rules_file),
            "--pretty",
            "--",
            *command,
        ]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecPolicyResultError(
                f"codex execpolicy check timed out after {self.timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise ExecPolicyResultError(
                f"could not run codex execpolicy check: {exc}"
            ) from exc
        if result.returncode != 0:
            raise ExecPolicyResultError(
                result.stderr.strip()
                or f"codex execpolicy check exited {result.returncode}"
            )
        return classify_execpolicy_result(result.stdout)


__all__ = [
    "CodexCliExecPolicy",
    "DECISION_KEY",
    "ExecPolicyChecker",
    "ExecPolicyOutcome",
    "ExecPolicyResultError",
    "MATCHED_RULES_KEY",
    "classify_execpolicy_result",
]
