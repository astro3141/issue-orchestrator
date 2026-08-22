"""Deterministic coverage of the sandbox probes' structured-output parsers.

This is the half of the sandbox proof that does **not** depend on a model
choosing anything, and #194's guardrails require it to stay in blocking
candidate validation while the live probes move to the assurance lane. It used
to live inside ``tests/integration/test_sandbox_os_boundary.py``; that module
is marked ``live_agent``, and a module-level marker takes every test in the
file with it, so leaving these here-and-there would have quietly removed them
from the blocking gate.

What they defend is the difference between "the tool was invoked and denied"
and "the model never invoked the tool". Every deny assertion in the live probe
is an ``all(...)`` over parsed events, and ``all([])`` is ``True`` — so a
parser that silently stops recognising a tool_use turns a security assertion
into a vacuous pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.sandbox_stream_events import (
    CodexCommandEvent,
    ToolEvent,
    codex_command_events,
    is_permission_denial,
    native_writes_to,
    tool_events,
)


def test_tool_events_supports_system_permission_denial_message() -> None:
    """Claude 2.1.224 emits permission denials with a string message."""
    tool_use_id = "toolu_denied_write"
    target = "/outside/native_escape.txt"
    denial = "Permission to use Write has been denied in don't ask mode."
    stream = "\n".join(
        [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Write",
                            "input": {"file_path": target, "content": "blocked"},
                        }
                    ]
                },
            }),
            json.dumps({
                "type": "system",
                "subtype": "status",
                "message": "non-tool status text",
            }),
            json.dumps({
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Write",
                "tool_use_id": tool_use_id,
                "message": denial,
            }),
        ]
    )

    assert tool_events(stream) == [ToolEvent("Write", target, True, denial)]


def test_tool_events_pairs_a_tool_result_with_its_use() -> None:
    """The inline ``tool_result`` phrasing still carries is_error and text."""
    stream = "\n".join(
        [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_ok",
                            "name": "Write",
                            "input": {"file_path": "/wt/ok.txt", "content": "x"},
                        }
                    ]
                },
            }),
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_ok",
                            "is_error": False,
                            "content": [{"type": "text", "text": "wrote"}],
                        }
                    ]
                },
            }),
        ]
    )

    assert tool_events(stream) == [ToolEvent("Write", "/wt/ok.txt", False, "wrote")]


def test_tool_events_ignores_unparsable_and_non_dict_lines() -> None:
    """A noisy stream must not crash the parser mid-probe."""
    stream = "\n".join(["not json", json.dumps(["a", "list"]), "", "   "])

    assert tool_events(stream) == []


def test_native_writes_to_selects_only_write_tools_naming_the_target() -> None:
    """A Read of the target, or a Write elsewhere, is not a write to it."""
    target = Path("/wt/policy.json")
    events = [
        ToolEvent("Write", str(target), True, "denied"),
        ToolEvent("Read", str(target), False, "contents"),
        ToolEvent("Write", "/wt/other.txt", False, "ok"),
        ToolEvent("Write", None, False, "no path"),
    ]

    assert native_writes_to(events, target) == [
        ToolEvent("Write", str(target), True, "denied")
    ]


def test_is_permission_denial_distinguishes_denial_from_arbitrary_error() -> None:
    """A tool failure that is not a permission denial must not read as one."""
    assert is_permission_denial("Permission to use Write has been denied")
    assert is_permission_denial("This was denied by your permission settings")
    assert not is_permission_denial("ENOSPC: no space left on device")


def test_codex_command_events_extracts_completed_command_executions() -> None:
    """Only ``item.completed`` command executions count as proof a command ran."""
    stream = "\n".join(
        [
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/sh -c probe",
                    "aggregated_output": "INSIDE_OK",
                    "exit_code": 0,
                    "status": "completed",
                },
            }),
            json.dumps({
                "type": "item.started",
                "item": {"type": "command_execution", "command": "ignored"},
            }),
            json.dumps({"type": "item.completed", "item": {"type": "message"}}),
            json.dumps({"type": "item.completed", "item": "not-a-dict"}),
            "unparsable",
        ]
    )

    assert codex_command_events(stream) == [
        CodexCommandEvent("/bin/sh -c probe", "INSIDE_OK", 0, "completed")
    ]


def test_codex_command_events_keeps_a_non_int_exit_code_out_of_the_record() -> None:
    """A missing or malformed exit code is ``None``, never coerced to success."""
    stream = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "probe",
            "aggregated_output": "",
            "exit_code": "0",
            "status": "completed",
        },
    })

    assert codex_command_events(stream)[0].exit_code is None
