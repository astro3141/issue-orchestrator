"""Tests for the worktree runtime setup owner.

These pin two things the lifecycle module used to own inline:

1. Applying setup to a worktree produces a *complete* runnable state, and says
   so through a typed result rather than leaving callers to infer it.
2. The failure semantics of each step. Runtime setup used to degrade silently
   (a phantom worktree identity, a dropped ``--no-verify`` flag, a settings
   file replaced without a word), which turned a broken worktree into a
   confusing session failure much later.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.adapters.worktree.api import (
    WorktreeError,
    WorktreeRuntimeSetup,
    install_claude_settings,
)
from issue_orchestrator.adapters.worktree._worktree_runtime import (
    ALLOW_NO_VERIFY_DRY_RUN_PATH,
    CLAUDE_SETTINGS_FOR_AGENTS,
)
from issue_orchestrator.ports.worktree_manager import WORKTREE_ID_MARKER
from tests.unit.worktree_git_helpers import (
    GitWorktree,
    block_worktree_config_writes,
    effective_hooks_path,
    make_git_worktree,
)


def _break_read_of(
    monkeypatch: pytest.MonkeyPatch, target: Path, error: OSError
) -> None:
    """Make exactly one path fail to read while its bytes stay intact on disk.

    This is the case the owner has to tell apart from "the content is broken",
    and it cannot be staged with ``chmod`` alone — a root test runner reads a
    ``0o000`` file happily, so the test would pass for the wrong reason there
    and fail for the wrong reason here. Only ``target`` is affected; every other
    read in the setup sequence runs for real.
    """
    original_read_text = Path.read_text

    def guarded(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise error
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)


def _install_environment(
    checkout: Path, *, editable_source: Path | None = None
) -> Path:
    """Build a ``.venv`` in ``checkout`` that is genuinely an environment.

    The marker file and the interpreter are what separate an environment from a
    directory named ``.venv``, and ``editable_source`` is the record that says
    which source tree the environment resolves imports from — the record uv
    writes, and the one the incident found naming another checkout (#53/#61).
    """
    venv = checkout / ".venv"
    site_packages = venv / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\nimplementation = CPython\nversion_info = 3.14.0\n"
    )
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    if editable_source is not None:
        (site_packages / "_editable_impl_project.pth").write_text(
            f"{editable_source}\n"
        )
    return venv


@pytest.fixture
def git_worktree(tmp_path: Path) -> GitWorktree:
    """A real repository plus one linked worktree, and a repository venv.

    Real Git rather than a hand-built ``.git``: setup asks the repository
    whether it owns the CLI-tools path before planting anything there, and a
    fabricated ``.git`` directory has no answer to give — which the owner
    treats as a failure rather than a licence to guess.

    The repository venv exists, and is a real working one recording its own
    checkout, so the tests below can prove setup neither reaches for it nor
    reaches *into* it (#53).
    """
    worktree = make_git_worktree(tmp_path, name="repo-123")
    _install_environment(worktree.main_repo, editable_source=worktree.main_repo)
    return worktree


@pytest.fixture
def repo_root(git_worktree: GitWorktree) -> Path:
    return git_worktree.main_repo


@pytest.fixture
def worktree_path(git_worktree: GitWorktree) -> Path:
    return git_worktree.worktree_path


def _setup(**overrides) -> WorktreeRuntimeSetup:
    options = {"enforce_hooks": False}
    options.update(overrides)
    return WorktreeRuntimeSetup(**options)


class TestApplyProducesRunnableWorktree:
    """One call must leave the worktree ready for an agent session."""

    def test_apply_installs_every_runtime_artifact(self, repo_root, worktree_path):
        state = _setup().apply(worktree_path)

        assert (worktree_path / ".claude" / "settings.json").exists()
        assert (worktree_path / WORKTREE_ID_MARKER).read_text() == state.worktree_id
        assert state.synced_cli_tool_paths
        for relative in state.synced_cli_tool_paths:
            assert (worktree_path / relative).exists()

    def test_apply_hides_runtime_artifacts_from_git_status(
        self, repo_root, worktree_path
    ):
        state = _setup().apply(worktree_path)

        exclude_text = (
            repo_root / ".git" / "worktrees" / "repo-123" / "info" / "exclude"
        ).read_text()
        assert ".claude/settings.json" in exclude_text
        assert str(WORKTREE_ID_MARKER) in exclude_text
        assert str(state.synced_cli_tool_paths[0]) in exclude_text

    def test_apply_reports_what_it_did(self, repo_root, worktree_path):
        state = _setup(
            enforce_hooks=False, allow_no_verify_dry_run_preflight=True
        ).apply(worktree_path)

        assert state.worktree_path == worktree_path
        assert state.worktree_id.startswith("wt-")
        assert state.hooks_installed is False
        assert state.no_verify_dry_run_allowed is True

    def test_apply_is_idempotent_and_keeps_worktree_identity(
        self, repo_root, worktree_path
    ):
        setup = _setup()

        first = setup.apply(worktree_path)
        second = setup.apply(worktree_path)

        assert second.worktree_id == first.worktree_id

    def test_hooks_are_installed_when_enforced(self, tmp_path):
        # Real repo: "installed" means git will run it, which only a real repo
        # can be asked.
        wt = make_git_worktree(tmp_path)

        state = WorktreeRuntimeSetup(enforce_hooks=True).apply(wt.worktree_path)

        assert state.hooks_installed is True
        assert (wt.hooks_dir / "pre-push").exists()
        assert effective_hooks_path(wt.worktree_path) == str(wt.hooks_dir)

    def test_hooks_are_skipped_when_not_enforced(self, repo_root, worktree_path):
        state = WorktreeRuntimeSetup(enforce_hooks=False).apply(worktree_path)

        assert state.hooks_installed is False
        assert not (
            repo_root / ".git" / "worktrees" / "repo-123" / "hooks" / "pre-push"
        ).exists()


class TestWorktreeRuntimeEnvironmentIsNotShared:
    """A worktree's ``.venv`` is its own, or absent — never another checkout's.

    Setup used to plant a symlink to the repository venv, which made one
    environment serve every checkout. ``worktrees.setup`` then runs the
    repository's setup recipe *inside* the worktree, so every provisioning run
    wrote through that link into the shared environment (#53). These pin the
    replacement invariant: the link is never created, and an existing one is
    removed before the worktree is handed on.
    """

    def test_setup_does_not_share_the_repository_venv(self, repo_root, worktree_path):
        _setup().apply(worktree_path)

        assert not (worktree_path / ".venv").exists()
        assert not (worktree_path / ".venv").is_symlink()
        # Nothing was installed in its place either: building the worktree's
        # environment belongs to `worktrees.setup`, not to worktree creation.
        assert (repo_root / ".venv").is_dir()

    def test_an_existing_shared_link_is_removed(self, repo_root, worktree_path):
        # The state every worktree created before this fix is sitting in.
        (worktree_path / ".venv").symlink_to(
            repo_root / ".venv", target_is_directory=True
        )

        _setup().apply(worktree_path)

        assert not (worktree_path / ".venv").is_symlink()
        assert (repo_root / ".venv" / "bin").is_dir()

    def test_a_dangling_shared_link_is_removed(self, repo_root, worktree_path):
        # What removing the *repository* checkout leaves behind; a dangling
        # link is still a write that would escape the worktree.
        (worktree_path / ".venv").symlink_to(
            repo_root / "gone" / ".venv", target_is_directory=True
        )

        _setup().apply(worktree_path)

        assert not (worktree_path / ".venv").is_symlink()

    def test_a_worktree_local_venv_is_left_alone(self, worktree_path):
        # The state `worktrees.setup` leaves behind: this worktree's own
        # environment, recording this worktree as its source. Deleting it would
        # make every reused worktree pay a full install it does not need.
        venv = _install_environment(worktree_path, editable_source=worktree_path)

        _setup().apply(worktree_path)

        assert (venv / "bin" / "python").exists()
        assert (venv / "pyvenv.cfg").exists()

    def test_a_link_that_stays_inside_the_worktree_is_left_alone(self, worktree_path):
        # A write through it cannot reach another checkout, and what it points
        # at is this worktree's own working environment.
        (worktree_path / "runtime").mkdir()
        _install_environment(worktree_path / "runtime", editable_source=worktree_path)
        (worktree_path / ".venv").symlink_to(
            worktree_path / "runtime" / ".venv", target_is_directory=True
        )

        _setup().apply(worktree_path)

        assert (worktree_path / ".venv").is_symlink()

    def test_removal_never_reaches_into_the_environment_it_unlinks(
        self, repo_root, worktree_path
    ):
        # Emptying the link instead of unlinking it would delete the contents of
        # the environment another checkout is using: the same defect with a
        # bigger blast radius, not a fix for it.
        (worktree_path / ".venv").symlink_to(
            repo_root / ".venv", target_is_directory=True
        )

        _setup().apply(worktree_path)

        assert (repo_root / ".venv" / "pyvenv.cfg").is_file()
        assert (repo_root / ".venv" / "bin" / "python").is_file()
        assert (
            repo_root
            / ".venv"
            / "lib"
            / "python3.14"
            / "site-packages"
            / "_editable_impl_project.pth"
        ).read_text().strip() == str(repo_root)

    def test_an_unremovable_shared_link_fails_setup(
        self, repo_root, worktree_path, monkeypatch
    ):
        # Continuing here would hand provisioning a worktree that still writes
        # into the repository's environment — the defect itself.
        (worktree_path / ".venv").symlink_to(
            repo_root / ".venv", target_is_directory=True
        )
        original_unlink = Path.unlink

        def guarded(self: Path, *args: Any, **kwargs: Any) -> None:
            if self == worktree_path / ".venv":
                raise PermissionError("simulated unlink failure")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", guarded)

        with pytest.raises(WorktreeError, match="remove shared venv link"):
            _setup().apply(worktree_path)

        assert (worktree_path / ".venv").is_symlink()


class TestAVenvDirectoryMustProveItIsThisWorktreesOwn:
    """A ``.venv`` directory is reused only when it proves it is ours (#61).

    The link cases above were the half of #53 that got fixed first. The other
    half is that a ``.venv`` which is a *real directory* was trusted for being a
    directory — no check that it belonged to this worktree, none that it was
    usable — so a worktree carrying what a previous failed run left behind
    passed setup untouched, and the recipe's own ``[ -d .venv ]`` test kept it.
    ``uv sync`` was then handed an environment it could not use, found the
    project installed-but-mismatched against a record naming another checkout,
    and reconciled by rewriting that checkout's environment.

    So provenance and health are both established here, and anything that fails
    either is removed rather than reused.
    """

    def test_a_directory_that_is_not_an_environment_is_removed(self, worktree_path):
        # The shape the incident left behind: an interpreter under `bin`, a
        # stray `.pth`, no `pyvenv.cfg` — and enough of a directory to satisfy
        # `[ -d .venv ]`, which is what kept it.
        venv = worktree_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n")
        (venv / "_virtualenv.pth").write_text("import _virtualenv\n")

        _setup().apply(worktree_path)

        assert not venv.exists()

    def test_an_environment_recording_another_checkout_is_removed(
        self, repo_root, worktree_path
    ):
        # Structurally a perfectly good environment — marker, interpreter, a
        # populated `site-packages`. It is only the recorded source path that
        # says it is not this worktree's, and that is the whole defect.
        venv = _install_environment(worktree_path, editable_source=repo_root)

        _setup().apply(worktree_path)

        assert not venv.exists()
        assert (repo_root / ".venv" / "pyvenv.cfg").is_file()

    def test_an_environment_whose_direct_url_names_another_checkout_is_removed(
        self, repo_root, worktree_path
    ):
        # The installer's other way of writing down where a distribution came
        # from. One record type checked and the other not would be a gap the
        # next installer release could walk through.
        venv = _install_environment(worktree_path)
        dist_info = venv / "lib" / "python3.14" / "site-packages" / "project.dist-info"
        dist_info.mkdir()
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": repo_root.as_uri(), "dir_info": {"editable": True}})
        )

        _setup().apply(worktree_path)

        assert not venv.exists()

    def test_an_environment_recording_this_worktree_is_reused(self, worktree_path):
        # The ordinary reused worktree. Rebuilding this one would introduce the
        # per-session full install the fix is explicitly not allowed to add.
        venv = _install_environment(worktree_path, editable_source=worktree_path)
        marker = venv / "lib" / "python3.14" / "site-packages" / "installed-package"
        marker.write_text("kept\n")

        _setup().apply(worktree_path)
        _setup().apply(worktree_path)

        assert marker.read_text() == "kept\n"

    def test_an_environment_whose_interpreter_is_gone_is_removed(self, worktree_path):
        # A base interpreter that has been upgraded out from under the venv.
        # It satisfies every structural test except the one that matters.
        venv = _install_environment(worktree_path, editable_source=worktree_path)
        (venv / "bin" / "python").unlink()
        (venv / "bin" / "python").symlink_to(worktree_path / "no-such-interpreter")

        _setup().apply(worktree_path)

        assert not venv.exists()

    def test_an_unreadable_install_record_is_not_trusted(
        self, worktree_path, monkeypatch
    ):
        # Unreadable is not evidence of provenance. Passing it would trust an
        # environment on the strength of a question that was never answered.
        venv = _install_environment(worktree_path, editable_source=worktree_path)
        record = (
            venv / "lib" / "python3.14" / "site-packages" / "_editable_impl_project.pth"
        )
        _break_read_of(monkeypatch, record, OSError("simulated read failure"))

        _setup().apply(worktree_path)

        assert not venv.exists()

    def test_an_unremovable_untrusted_environment_fails_setup(
        self, repo_root, worktree_path, monkeypatch
    ):
        # Same reasoning as the unremovable link: continuing would hand
        # provisioning the environment that was just found untrustworthy.
        venv = _install_environment(worktree_path, editable_source=repo_root)

        def guarded(path, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("simulated rmtree failure")

        monkeypatch.setattr("shutil.rmtree", guarded)

        with pytest.raises(WorktreeError, match="names .*, which is outside"):
            _setup().apply(worktree_path)

        assert venv.is_dir()


class TestEnforcedHooksAreAnInvariantNotARequest:
    """``hooks_installed`` must be an observed outcome, never the input echoed.

    A worktree that reports enforced guardrails while having none is worse than
    one that fails to be created: the session runs and can push past validation.
    """

    def test_enforced_hooks_that_cannot_be_installed_fail_setup(self, tmp_path):
        wt = make_git_worktree(tmp_path)
        missing_hook = tmp_path / "nonexistent-pre-push"

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(
                enforce_hooks=True,
                pre_push_hook=missing_hook,
            ).apply(wt.worktree_path)

        assert not (wt.hooks_dir / "pre-push").exists()

    def test_enforced_hooks_fail_when_git_config_will_not_take(self, tmp_path):
        """The hook file can land in a directory git never consults.

        Nothing about the filesystem looks wrong in this case — the hooks
        directory is writable, the copy succeeds — so an owner that only checks
        for the file reports a guardrail that will never run.
        """
        wt = make_git_worktree(tmp_path)
        block_worktree_config_writes(wt.gitdir)

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(enforce_hooks=True).apply(wt.worktree_path)

        assert effective_hooks_path(wt.worktree_path) != str(wt.hooks_dir)

    def test_enforced_hooks_fail_when_the_worktree_has_no_git_link(
        self, repo_root, tmp_path
    ):
        # No ``.git`` link means hook installation has nowhere to write; the
        # old owner reported success anyway.
        detached = tmp_path / "detached"
        detached.mkdir()

        with pytest.raises(WorktreeError, match="pre-push hook was installed"):
            WorktreeRuntimeSetup(enforce_hooks=True).apply(detached)


class TestOwnerErrorBoundary:
    """``WorktreeError`` is the owner's whole failure surface."""

    def test_step_failures_are_translated_to_worktree_error(
        self, repo_root, worktree_path
    ):
        # A file where git's ``info/`` directory belongs makes the exclude
        # write raise a bare OSError from inside a composed step.
        (repo_root / ".git" / "worktrees" / "repo-123" / "info").write_text(
            "not a directory"
        )

        with pytest.raises(WorktreeError, match="Worktree runtime setup failed"):
            _setup().apply(worktree_path)


class TestNoVerifyDryRunFlag:
    """The flag gates a hook bypass, so both directions must actually land."""

    def test_flag_is_written_when_preflight_allows_no_verify(
        self, repo_root, worktree_path
    ):
        _setup(allow_no_verify_dry_run_preflight=True).apply(worktree_path)

        assert (worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH).exists()

    def test_stale_flag_is_cleared_when_preflight_disallows_no_verify(
        self, repo_root, worktree_path
    ):
        stale = worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH
        stale.parent.mkdir(parents=True)
        stale.write_text("allow\n")

        _setup(allow_no_verify_dry_run_preflight=False).apply(worktree_path)

        assert not stale.exists()

    def test_unwritable_flag_fails_setup_instead_of_leaving_it_ambiguous(
        self, repo_root, worktree_path
    ):
        # A file where the runtime directory belongs makes every write under it
        # fail; setup must surface that rather than run with an unknown flag state.
        (worktree_path / ".issue-orchestrator").write_text("not a directory")

        with pytest.raises(WorktreeError, match="no-verify dry-run flag"):
            _setup(allow_no_verify_dry_run_preflight=True).apply(
                worktree_path
            )


class TestWorktreeIdentityFailureSemantics:
    """A worktree identity nobody can read back is worse than no worktree."""

    def test_unpersistable_identity_fails_setup(self, repo_root, worktree_path):
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.mkdir()  # a directory where the marker file belongs

        with pytest.raises(WorktreeError, match="worktree identity"):
            _setup().apply(worktree_path)

    def test_empty_identity_marker_is_regenerated(self, repo_root, worktree_path):
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("   \n")

        state = _setup().apply(worktree_path)

        assert state.worktree_id.startswith("wt-")
        assert marker.read_text() == state.worktree_id

    def test_non_utf8_identity_marker_is_regenerated(self, repo_root, worktree_path):
        # Undecodable bytes carry no identity, so replacing them loses nothing.
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"\xff\xfe not an id")

        state = _setup().apply(worktree_path)

        assert state.worktree_id.startswith("wt-")
        assert marker.read_text() == state.worktree_id

    def test_unreadable_identity_marker_fails_without_reissuing_the_identity(
        self, repo_root, worktree_path, monkeypatch
    ):
        # A read that fails is not evidence the identity is gone. Regenerating
        # would tell every job holding "wt-original" that its worktree was
        # replaced underneath it.
        marker = worktree_path / WORKTREE_ID_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("wt-original")
        _break_read_of(monkeypatch, marker, PermissionError("simulated read failure"))

        with pytest.raises(WorktreeError, match="read worktree identity marker"):
            _setup().apply(worktree_path)

        assert marker.read_bytes() == b"wt-original"


class TestInstallClaudeSettingsFailureSemantics:
    """The Stop hook is a completion guardrail; installing it cannot half-fail."""

    def _stop_hook_commands(self, settings_file: Path) -> list[str]:
        settings = json.loads(settings_file.read_text())
        return [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]

    def test_corrupt_settings_are_replaced_with_the_enforced_hook(
        self, tmp_path, caplog
    ):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text("{not json")

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "Replacing unreadable Claude settings" in caplog.text

    def test_wrong_shaped_hooks_are_replaced_rather_than_crashing(
        self, tmp_path, caplog
    ):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"hooks": ["not-an-object"]}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-object 'hooks'" in caplog.text

    def test_null_hooks_are_replaced_rather_than_crashing(self, tmp_path, caplog):
        # Regression: an explicit JSON ``null`` used to be indistinguishable
        # from a missing key, so the merge tried to ``setdefault`` into None
        # and raised AttributeError out of worktree setup.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"model": "opus", "hooks": None}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-object 'hooks'" in caplog.text

    def test_missing_hooks_key_preserves_operator_settings(self, tmp_path):
        # The counterpart to the null case: absent really is absent, and an
        # operator's unrelated settings must survive the merge.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}})
        )

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())
        assert settings["model"] == "opus"
        assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
        assert self._stop_hook_commands(settings_file) == [
            CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"][0]["hooks"][0]["command"]
        ]

    def test_non_utf8_settings_are_replaced(self, tmp_path, caplog):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_bytes(b"\xff\xfe{")

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-UTF-8 Claude settings" in caplog.text

    def test_unreadable_settings_fail_without_discarding_operator_content(
        self, tmp_path, monkeypatch
    ):
        # Failing to read a file says nothing about what is in it. Overwriting
        # on a read error silently deletes operator settings that were fine.
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        original = json.dumps({"model": "opus"})
        settings_file.write_text(original)
        _break_read_of(
            monkeypatch, settings_file, PermissionError("simulated read failure")
        )

        with pytest.raises(WorktreeError, match="read existing Claude settings"):
            install_claude_settings(tmp_path)

        assert settings_file.read_bytes() == original.encode()

    def test_non_list_stop_hooks_are_replaced(self, tmp_path, caplog):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"hooks": {"Stop": "nope"}}))

        with caplog.at_level("WARNING"):
            install_claude_settings(tmp_path)

        assert json.loads(settings_file.read_text()) == CLAUDE_SETTINGS_FOR_AGENTS
        assert "non-list 'hooks.Stop'" in caplog.text

    def test_repeated_installs_do_not_duplicate_the_stop_hook(self, tmp_path):
        install_claude_settings(tmp_path)
        install_claude_settings(tmp_path)

        commands = self._stop_hook_commands(tmp_path / ".claude" / "settings.json")
        assert len(commands) == 1

    def test_existing_operator_settings_survive_the_merge(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]
                    },
                }
            )
        )

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())
        assert settings["model"] == "opus"
        assert "echo mine" in self._stop_hook_commands(settings_file)
        assert len(settings["hooks"]["Stop"]) == 2

    def test_install_does_not_mutate_the_shared_settings_template(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "echo mine"}]}]}})
        )

        install_claude_settings(tmp_path)

        assert len(CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"]) == 1

    def test_unwritable_settings_fail_setup(self, tmp_path):
        (tmp_path / ".claude").write_text("not a directory")

        with pytest.raises(WorktreeError, match="Claude settings"):
            install_claude_settings(tmp_path)
