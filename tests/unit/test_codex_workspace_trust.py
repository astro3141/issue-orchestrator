"""The approved Codex repository-root trust, materialized per launch (#215).

A fresh Codex launch in a linked managed worktree parks forever on the
interactive workspace-trust dialog: trust decides whether the repository's own
files may configure Codex, and it is settled before the layers
``--ask-for-approval`` / ``--sandbox`` /
``--dangerously-bypass-approvals-and-sandbox`` live in are assembled (#204).

These tests pin the whole decision, in the direction that matters — **absent
approval denies**:

* the approval is one absolute repository root, never a travelling boolean;
* the root is resolved as Codex's *common* repository root, so a linked
  worktree resolves to the checkout that owns the git common directory;
* absent, malformed, unreadable, non-git, and mismatched all fail closed
  before anything spawns;
* the grant is materialized as exactly one root-command ``-c`` override,
  ahead of any subcommand, displacing none of the existing overrides;
* nothing is written to any Codex home.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.workspace_trust import (
    ApprovedRepositoryTrust,
    LaunchWorkspace,
    RepositoryTrustGrant,
    TrustAuthoritySource,
    WorkspaceTrustError,
    launch_attribution,
)
from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider
from issue_orchestrator.execution.agent_runner_providers.codex_trust import (
    CODEX_TRUST_MECHANISM,
    authorize_codex_workspace_trust,
    codex_trust_override_argv,
    resolve_codex_common_repository_root,
)

from tests.workspace_trust import (
    APPROVAL_FINGERPRINT,
    approval_for,
    approved_workspace,
    make_linked_worktree,
    make_repository,
)

TRUST_KEY_PREFIX = "projects="


def _config_overrides(argv: list[str]) -> dict[str, object]:
    """Decode every ``-c key=<toml>`` pair in *argv* the way Codex would."""
    overrides: dict[str, object] = {}
    for index, token in enumerate(argv):
        if token != "-c":
            continue
        key, raw = argv[index + 1].split("=", 1)
        overrides[key] = tomllib.loads(f"value = {raw}")["value"]
    return overrides


def _trust_overrides(argv: list[str]) -> list[str]:
    """Every emitted ``-c`` pair that assigns Codex's ``projects`` table."""
    return [
        argv[index + 1]
        for index, token in enumerate(argv)
        if token == "-c" and argv[index + 1].startswith(TRUST_KEY_PREFIX)
    ]


class TestApprovalIsAnAbsoluteRootAndNothingElse:
    """Malformed authority state is a hard failure, never a weaker grant."""

    @pytest.mark.parametrize(
        "root",
        [
            pytest.param(Path("relative/repo"), id="relative"),
            pytest.param(Path("~/repo"), id="home-anchored"),
            pytest.param(Path("/repos/../repo"), id="unnormalized"),
            pytest.param(Path("/"), id="filesystem-root"),
            pytest.param(Path('/repos/we"ird'), id="quote"),
            pytest.param(Path("/repos/we\nird"), id="newline"),
        ],
    )
    def test_unusable_root_is_rejected(self, root: Path) -> None:
        with pytest.raises(WorkspaceTrustError):
            ApprovedRepositoryTrust(
                repository_root=root,
                source=TrustAuthoritySource(
                    path=Path("/approvals/selfhost.yaml"),
                    fingerprint=APPROVAL_FINGERPRINT,
                ),
            )

    def test_authority_source_must_be_identifiable(self) -> None:
        """A grant with no fingerprint could not be audited back to a document."""
        with pytest.raises(WorkspaceTrustError):
            TrustAuthoritySource(path=Path("/approvals/selfhost.yaml"), fingerprint="")
        with pytest.raises(WorkspaceTrustError):
            TrustAuthoritySource(
                path=Path("relative.yaml"), fingerprint=APPROVAL_FINGERPRINT
            )

    def test_grant_cannot_exist_for_a_root_that_was_not_approved(self) -> None:
        """Construction *is* the verification — there is no unverified grant."""
        with pytest.raises(WorkspaceTrustError, match="not the approved root"):
            RepositoryTrustGrant(
                approved=approval_for(Path("/repos/approved")),
                resolved_common_root=Path("/repos/other"),
                mechanism=CODEX_TRUST_MECHANISM,
            )


class TestCommonRootResolution:
    """Codex keys trust to the owner of the git *common* directory."""

    def test_main_checkout_resolves_to_itself(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path / "repo")
        assert resolve_codex_common_repository_root(repository) == repository

    def test_linked_worktree_resolves_to_the_repository_root(
        self, tmp_path: Path
    ) -> None:
        """The trust key is the checkout, not the worktree the agent runs in.

        This is the case the project-config root walk gets wrong: a linked
        worktree's ``.git`` *exists* (as a file), so a walk that stops at the
        first ``.git`` never reaches the root Codex actually trusts.
        """
        repository = make_repository(tmp_path / "repo")
        worktree = make_linked_worktree(repository, tmp_path / "wt-215")

        assert (worktree / ".git").is_file()
        assert resolve_codex_common_repository_root(worktree) == repository

    def test_non_git_directory_fails_closed(self, tmp_path: Path) -> None:
        plain = tmp_path / "plaindir"
        plain.mkdir()
        with pytest.raises(WorkspaceTrustError, match="Cannot resolve"):
            resolve_codex_common_repository_root(plain)

    def test_unreadable_pointer_fails_closed(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        with pytest.raises(WorkspaceTrustError, match="Cannot resolve"):
            resolve_codex_common_repository_root(worktree)

    def test_dangling_worktree_pointer_fails_closed(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {tmp_path / 'gone' / 'worktrees' / 'wt'}\n", encoding="utf-8"
        )
        with pytest.raises(WorkspaceTrustError, match="Cannot resolve"):
            resolve_codex_common_repository_root(worktree)


class TestAuthorization:
    """Authorization is the whole gate: absent or mismatched means no launch."""

    def test_absent_approval_denies(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path / "repo")
        with pytest.raises(WorkspaceTrustError, match="no approved repository-root"):
            authorize_codex_workspace_trust(
                LaunchWorkspace(working_directory=repository)
            )

    def test_a_different_checkout_of_the_same_repository_is_denied(
        self, tmp_path: Path
    ) -> None:
        """The live case: the recovery checkout is not the approved root.

        Two checkouts of one project are two repository roots. An approval
        naming one grants the other nothing — which is exactly why the
        approval is an absolute path and not "the current repository".
        """
        approved = make_repository(tmp_path / "io-fork" / "issue-orchestrator")
        recovery = make_repository(tmp_path / "io-recovery" / "issue-orchestrator")
        recovery_worktree = make_linked_worktree(recovery, tmp_path / "wt-215")

        with pytest.raises(WorkspaceTrustError, match="not the approved root"):
            authorize_codex_workspace_trust(
                LaunchWorkspace(
                    working_directory=recovery_worktree,
                    approved_trust=approval_for(approved),
                )
            )

    def test_approved_linked_worktree_is_granted(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path / "repo")
        worktree = make_linked_worktree(repository, tmp_path / "wt-215")

        grant = authorize_codex_workspace_trust(
            approved_workspace(worktree, repository)
        )

        assert grant.repository_root == repository
        assert grant.resolved_common_root == repository

    def test_grant_evidence_reconstructs_the_decision(self, tmp_path: Path) -> None:
        """Launch evidence answers *why* this repository was trusted."""
        repository = make_repository(tmp_path / "repo")
        worktree = make_linked_worktree(repository, tmp_path / "wt-215")
        authority = tmp_path / "approvals" / "selfhost.yaml"
        authority.parent.mkdir()
        authority.write_text("security: {}\n", encoding="utf-8")

        grant = authorize_codex_workspace_trust(
            LaunchWorkspace(
                working_directory=worktree,
                approved_trust=approval_for(repository, authority_path=authority),
            )
        )

        assert grant.evidence() == {
            "approved_repository_root": str(repository),
            "resolved_common_root": str(repository),
            "authority_source": str(authority),
            "authority_fingerprint": APPROVAL_FINGERPRINT,
            "mechanism": CODEX_TRUST_MECHANISM,
            "verified": "true",
        }


class TestMaterialization:
    """One root-command override, scoped to the approved root."""

    def test_override_trusts_the_approved_root_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        """One ``projects`` table, one entry, ``trust_level = "trusted"``.

        The dotted spelling #215 named —
        ``projects."<root>".trust_level="trusted"`` — is not what 0.147.0's
        ``-c`` parser resolves: it splits the key on every ``.``, and a path is
        full of them, so the grant never lands and the dialog still blocks.
        The live proof caught that. Assigning the table does land, and narrows
        the launch to exactly this root: no other project the user layer
        trusts survives into this process.
        """
        repository = make_repository(tmp_path / "repo")
        grant = authorize_codex_workspace_trust(approved_workspace(repository))

        argv = codex_trust_override_argv(grant)

        assert argv[0] == "-c"
        key, raw = argv[1].split("=", 1)
        assert key == "projects"
        assert tomllib.loads(f"value = {raw}")["value"] == {
            str(repository): {"trust_level": "trusted"},
        }

    def test_interactive_launch_carries_exactly_one_trust_override(
        self, tmp_path: Path
    ) -> None:
        repository = make_repository(tmp_path / "repo")
        worktree = make_linked_worktree(repository, tmp_path / "wt-215")

        argv = CodexProvider().build_command(
            prompt="task",
            launch_workspace=approved_workspace(worktree, repository),
        )

        assert _trust_overrides(argv) == [
            f'projects={{ "{repository}" = {{ trust_level = "trusted" }} }}'
        ]

    def test_trust_override_precedes_any_subcommand_and_the_prompt(
        self, tmp_path: Path
    ) -> None:
        """#205's invariant: a subcommand-level ``-c`` discards the root list.

        Codex drops the *whole* root-level ``-c`` list when a subcommand
        carries its own — which is how the sandbox permission profile was
        silently lost once already. The trust grant must never be the override
        that re-opens that.
        """
        repository = make_repository(tmp_path / "repo")
        argv = CodexProvider().build_command(
            prompt="task",
            model="gpt-5.3-codex",
            launch_workspace=approved_workspace(repository),
        )

        trust_index = argv.index(
            f'projects={{ "{repository}" = {{ trust_level = "trusted" }} }}'
        )
        assert argv[trust_index - 1] == "-c"
        assert trust_index < argv.index("--model")
        assert trust_index < len(argv) - 1  # the prompt is last
        assert "exec" not in argv

    def test_trust_override_displaces_no_existing_override(
        self, tmp_path: Path
    ) -> None:
        repository = make_repository(tmp_path / "repo")

        overrides = _config_overrides(
            CodexProvider().build_command(
                prompt="task",
                reasoning_effort="xhigh",
                launch_workspace=approved_workspace(repository),
            )
        )

        assert overrides["check_for_update_on_startup"] is False
        assert overrides["model_reasoning_effort"] == "xhigh"
        assert overrides["projects"] == {str(repository): {"trust_level": "trusted"}}

    def test_interactive_launch_without_approval_does_not_build(
        self, tmp_path: Path
    ) -> None:
        repository = make_repository(tmp_path / "repo")
        with pytest.raises(WorkspaceTrustError):
            CodexProvider().build_command(
                prompt="task",
                launch_workspace=LaunchWorkspace(working_directory=repository),
            )

    def test_interactive_launch_without_a_workspace_does_not_build(self) -> None:
        """No declared workspace is no declared approval, so it denies."""
        with pytest.raises(WorkspaceTrustError, match="no launch workspace"):
            CodexProvider().build_command(prompt="task")

    def test_materialization_writes_nothing_to_any_codex_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The grant's lifetime is the launch; no host state is mutated.

        Per-launch materialization was chosen over seeding
        ``$CODEX_HOME/config.toml`` or a ``-p`` profile file precisely so the
        operator's Codex home — shared with the desktop app — is never touched.
        """
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        config_toml = codex_home / "config.toml"
        config_toml.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        before = {
            path: path.read_bytes() for path in sorted(codex_home.rglob("*"))
        }

        repository = make_repository(tmp_path / "repo")
        CodexProvider().build_command(
            prompt="task",
            launch_workspace=approved_workspace(repository),
        )

        after = {path: path.read_bytes() for path in sorted(codex_home.rglob("*"))}
        assert after == before


class TestOrchestratedLaunchSeam:
    """The orchestrated command path always declares what it may trust."""

    def _agent(self, tmp_path: Path, **kwargs: object) -> AgentConfig:
        prompt = tmp_path / "prompt.md"
        prompt.write_text("do the work", encoding="utf-8")
        return AgentConfig(
            prompt_path=prompt,
            provider="codex",
            ai_system="codex",
            model="gpt-5.3-codex",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_agent_without_an_approval_cannot_render_a_codex_launch(
        self, tmp_path: Path
    ) -> None:
        repository = make_repository(tmp_path / "repo")
        worktree = make_linked_worktree(repository, tmp_path / "wt-215")

        with pytest.raises(WorkspaceTrustError):
            self._agent(tmp_path).get_command_for_prompt(
                "review this", worktree=worktree
            )

    def test_agent_launch_in_an_unapproved_checkout_is_denied(
        self, tmp_path: Path
    ) -> None:
        approved = make_repository(tmp_path / "io-fork" / "issue-orchestrator")
        recovery = make_repository(tmp_path / "io-recovery" / "issue-orchestrator")
        worktree = make_linked_worktree(recovery, tmp_path / "wt-215")

        with pytest.raises(WorkspaceTrustError, match="not the approved root"):
            self._agent(
                tmp_path, workspace_trust=approval_for(approved)
            ).get_command_for_prompt("review this", worktree=worktree)

    def test_approved_agent_launch_carries_the_grant_in_its_command(
        self, tmp_path: Path
    ) -> None:
        repository = make_repository(tmp_path / "repo")
        worktree = make_linked_worktree(repository, tmp_path / "wt-215")

        command = self._agent(
            tmp_path, workspace_trust=approval_for(repository)
        ).get_command_for_prompt("review this", worktree=worktree)

        assert (
            f'projects={{ "{repository}" = {{ trust_level = "trusted" }} }}'
            in command
        )

    def test_claude_launches_are_unchanged_by_the_trust_seam(
        self, tmp_path: Path
    ) -> None:
        """Claude materializes no grant; its trust confirmation is unchanged."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("do the work", encoding="utf-8")
        repository = make_repository(tmp_path / "repo")
        agent = AgentConfig(prompt_path=prompt, provider="claude-code", model="opus")

        without = agent.get_command_for_prompt("work", worktree=repository)
        with_approval = AgentConfig(
            prompt_path=prompt,
            provider="claude-code",
            model="opus",
            workspace_trust=approval_for(repository),
        ).get_command_for_prompt("work", worktree=repository)

        assert without == with_approval
        assert "trust_level" not in without


class TestLaunchRecordAttribution:
    """A session's own record says which approval its launch carried."""

    def test_absent_approval_is_recorded_as_absent(self) -> None:
        """Empty values, not missing keys — silence must not read as "unknown"."""
        assert launch_attribution(None) == {
            "workspace_trust_approved_root": "",
            "workspace_trust_authority": "",
            "workspace_trust_authority_fingerprint": "",
        }

    def test_approval_names_its_root_and_authority_document(self) -> None:
        approval = approval_for(
            Path("/repos/approved"), authority_path=Path("/approvals/selfhost.yaml")
        )

        assert launch_attribution(approval) == {
            "workspace_trust_approved_root": "/repos/approved",
            "workspace_trust_authority": "/approvals/selfhost.yaml",
            "workspace_trust_authority_fingerprint": APPROVAL_FINGERPRINT,
        }
