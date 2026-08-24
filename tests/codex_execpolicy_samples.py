"""Recorded ``codex execpolicy check`` payloads, shared by both proofs (#252).

The unit proof classifies these payloads without an installed Codex; the live
proof in ``tests/integration/test_codex_execpolicy_live.py`` asks the installed
CLI the same questions and asserts it still answers in these shapes. Keeping
one copy is what makes the second test capable of invalidating the first: a
provider that changed its result contract fails the live proof instead of
leaving the unit proof quietly describing a Codex that no longer exists.

Measured against codex-cli 0.147.0 with
``src/issue_orchestrator/templates/hooks/codex/orchestrator.rules``.
"""

from __future__ import annotations

import json

DANGEROUS_COMMAND = ("git", "push", "--no-verify")
"""The push the shipped rules exist to forbid."""

SAFE_COMMAND = ("git", "push", "origin", "main")
"""The push the shipped rules list as ``not_match`` — and which Codex therefore
answers with the no-match shape, not with a decision."""

NO_MATCH_PAYLOAD = json.dumps({"matchedRules": []})
"""No rule matched. The absence of ``decision`` is the whole point: reading it
as a block is the defect this shape's handling exists to prevent."""

FORBIDDEN_PAYLOAD = json.dumps(
    {
        "matchedRules": [
            {
                "prefixRuleMatch": {
                    "matchedPrefix": list(DANGEROUS_COMMAND),
                    "decision": "forbidden",
                    "justification": "Pre-push hooks must run.",
                }
            }
        ],
        "decision": "forbidden",
    }
)

ALLOW_PAYLOAD = json.dumps(
    {
        "matchedRules": [
            {
                "prefixRuleMatch": {
                    "matchedPrefix": ["git", "push", "origin"],
                    "decision": "allow",
                    "justification": "explicitly permitted",
                }
            }
        ],
        "decision": "allow",
    }
)

PROMPT_PAYLOAD = json.dumps(
    {
        "matchedRules": [
            {
                "prefixRuleMatch": {
                    "matchedPrefix": ["git", "push"],
                    "decision": "prompt",
                    "justification": "ask first",
                }
            }
        ],
        "decision": "prompt",
    }
)
"""A decision Codex can emit and this repository refuses to accept: the CLI has
flags that skip questions, so "ask the human" would silently become "proceed"."""

PROMPT_RULES = """\
prefix_rule(
    pattern = ["git", "push"],
    decision = "prompt",
    justification = "ask first",
)
"""
"""A rules file that provokes :data:`PROMPT_PAYLOAD` from the real CLI."""


__all__ = [
    "ALLOW_PAYLOAD",
    "DANGEROUS_COMMAND",
    "FORBIDDEN_PAYLOAD",
    "NO_MATCH_PAYLOAD",
    "PROMPT_PAYLOAD",
    "PROMPT_RULES",
    "SAFE_COMMAND",
]
