"""Unit tests for ``CodexProvider.build_command``.

Sandbox-clean: no subprocess, no network. Asserts on the assembled
argv only.
"""

from __future__ import annotations

import tomllib

import pytest

from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider


def _cmd(**kwargs: str) -> list[str]:
    """Build the argv with sane defaults; return the list for assertions."""
    return CodexProvider().build_command(prompt="task", **kwargs)


def _config_overrides(argv: list[str]) -> dict[str, object]:
    """Decode every ``-c key=<toml>`` pair in *argv* the way Codex would."""
    overrides: dict[str, object] = {}
    for index, token in enumerate(argv):
        if token != "-c":
            continue
        key, raw = argv[index + 1].split("=", 1)
        overrides[key] = tomllib.loads(f"value = {raw}")["value"]
    return overrides


class TestCodexJsonOutputDefault:
    """The default for ``json_output`` is **off** so the PTY recording
    captures codex's terminal UI (what a human sees at the terminal),
    not its structured JSONL event stream.

    Regression guard for tixmeup #362's reviewer-timeline content: with
    ``--json`` set, the recording is a stream of
    ``{"type":"thread.started"}`` / ``{"type":"item.completed"}`` /
    etc., which the timeline viewer's terminal renderer concatenates
    as raw, unstyled text. With ``--json`` unset, codex emits its
    normal terminal UI (formatted, ANSI-coloured) and the renderer
    plays it back faithfully.

    Nothing in this codebase parses codex stdout for protocol data —
    persistent-session review-exchange uses a response file, one-shot
    runs use ``coding-done`` callbacks. So the JSON event stream
    has no production consumer; defaulting it on was pure waste plus
    a viewer-rendering bug. Keep this test tight: any path that flips
    the default back without an explicit caller opt-in re-introduces
    the empty-looking timeline symptom.
    """

    def test_default_does_not_pass_json_flag(self) -> None:
        cmd = _cmd()
        assert "--json" not in cmd, (
            f"codex default invocation must NOT pass --json (recording "
            f"would become unstyled JSONL); got argv={cmd}"
        )

    def test_explicit_false_does_not_pass_json_flag(self) -> None:
        cmd = _cmd(json_output="false")
        assert "--json" not in cmd

    def test_explicit_true_does_pass_json_flag(self) -> None:
        """The opt-in path is still wired — automation that genuinely
        wants codex's JSONL events can request them via
        ``provider_args: {execution_mode: "exec", json_output: "true"}``
        per agent."""
        cmd = _cmd(execution_mode="exec", json_output="true")
        assert "--json" in cmd

    def test_json_output_requires_exec_mode(self) -> None:
        with pytest.raises(ValueError, match="json_output requires"):
            _cmd(json_output="true")

    @pytest.mark.parametrize("yes_value", ["TRUE", "True", "tRuE"])
    def test_truthy_string_case_insensitive_passes_json_flag(
        self, yes_value: str,
    ) -> None:
        """Match historical behavior: ``json_output`` is parsed
        case-insensitively. Locks the contract so a downstream caller
        passing ``"True"`` keeps working."""
        cmd = _cmd(execution_mode="exec", json_output=yes_value)
        assert "--json" in cmd

    @pytest.mark.parametrize("no_value", ["", "0", "no", "off", "False"])
    def test_falsey_strings_do_not_pass_json_flag(
        self, no_value: str,
    ) -> None:
        """Any non-``true`` (case-insensitive) string is treated as
        opt-out. This matches the original parser shape and keeps the
        default safe even when someone passes a typo."""
        cmd = _cmd(json_output=no_value)
        assert "--json" not in cmd


class TestCodexUpdateCheckSuppression:
    """Every Codex launch disables the startup update check (#205).

    In a fully trusted repository the interactive TUI still stops before
    the composer on::

        ✨ Update available!  0.147.0 -> 0.149.0
          › 1. Update now  2. Skip  3. Skip until next version
        Press enter to continue

    Nothing in an unattended launch answers that prompt, and the trigger is
    upstream releasing a newer version — not anything IO does — so pinning a
    runtime guarantees the prompt recurs rather than suppressing it. The
    launch argv carries Codex's documented ``check_for_update_on_startup``
    field as a ``-c`` override, which is the highest-precedence config layer
    and therefore holds whatever the user, project, or system layer says.
    """

    def test_interactive_launch_disables_update_check(self) -> None:
        assert _config_overrides(_cmd())["check_for_update_on_startup"] is False

    def test_exec_launch_disables_update_check(self) -> None:
        cmd = _cmd(execution_mode="exec")
        assert _config_overrides(cmd)["check_for_update_on_startup"] is False

    def test_override_is_emitted_as_an_adjacent_c_pair(self) -> None:
        """``-c`` and its assignment must stay adjacent, and the value must be
        a TOML boolean — ``check_for_update_on_startup="false"`` would be a
        string, which is not what the field accepts."""
        cmd = _cmd()
        assert "check_for_update_on_startup=false" in cmd
        assert cmd[cmd.index("check_for_update_on_startup=false") - 1] == "-c"

    def test_override_is_emitted_exactly_once(self) -> None:
        cmd = _cmd(reasoning_effort="high")
        assert cmd.count("check_for_update_on_startup=false") == 1

    def test_override_survives_alongside_reasoning_effort(self) -> None:
        """Both overrides ride the same ``-c`` seam; neither may displace the
        other."""
        overrides = _config_overrides(_cmd(reasoning_effort="high"))
        assert overrides["check_for_update_on_startup"] is False
        assert overrides["model_reasoning_effort"] == "high"

    def test_override_precedes_the_prompt(self) -> None:
        cmd = _cmd()
        assert cmd.index("check_for_update_on_startup=false") < cmd.index("task")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"execution_mode": "exec"},
            {"approval_mode": "yolo"},
            {"execution_mode": "exec", "approval_mode": "yolo"},
            {"execution_mode": "exec", "json_output": "true"},
        ],
    )
    def test_every_supported_launch_shape_disables_update_check(
        self, kwargs: dict[str, str],
    ) -> None:
        """The prompt is a startup-time gate, so no approval mode, execution
        mode, or output mode may leave it armed."""
        overrides = _config_overrides(_cmd(**kwargs))
        assert overrides["check_for_update_on_startup"] is False


class TestCodexConfigOverridePosition:
    """``-c`` pairs are root-command options and must precede ``exec``.

    Position is not cosmetic. A ``-c`` pair placed after the subcommand binds
    to the subcommand's own occurrence of the option, and the root-level
    overrides — including the permission profile a ``SandboxScope`` emits — are
    then not applied. The live boundary test caught this: a scoped
    ``codex exec`` launch still ran the requested command, but its write into
    the worktree was denied, because one post-``exec`` override had displaced
    the profile (#205). Guard the rule here too, deterministically, so a new
    override cannot re-introduce it without a live run.
    """

    @staticmethod
    def _config_flag_positions(argv: list[str]) -> list[int]:
        return [index for index, token in enumerate(argv) if token == "-c"]

    def test_update_override_precedes_the_exec_subcommand(self) -> None:
        cmd = _cmd(execution_mode="exec")
        assert cmd.index("check_for_update_on_startup=false") < cmd.index("exec")

    def test_reasoning_effort_override_precedes_the_exec_subcommand(self) -> None:
        cmd = _cmd(execution_mode="exec", reasoning_effort="high")
        assert cmd.index('model_reasoning_effort="high"') < cmd.index("exec")

    def test_every_config_flag_precedes_the_exec_subcommand(self) -> None:
        cmd = _cmd(execution_mode="exec", reasoning_effort="high")
        exec_index = cmd.index("exec")
        assert self._config_flag_positions(cmd), "expected at least one -c pair"
        assert all(index < exec_index for index in self._config_flag_positions(cmd))

    def test_config_overrides_are_contiguous(self) -> None:
        """One block, one owner — overrides are emitted from a single place."""
        cmd = _cmd(reasoning_effort="high")
        positions = self._config_flag_positions(cmd)
        assert positions == list(range(positions[0], positions[0] + 2 * 2, 2))


class TestCodexBaseCommand:
    """Sanity-check the rest of the argv shape so a refactor that
    moves the ``--json`` decision around doesn't accidentally drop
    other flags. These aren't exhaustive — just enough to catch a
    structural regression."""

    def test_default_starts_interactive_codex(self) -> None:
        cmd = _cmd()
        assert cmd[0] == "codex"
        assert "exec" not in cmd[:2]

    def test_exec_mode_uses_codex_exec(self) -> None:
        cmd = _cmd(execution_mode="exec")
        assert cmd[0] == "codex"
        assert "exec" in cmd

    def test_full_auto_default(self) -> None:
        cmd = _cmd()
        assert "--ask-for-approval" in cmd
        assert "never" in cmd
        assert "--sandbox" in cmd
        assert "workspace-write" in cmd
        assert "--full-auto" not in cmd

    def test_exec_mode_uses_supported_full_auto_equivalent(self) -> None:
        cmd = _cmd(execution_mode="exec")
        assert "--full-auto" not in cmd
        assert cmd[cmd.index("--ask-for-approval") + 1] == "on-request"
        assert cmd.index("--ask-for-approval") < cmd.index("exec")
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"

    def test_yolo_swaps_to_dangerously_bypass(self) -> None:
        cmd = _cmd(approval_mode="yolo")
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--full-auto" not in cmd

    def test_exec_yolo_flag_precedes_subcommand(self) -> None:
        cmd = _cmd(execution_mode="exec", approval_mode="yolo")
        assert cmd.index("--dangerously-bypass-approvals-and-sandbox") < cmd.index(
            "exec"
        )

    def test_prompt_is_last_arg(self) -> None:
        cmd = CodexProvider().build_command(prompt="hello world")
        assert cmd[-1] == "hello world"

    def test_provider_is_interactive_by_default(self) -> None:
        provider = CodexProvider()
        assert provider.interactive is True
        assert provider.runs_interactively() is True
        assert provider.runs_interactively(execution_mode="exec") is False
        assert provider.needs_fresh_prompt_process() is True
        assert provider.needs_fresh_prompt_process(execution_mode="exec") is False
