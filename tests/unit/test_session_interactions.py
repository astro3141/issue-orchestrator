from __future__ import annotations

import pytest
from unittest.mock import Mock

from issue_orchestrator.execution.session_interactions import (
    SessionInteractionHandler,
    SessionInteractionRule,
    builtin_session_interaction_rules,
)


def test_session_interaction_handler_matches_split_chunks_once() -> None:
    sender = Mock(return_value=True)
    handler = SessionInteractionHandler(
        session_name="issue-1",
        rules=[
            SessionInteractionRule(
                name="trust",
                required_substrings=(
                    "Quick safety check: Is this a project you created or one you trust?",
                    "Yes, I trust this folder",
                    "No, exit",
                ),
                response="",
            )
        ],
    )
    handler.bind_sender(sender)

    handler.on_output(b"Quick safety check: Is this a project you created ")
    handler.on_output(b"or one you trust?\r\n")
    handler.on_output(b"\xe2\x9d\xaf 1. Yes, I trust this folder\r\n  2. No, exit\r\n")
    handler.on_output(b"Enter to confirm\r\n")
    handler.on_output(b"Quick safety check: Is this a project you created or one you trust?\r\n")

    sender.assert_called_once_with("")


def test_session_interaction_handler_ignores_ansi_noise() -> None:
    sender = Mock(return_value=True)
    handler = SessionInteractionHandler(
        session_name="issue-2",
        rules=[
            SessionInteractionRule(
                name="trust",
                required_substrings=("Yes, I trust this folder", "No, exit"),
                response="",
            )
        ],
    )
    handler.bind_sender(sender)

    handler.on_output("\x1b[32mYes, I trust this folder\x1b[0m\r\n")
    handler.on_output("\x1b[1mNo, exit\x1b[0m\r\n")

    sender.assert_called_once_with("")


def test_builtin_session_interaction_rules_are_scoped_to_claude() -> None:
    assert builtin_session_interaction_rules("claude --model sonnet 'fix it'")
    assert builtin_session_interaction_rules("FOO=1 BAR=2 && claude --model sonnet 'fix it'")
    assert builtin_session_interaction_rules("exec CLAUDE --model sonnet 'fix it'") == ()
    assert builtin_session_interaction_rules("FOO=1 claude --model sonnet 'fix it'")
    assert builtin_session_interaction_rules("cat prompt.md | claude --print") == ()
    assert builtin_session_interaction_rules("python -m provider_runner --command 'claude foo'") == ()


def test_no_builtin_rule_can_answer_the_codex_trust_dialog() -> None:
    """The TTY responder must never grant Codex workspace trust (#215).

    Trust is a repository-root authority decision settled before spawn: the
    launch argv carries the human-approved grant, verified against the resolved
    common repository root. A keystroke responder would answer "Yes" for
    whatever directory is on screen — and Codex keys the resulting grant to the
    *repository root*, not the disposable worktree — so a returning dialog must
    fail visibly instead of being quietly answered. That holds for the exact
    managed shape the pilot launched with, grant included.
    """
    managed_launch = (
        "codex --ask-for-approval never --model gpt-5-codex "
        '-c check_for_update_on_startup=false '
        '-c projects={ "/Users/o/repo" = { trust_level = "trusted" } } '
        '--sandbox workspace-write "review this"'
    )
    for command in (
        managed_launch,
        "codex --ask-for-approval never --model gpt-5-codex "
        '--sandbox workspace-write "review this"',
        "FOO=1 BAR=2 && codex -m gpt-5-codex -c model_reasoning_effort='xhigh' "
        "'review this'",
        "codex exec --full-auto",
        "codex --model gpt-5-codex exec",
    ):
        assert builtin_session_interaction_rules(command) == (), command


def test_session_interaction_rules_only_support_one_shot_rules() -> None:
    with pytest.raises(ValueError, match="fire_once=True"):
        SessionInteractionRule(
            name="repeat",
            required_substrings=("prompt",),
            response="y",
            fire_once=False,
        )
