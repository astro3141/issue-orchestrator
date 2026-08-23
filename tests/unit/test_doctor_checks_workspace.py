"""Unit tests for workspace doctor checks."""

import subprocess
from pathlib import Path

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config, DefaultAgentConfig
from issue_orchestrator.ports.command_runner import CommandResult, OutputNewlines
from issue_orchestrator.infra.doctor.checks.workspace import check_agents

from tests.workspace_trust import approval_for


def _agent_scripts_check(config: Config):
    return next(check for check in check_agents(config) if check.name == "Agent Scripts")


class _FakeProvider:
    def __init__(self, name: str, executable: str, available: bool = True):
        self.name = name
        self.executable = executable
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def requires_workspace_trust(self, **kwargs: object) -> bool:
        """Stand-in for a provider that needs no approved repository root.

        Present rather than omitted: the doctor asks every configured
        provider this, so a fake that could not answer would make the
        agent-script tests fail for an unrelated reason.
        """
        return False


def _patch_providers(monkeypatch, providers: dict[str, _FakeProvider]) -> None:
    def get_provider(name: str) -> _FakeProvider:
        try:
            return providers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown provider: {name!r}") from exc

    monkeypatch.setattr("issue_orchestrator.agent_runner.get_provider", get_provider)


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )


def _agent_prompts_check(config: Config, runner=None):
    return next(
        check for check in check_agents(config, runner=runner) if check.name == "Agent Prompts"
    )


def test_agent_scripts_use_provider_registry_for_provider_agents(
    monkeypatch,
    tmp_path: Path,
):
    config = Config()
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            provider="claude-code",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=tmp_path / "reviewer.md",
            provider="codex",
        ),
    }
    _patch_providers(
        monkeypatch,
        {
            "claude-code": _FakeProvider("claude-code", "claude"),
            "codex": _FakeProvider("codex", "codex"),
        },
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "ok"
    assert check.detail == "All found"


def test_agent_scripts_report_missing_configured_provider_cli(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("NVM_BIN", "/Users/test/.nvm/versions/node/v24.11.1/bin")
    monkeypatch.setenv("PATH", "/Users/test/.nvm/versions/node/v24.11.1/bin:/usr/bin:/bin")
    monkeypatch.setattr(
        "issue_orchestrator.infra.provider_cli_diagnostics._find_executable_outside_path",
        lambda executable: [Path("/Users/test/.nvm/versions/node/v24.14.1/bin/claude")],
    )
    config = Config()
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            provider="claude-code",
        ),
    }
    _patch_providers(
        monkeypatch,
        {"claude-code": _FakeProvider("claude-code", "claude", available=False)},
    )

    check = _agent_scripts_check(config)

    assert check.status == "error"
    assert check.detail == (
        "Missing: agent:backend: claude-code (expected executable: claude); "
        "executable 'claude' not found on PATH; "
        "NVM_BIN=/Users/test/.nvm/versions/node/v24.11.1/bin; "
        "found outside PATH: /Users/test/.nvm/versions/node/v24.14.1/bin/claude"
    )


def test_agent_scripts_use_default_agent_provider(monkeypatch, tmp_path: Path):
    config = Config()
    config.default_agent = DefaultAgentConfig(provider="codex")
    config.agents = {
        "agent:reviewer": AgentConfig(prompt_path=tmp_path / "reviewer.md"),
    }
    _patch_providers(monkeypatch, {"codex": _FakeProvider("codex", "codex")})
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "ok"
    assert check.detail == "All found"


def test_agent_scripts_still_validate_legacy_commands(monkeypatch, tmp_path: Path):
    config = Config()
    config.agents = {
        "agent:legacy": AgentConfig(
            prompt_path=tmp_path / "legacy.md",
            command="missing-agent --do-work",
        ),
    }
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "error"
    assert check.detail == "Missing: agent:legacy: missing-agent"


def test_agent_prompts_error_when_prompt_not_committed_to_head(
    monkeypatch,
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")
    (repo_root / "README.md").write_text("hello\n")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "init")

    prompt_path = repo_root / ".prompts" / "dev.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Prompt\n")

    config = Config()
    config.repo_root = repo_root
    config.worktree_seed_ref = "HEAD"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=prompt_path, provider="codex"),
    }

    check = _agent_prompts_check(config)

    assert check.status == "error"
    assert "Not available from worktree seed ref HEAD" in check.detail
    assert ".prompts/dev.md" in check.detail
    assert "set worktrees.seed_ref for local iteration" in check.detail


def test_agent_prompts_warn_when_prompt_only_modified_locally(
    monkeypatch,
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")
    prompt_path = repo_root / ".prompts" / "dev.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Prompt\n")
    _git(repo_root, "add", ".prompts/dev.md")
    _git(repo_root, "commit", "-m", "add prompt")
    prompt_path.write_text("Prompt updated\n")

    config = Config()
    config.repo_root = repo_root
    config.worktree_seed_ref = "HEAD"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=prompt_path, provider="codex"),
    }

    check = _agent_prompts_check(config)

    assert check.status == "warning"
    assert ".prompts/dev.md" in check.detail
    assert "seed ref version (HEAD)" in check.detail


def test_agent_prompts_use_injected_runner(monkeypatch, tmp_path: Path):
    class _RecordingRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(
            self,
            command: str | list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout_seconds: int | None = None,
            shell: bool = False,
            newlines: OutputNewlines = OutputNewlines.TRANSLATED,
        ) -> CommandResult:
            assert isinstance(command, list)
            self.commands.append(command)
            if "cat-file" in command:
                return CommandResult(returncode=0, stdout="", stderr="")
            if "status" in command:
                return CommandResult(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Unexpected git command: {command}")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    prompt_path = repo_root / ".prompts" / "dev.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Prompt\n")

    config = Config()
    config.repo_root = repo_root
    config.worktree_seed_ref = "HEAD"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=prompt_path, provider="codex"),
    }

    runner = _RecordingRunner()
    check = _agent_prompts_check(config, runner=runner)

    assert check.status == "ok"
    assert runner.commands == [
        ["git", "-C", str(repo_root), "cat-file", "-e", "HEAD:.prompts/dev.md"],
        ["git", "-C", str(repo_root), "status", "--porcelain", "--", ".prompts/dev.md"],
    ]


class TestWorkspaceTrustCheck:
    """An agent that cannot launch must be reported before it tries (#215).

    Without this, a configured Codex agent with no approved repository root is
    only discovered at launch, after the claim, the label, and the provisioned
    worktree have already been spent, and the operator's only signal is a
    stack trace.
    """

    def _check(self, config: Config):
        return next(
            (
                check
                for check in check_agents(config)
                if check.name == "Workspace Trust"
            ),
            None,
        )

    def _codex_config(self, tmp_path: Path, **agent_kwargs) -> Config:
        config = Config()
        config.agents = {
            "agent:goal-pilot": AgentConfig(
                prompt_path=tmp_path / "goal-pilot.md",
                provider="codex",
                **agent_kwargs,
            ),
        }
        return config

    def test_interactive_codex_agent_without_an_approval_is_an_error(
        self, tmp_path: Path
    ):
        check = self._check(self._codex_config(tmp_path))

        assert check is not None
        assert check.status == "error"
        assert "agent:goal-pilot" in check.detail
        assert "security.workspace_trust.approved_repository_root" in check.detail

    def test_the_error_names_the_decision_a_human_must_make(self, tmp_path: Path):
        """The fix is a human authority decision, so the message must carry it."""
        check = self._check(self._codex_config(tmp_path))

        assert check is not None
        assert "a human approved" in check.detail

    def test_a_recorded_approval_clears_it_and_names_its_authority(
        self, tmp_path: Path
    ):
        config = self._codex_config(tmp_path)
        config.workspace_trust = approval_for(
            tmp_path, authority_path=tmp_path / "selfhost.yaml"
        )

        check = self._check(config)

        assert check is not None
        assert check.status == "ok"
        assert str(tmp_path.resolve()) in check.detail
        assert "selfhost.yaml" in check.detail

    def test_a_codex_exec_agent_needs_no_approval(self, tmp_path: Path):
        """``codex exec`` never reaches the trust dialog, so it must not error."""
        check = self._check(
            self._codex_config(tmp_path, provider_args={"execution_mode": "exec"})
        )

        assert check is None

    def test_a_deployment_with_no_trust_requiring_agent_is_not_told_about_the_key(
        self, tmp_path: Path
    ):
        config = Config()
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=tmp_path / "backend.md",
                provider="claude-code",
            ),
        }

        assert self._check(config) is None

    def test_a_recorded_approval_is_reported_even_with_no_agent_needing_it(
        self, tmp_path: Path
    ):
        """An operator who edited the key must be able to see it took effect."""
        config = Config()
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=tmp_path / "backend.md",
                provider="claude-code",
            ),
        }
        config.workspace_trust = approval_for(tmp_path)

        check = self._check(config)

        assert check is not None
        assert check.status == "ok"
        assert str(tmp_path.resolve()) in check.detail

    def test_an_uninterpretable_execution_mode_denies(self, tmp_path: Path):
        """"I could not tell" is a denial here, as it is at launch."""
        check = self._check(
            self._codex_config(tmp_path, provider_args={"execution_mode": "sideways"})
        )

        assert check is not None
        assert check.status == "error"

    def test_uninterpretable_args_invent_no_requirement_for_claude(
        self, tmp_path: Path
    ):
        """Unreadable args must not manufacture a need the provider never has."""
        config = Config()
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=tmp_path / "backend.md",
                provider="claude-code",
                provider_args={"execution_mode": "sideways"},
            ),
        }

        assert self._check(config) is None

    def test_an_unknown_provider_is_not_reported_here(self, tmp_path: Path):
        """Config validation owns that error; this check must not double it."""
        config = Config()
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=tmp_path / "backend.md",
                provider="nonesuch",
            ),
        }

        assert self._check(config) is None
