"""The Codex-home isolation contract.

Codex writes a rollout transcript per session under ``$CODEX_HOME/sessions``.
Nothing in the test suite may spawn the real CLI against the operator's
``~/.codex``, and the rule has to hold for a test nobody remembered to
annotate. These tests cover the rule itself and the environment a spawn would
actually receive; ``tests/integration/test_codex_home_isolation.py`` proves the
guard fails a leaking test end to end.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.control.isolation import build_isolation_prefix
from issue_orchestrator.execution.agent_runner_env import (
    build_filtered_env,
    get_forbidden_env_vars,
)
from tests.codex_home import (
    CODEX_HOME_ENV,
    CODEX_HOME_POLICY,
    CodexHomePolicy,
    provision_codex_home,
    spawns_codex,
)


@pytest.fixture
def policy(tmp_path: Path) -> CodexHomePolicy:
    """A policy whose operator home is a stand-in for ``~/.codex``."""
    operator_home = tmp_path / "home" / ".codex"
    operator_home.mkdir(parents=True)
    return CodexHomePolicy(operator_home.resolve())


class TestCodexHomePolicy:
    """What counts as a leak into the operator's Codex home."""

    def test_unset_codex_home_is_a_leak(self, policy: CodexHomePolicy) -> None:
        leak = policy.describe_leak({})
        assert leak is not None
        assert str(policy.operator_home) in leak

    def test_empty_codex_home_is_a_leak(self, policy: CodexHomePolicy) -> None:
        assert policy.describe_leak({CODEX_HOME_ENV: ""}) is not None

    def test_operator_home_itself_is_a_leak(self, policy: CodexHomePolicy) -> None:
        env = {CODEX_HOME_ENV: str(policy.operator_home)}
        assert policy.describe_leak(env) is not None

    def test_path_under_operator_home_is_a_leak(
        self, policy: CodexHomePolicy
    ) -> None:
        env = {CODEX_HOME_ENV: str(policy.operator_home / "sessions" / "probe")}
        assert policy.describe_leak(env) is not None

    def test_sibling_sharing_a_name_prefix_is_not_a_leak(
        self, policy: CodexHomePolicy
    ) -> None:
        sibling = policy.operator_home.with_name(".codex-backup")
        assert policy.describe_leak({CODEX_HOME_ENV: str(sibling)}) is None

    def test_isolated_home_is_not_a_leak(
        self, policy: CodexHomePolicy, tmp_path: Path
    ) -> None:
        assert policy.describe_leak({CODEX_HOME_ENV: str(tmp_path)}) is None

    def test_user_relative_home_is_expanded_before_comparison(
        self, policy: CodexHomePolicy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(policy.operator_home.parent))
        assert policy.describe_leak({CODEX_HOME_ENV: "~/.codex"}) is not None

    def test_enforce_raises_naming_the_spawn(self, policy: CodexHomePolicy) -> None:
        with pytest.raises(AssertionError, match="codex --version"):
            policy.enforce({}, spawning="subprocess.Popen('codex --version')")

    def test_enforce_is_silent_when_isolated(
        self, policy: CodexHomePolicy, tmp_path: Path
    ) -> None:
        policy.enforce({CODEX_HOME_ENV: str(tmp_path)}, spawning="codex")


class TestProtectedHomeResolution:
    """Which homes the policy protects, given the environment it starts from."""

    @pytest.fixture
    def account_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Stand in for the operator's account, so ``~`` is a temp directory."""
        home = tmp_path / "operator"
        (home / ".codex").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        return (home / ".codex").resolve()

    def test_unset_codex_home_protects_the_account_default(
        self, account_home: Path
    ) -> None:
        policy = CodexHomePolicy.for_environment({})
        assert policy.protected_homes == (account_home,)

    def test_codex_home_below_the_account_default_protects_both(
        self, account_home: Path
    ) -> None:
        """A subdirectory config must not demote ``~/.codex`` to a bystander."""
        policy = CodexHomePolicy.for_environment(
            {CODEX_HOME_ENV: str(account_home / "ci")}
        )

        assert policy.operator_home == account_home / "ci"
        assert policy.describe_leak({CODEX_HOME_ENV: str(account_home)}) is not None
        leak = policy.describe_leak({CODEX_HOME_ENV: str(account_home / "sibling")})
        assert leak is not None

    def test_codex_home_elsewhere_still_protects_the_account_default(
        self, account_home: Path, tmp_path: Path
    ) -> None:
        elsewhere = (tmp_path / "shared-codex").resolve()
        policy = CodexHomePolicy.for_environment({CODEX_HOME_ENV: str(elsewhere)})

        assert policy.protected_homes == (elsewhere, account_home)
        assert policy.describe_leak({CODEX_HOME_ENV: str(account_home)}) is not None

    def test_account_default_is_not_listed_twice(self, account_home: Path) -> None:
        policy = CodexHomePolicy.for_environment({CODEX_HOME_ENV: str(account_home)})
        assert policy.protected_homes == (account_home,)

    def test_this_session_protects_the_real_account_default(self) -> None:
        """The policy the whole suite runs under, not a constructed one."""
        assert (Path.home() / ".codex").resolve() in CODEX_HOME_POLICY.protected_homes


class TestCodexSpawnDetection:
    """Which commands the guard treats as starting the real Codex CLI."""

    @pytest.mark.parametrize(
        "command",
        [
            ["codex", "--version"],
            ["/opt/homebrew/bin/codex", "exec", "hi"],
            'cd "/wt" && export PATH="/x" && codex exec --json "prompt"',
            ["/bin/bash", "-c", "unset GH_TOKEN && codex exec 'do a thing'"],
            ("/bin/bash", ["-c", "codex exec 'do a thing'"]),
        ],
    )
    def test_codex_invocations_are_detected(self, command: object) -> None:
        assert spawns_codex(command) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "command",
        [
            ["git", "status"],
            ["pytest", "tests/integration/test_codex_execution.py"],
            ["ls", "/tmp/codex-home"],
            ["claude", "-p", "write about codex.md"],
        ],
    )
    def test_unrelated_commands_are_not_detected(self, command: object) -> None:
        assert spawns_codex(command) is False  # type: ignore[arg-type]


class TestProvisionedHome:
    """What an isolated home carries, and what it deliberately leaves behind."""

    def test_credentials_are_copied_but_operator_config_is_not(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "auth.json").write_text('{"token": "x"}', encoding="utf-8")
        (source / "config.toml").write_text("model = 'operator'\n", encoding="utf-8")

        home = provision_codex_home(tmp_path / "isolated", source=source)

        assert (home / "auth.json").read_text(encoding="utf-8") == '{"token": "x"}'
        assert not (home / "config.toml").exists()

    def test_missing_credentials_do_not_fail_provisioning(
        self, tmp_path: Path
    ) -> None:
        home = provision_codex_home(tmp_path / "isolated", source=tmp_path / "absent")
        assert home.is_dir()


class TestEffectiveSpawnEnvironment:
    """The environment a Codex spawn would receive from *this* test session."""

    def test_inherited_process_environment_is_isolated(self) -> None:
        assert CODEX_HOME_POLICY.describe_leak(os.environ) is None

    def test_filtered_agent_environment_carries_the_isolated_home(self) -> None:
        env = build_filtered_env()
        assert env[CODEX_HOME_ENV] == os.environ[CODEX_HOME_ENV]
        assert CODEX_HOME_POLICY.describe_leak(env) is None

    def test_allowlist_filtered_environment_fails_closed_rather_than_leaking(
        self,
    ) -> None:
        """Allowlist mode drops ``CODEX_HOME``; the guard must then refuse.

        ``CODEX_HOME`` is not in ``ALWAYS_PASSTHROUGH_ENV_VARS``, so an agent
        spec that opts into ``passthrough_vars`` hands Codex an environment
        with no home at all - which resolves to the operator's. That must be a
        loud spawn-time failure, not a silent leak.
        """
        env = build_filtered_env(passthrough_vars=["PATH"])

        assert CODEX_HOME_ENV not in env
        with pytest.raises(AssertionError, match="Codex home leak"):
            CODEX_HOME_POLICY.enforce(env, spawning="subprocess.Popen(['codex'])")

    def test_shell_isolation_prefix_does_not_strip_the_isolated_home(
        self, tmp_path: Path
    ) -> None:
        assert CODEX_HOME_ENV not in get_forbidden_env_vars()
        assert f"unset {CODEX_HOME_ENV}" not in build_isolation_prefix(tmp_path)

    def test_per_test_isolation_layers_on_the_session_default(
        self, isolated_codex_home: Path
    ) -> None:
        assert os.environ[CODEX_HOME_ENV] == str(isolated_codex_home)
        assert CODEX_HOME_POLICY.describe_leak(os.environ) is None
