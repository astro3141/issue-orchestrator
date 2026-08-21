"""Structured-output parsers for the sandbox boundary probes.

Both agent CLIs report what they actually did as a JSONL event stream —
``claude --output-format stream-json`` and ``codex exec --json`` — and the
boundary probes assert on those events rather than on prose, because "the tool
was invoked and denied" and "the model declined to invoke the tool" are
indistinguishable from the transcript but decide opposite verdicts.

The parsing is **deterministic**: given a stream it yields the same events,
with no provider, no network and no model choice involved. That is why it
lives here rather than inside
``tests/integration/test_sandbox_os_boundary.py``. That module is marked
``live_agent`` and is therefore owned by the live-assurance lane (#194); a
deterministic parser test sitting inside it would have left blocking candidate
validation along with the module, which is exactly the weakening #194's
guardrails forbid. Split out, the parser keeps its unit coverage in
``tests/unit/test_sandbox_stream_events.py`` — in the blocking lane, where a
regression in it fails a candidate — while the live probes that consume it
move to assurance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# Substrings that mark a tool_result as a *permission* denial (as opposed to an
# arbitrary tool failure). Both known phrasings are covered: the allow-absence
# dontAsk denial ("Permission to use <Tool> has been denied … don't ask mode")
# and the explicit deny-path denial ("… denied by your permission settings").
PERMISSION_DENIAL_SIGNS = (
    "permission to use",
    "has been denied",
    "denied by your permission",
    "permission settings",
    "don't ask mode",
)


class ToolEvent(NamedTuple):
    tool: str
    file_path: str | None
    is_error: bool
    result_text: str


class CodexCommandEvent(NamedTuple):
    command: str
    output: str
    exit_code: int | None
    status: str


def codex_command_events(stdout: str) -> list[CodexCommandEvent]:
    """Extract completed command executions from Codex's JSONL event stream."""
    events: list[CodexCommandEvent] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        exit_code = item.get("exit_code")
        events.append(
            CodexCommandEvent(
                command=str(item.get("command", "")),
                output=str(item.get("aggregated_output", "")),
                exit_code=exit_code if isinstance(exit_code, int) else None,
                status=str(item.get("status", "")),
            )
        )
    return events


def _result_text(content: object) -> str:
    """Flatten a ``tool_result`` ``content`` (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return "" if content is None else str(content)


def tool_events(stdout: str) -> list[ToolEvent]:
    """Parse ``--output-format stream-json`` JSONL into structured tool events.

    Pairs each ``tool_use`` with its ``tool_result``, preserving both the
    ``is_error`` flag and the result *text* so callers can assert that a failure
    is specifically a permission denial rather than an arbitrary tool error.
    """
    uses: dict[str, tuple[str, str | None]] = {}
    results: dict[str, tuple[bool, str]] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "system" and evt.get("subtype") == "permission_denied":
            denied_tool_use_id = evt.get("tool_use_id")
            if isinstance(denied_tool_use_id, str):
                results[denied_tool_use_id] = (True, _result_text(evt.get("message")))
            continue
        # The CLI's stream carries non-assistant events too, and some of them
        # (errors, system notices) put a plain string in ``message``. Skip them
        # the way codex_command_events skips non-dict ``item`` payloads —
        # a missing tool event still fails the assertions below, but with the
        # assertion's message instead of an AttributeError in the parser.
        message = evt.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uid = block.get("id")
                if not isinstance(uid, str):
                    continue
                inp = block.get("input") or {}
                fp = inp.get("file_path") or inp.get("path")
                uses[uid] = (
                    str(block.get("name")),
                    fp if isinstance(fp, str) else None,
                )
            elif block.get("type") == "tool_result":
                rid = block.get("tool_use_id")
                if not isinstance(rid, str):
                    continue
                results[rid] = (
                    bool(block.get("is_error")),
                    _result_text(block.get("content")),
                )
    events: list[ToolEvent] = []
    for uid, (name, fp) in uses.items():
        is_error, text = results.get(uid, (False, ""))
        events.append(ToolEvent(name, fp, is_error, text))
    return events


def native_writes_to(events: list[ToolEvent], target: Path) -> list[ToolEvent]:
    """The write-tool events in ``events`` that name ``target`` exactly."""
    tp = str(target)
    return [
        e
        for e in events
        if e.tool in WRITE_TOOLS and e.file_path and str(Path(e.file_path)) == tp
    ]


def is_permission_denial(text: str) -> bool:
    """Whether ``text`` reads as a permission denial rather than a tool error."""
    low = text.lower()
    return any(sign in low for sign in PERMISSION_DENIAL_SIGNS)


__all__ = [
    "PERMISSION_DENIAL_SIGNS",
    "WRITE_TOOLS",
    "CodexCommandEvent",
    "ToolEvent",
    "codex_command_events",
    "is_permission_denial",
    "native_writes_to",
    "tool_events",
]
