"""Unit tests for ``CodexProvider.build_command``.

Sandbox-clean: no subprocess, no network. Asserts on the assembled
argv only.

Every interactive launch must declare an approved workspace (#215), so these
tests build one real (empty) repository directory for the whole module — the
trust resolver reads the launch directory's Git layout off disk, which a
function-scoped ``tmp_path`` would rebuild ~30 times for no added coverage.
The trust decision itself is covered in ``test_codex_workspace_trust.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import tomllib

import pytest

from issue_orchestrator.domain.sandbox_scope import SandboxScope
from issue_orchestrator.domain.workspace_trust import WorkspaceTrustError
from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider

from tests.workspace_trust import approved_workspace, make_repository

_REPOSITORY: Path | None = None


@pytest.fixture(scope="module", autouse=True)
def _approved_repository(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """One approved repository the module's launches run in."""
    global _REPOSITORY
    _REPOSITORY = make_repository(tmp_path_factory.mktemp("repo") / "checkout")
    yield
    _REPOSITORY = None


def _cmd(**kwargs: str) -> list[str]:
    """Build the argv with sane defaults; return the list for assertions."""
    assert _REPOSITORY is not None
    return CodexProvider().build_command(
        prompt="task",
        launch_workspace=approved_workspace(_REPOSITORY),
        **kwargs,
    )


def _trust_override(argv: list[str]) -> str:
    """The emitted workspace-trust override for the module's repository."""
    return f'projects={{ "{_REPOSITORY}" = {{ trust_level = "trusted" }} }}'


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
        """One block, one owner — overrides are emitted from a single place.

        An interactive launch emits three: the update-check suppression, the
        workspace-trust grant (#215), and the reasoning effort.
        """
        cmd = _cmd(reasoning_effort="high")
        positions = self._config_flag_positions(cmd)
        assert len(positions) == 3
        assert positions == list(
            range(positions[0], positions[0] + 2 * len(positions), 2)
        )
        assert _trust_override(cmd) in cmd


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
        assert _REPOSITORY is not None
        cmd = CodexProvider().build_command(
            prompt="hello world",
            launch_workspace=approved_workspace(_REPOSITORY),
        )
        assert cmd[-1] == "hello world"

    def test_provider_is_interactive_by_default(self) -> None:
        provider = CodexProvider()
        assert provider.interactive is True
        assert provider.runs_interactively() is True
        assert provider.runs_interactively(execution_mode="exec") is False
        assert provider.needs_fresh_prompt_process() is True
        assert provider.needs_fresh_prompt_process(execution_mode="exec") is False

    def test_workspace_trust_requirement_matches_what_build_command_does(self) -> None:
        """Config validation must not drift from the launch it predicts (#215).

        The capability is what a config-time check reads; the fail-closed
        denial is what a launch does. They are asserted together so the pair
        cannot come apart.
        """
        provider = CodexProvider()

        assert provider.requires_workspace_trust() is True
        with pytest.raises(WorkspaceTrustError):
            provider.build_command(prompt="hi")

        assert provider.requires_workspace_trust(execution_mode="exec") is False
        assert provider.build_command(prompt="hi", execution_mode="exec")


class TestProviderDefaultsDoNotDefineRoleAuthority:
    """#370 F5: the orchestrator's role scope outranks every provider default.

    The Human's role/model topology puts Codex in the Reviewer and Tech Lead
    seats. That is a model choice, and it must not become an authority choice:
    a role whose bounds the orchestrator computed may not be widened — or
    narrowed — by whichever approval/sandbox default the provider happens to
    carry, or by a stray kwarg surviving in a config.

    So when a ``SandboxScope`` is active the provider emits the scope's
    enforcing argv and nothing else on those axes. These fix that, including
    against the two kwargs that would otherwise grant the most.
    """

    @staticmethod
    def _scope() -> SandboxScope:
        assert _REPOSITORY is not None
        return SandboxScope(
            working_directory=_REPOSITORY,
            read_roots=(_REPOSITORY,),
            write_roots=(_REPOSITORY,),
            egress="model-only",
            deny_env=("GITHUB_TOKEN",),
            deny_read_files=("~/.ssh",),
        )

    @pytest.fixture(autouse=True)
    def _git_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The scope adapter reads the launch worktree's Git layout off disk.

        Stubbed to a fixed linked-worktree layout: what these tests are about
        is which approval/sandbox flags survive a scoped launch, not how a
        real ``.git`` file resolves — that is covered in
        ``test_sandbox_provider_adapter.py``.
        """
        from issue_orchestrator.execution.agent_runner_providers import (
            sandbox as sandbox_module,
        )
        from issue_orchestrator.execution.agent_runner_providers.sandbox import (
            GitWorktreeAccess,
        )

        common_dir = Path("/repo/.git")
        monkeypatch.setattr(
            sandbox_module,
            "resolve_git_worktree_access",
            lambda _worktree: GitWorktreeAccess(
                git_dir=common_dir / "worktrees" / "issue-42",
                common_dir=common_dir,
                head_ref=common_dir / "refs" / "heads" / "42-fix",
            ),
        )

    def _scoped_cmd(self, **kwargs: str) -> list[str]:
        assert _REPOSITORY is not None
        return CodexProvider().build_command(
            prompt="task",
            launch_workspace=approved_workspace(_REPOSITORY),
            sandbox_scope=self._scope(),
            **kwargs,
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"approval_mode": "yolo"},
            {"approval_mode": "full-auto"},
            {"approval_mode": "default"},
            {"sandbox": "danger-full-access"},
            {"approval_mode": "yolo", "sandbox": "danger-full-access"},
            {"execution_mode": "exec", "approval_mode": "yolo"},
        ],
    )
    def test_no_provider_default_reaches_a_scoped_launch(
        self, kwargs: dict[str, str]
    ) -> None:
        cmd = self._scoped_cmd(**kwargs)

        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "--sandbox" not in cmd
        assert "-s" not in cmd
        assert "--ask-for-approval" not in cmd

    def test_the_scope_still_pins_the_bounds_it_computed(self) -> None:
        """Not narrowed either: the role's own approval and cwd survive."""
        cmd = self._scoped_cmd(approval_mode="yolo")

        assert cmd[cmd.index("-a") + 1] == "never"
        assert cmd[cmd.index("-C") + 1] == str(_REPOSITORY)
        assert "--strict-config" in cmd

    def test_an_unscoped_launch_keeps_the_provider_default(self) -> None:
        """The falsification: remove the scope and the default reappears.

        Without this the parametrized test above would pass on a provider that
        emitted no flags at all, which would prove nothing about precedence.
        """
        cmd = _cmd(approval_mode="yolo")

        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
