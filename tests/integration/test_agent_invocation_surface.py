"""The parts of an agent invocation that no provider decides (#194).

Everything here shells out — to ``bash``, to the ``agent-done`` wrapper — and
none of it spawns a provider CLI or depends on a model choosing to do
anything. Given the same tree it reaches the same verdict every time, so it
belongs in blocking candidate validation, and this module is deliberately
**not** marked ``live_agent``.

It exists because that marker is module scope. ``pytest.mark.live_agent`` in a
``pytestmark`` list takes the whole file, so a deterministic assertion left
inside a live-agent module leaves blocking validation along with the probes it
was sitting next to — it then runs in no gate at all, since the assurance lane
files a record and cannot fail a candidate. That is what #194's migration did
to these cases while they lived in ``test_claude_execution.py``:

* ``TestShellEscaping`` — the POSIX single-quote pattern
  (``replace("'", "'\\''")``) that the terminal adapters build every command
  with. Its only other home in the tree is
  ``src/issue_orchestrator/domain/models.py``; nothing else asserts it.
* the ``agent-done`` cases — the wrapper resolves, and the completion record
  lands in the directory the agent was ``cd``-ed into rather than wherever the
  session happened to start. Both were additionally gated behind a
  claude-availability ``skipif`` that neither of them needs.

``tests/sandbox_stream_events.py`` + ``tests/unit/test_sandbox_stream_events.py``
is the same split for the sandbox probes' stream parsers, and
``tests/live_agent_reach.py`` is the guardrail that now states the rule
generally instead of one module at a time.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.infra.env import ENV_PREFIX

from .conftest import xdist_timeout

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "src" / "issue_orchestrator" / "scripts"


class TestShellEscaping:
    """Test POSIX single-quote escaping used by terminal adapters.

    The escaping pattern replace("'", "'\\''") is POSIX standard and works
    identically in bash and zsh. Production uses 'zsh -l -c' for login shell
    PATH setup, but the escaping itself is shell-agnostic.
    """

    def test_single_quote_escaping(self):
        """Verify the POSIX single-quote escaping pattern works.

        This tests the escaping used by terminal adapters: replace("'", "'\\''")
        The pattern: end quote, escaped literal quote, start quote.
        """
        # This is the pattern: replace ' with '\''
        original = "echo 'hello world'"
        escaped = original.replace("'", "'\\''")
        wrapped = f"bash -c '{escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(5),
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "hello world" in result.stdout

    def test_complex_quoting_pattern(self):
        """Test the quoting pattern with multiple quoted arguments."""
        command = "echo --flag 'value with spaces' 'another value'"
        escaped = command.replace("'", "'\\''")
        wrapped = f"bash -c 'cd /tmp && {escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(5),
        )

        assert result.returncode == 0
        assert "value with spaces" in result.stdout
        assert "another value" in result.stdout

    def test_nested_quotes_in_prompt(self):
        """Test quoting when the prompt itself contains quotes."""
        # Prompts may contain quotes like: "Fix the 'broken' feature"
        prompt = "This has 'single' and \"double\" quotes"
        # For shell safety, we escape single quotes
        escaped_prompt = prompt.replace("'", "'\\''")
        command = f"echo '{escaped_prompt}'"
        escaped = command.replace("'", "'\\''")
        wrapped = f"bash -c '{escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(5),
        )

        assert result.returncode == 0
        # The output should contain the original text (with quotes resolved)
        assert "single" in result.stdout or "double" in result.stdout


class TestAgentDoneWrapper:
    """The completion wrapper an agent's PATH actually resolves."""

    def test_agent_done_wrapper_resolves_correctly(self):
        """Verify the agent-done wrapper script finds the real completion command.

        This tests the wrapper at scripts/agent-done can locate
        and execute the venv-installed coding-done/reviewer-done.

        The forwarding half used to be guarded by ``.venv/bin/agent-done``
        existing, which is not what the wrapper resolves — there is no
        ``agent-done`` console script, and the wrapper execs the ``coding-done``
        sibling in its own directory. That guard was therefore always false and
        the case degraded to two ``exists()`` assertions. Tracked file, tracked
        sibling: the forward is asserted unconditionally.
        """
        wrapper = SCRIPTS_DIR / "agent-done"

        # Wrapper should exist and be executable
        assert wrapper.exists(), f"Wrapper not found at {wrapper}"
        assert os.access(wrapper, os.X_OK), f"Wrapper not executable: {wrapper}"

        # Run wrapper with --help to verify it forwards correctly
        env = dict(os.environ)
        env["PATH"] = f"{wrapper.parent}:{env.get('PATH', '')}"

        result = subprocess.run(
            ["agent-done", "--help"],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(30),
            env=env,
        )

        assert result.returncode == 0, f"agent-done --help failed: {result.stderr}"
        assert "completed" in result.stdout.lower(), (
            f"Unexpected help output: {result.stdout}"
        )


class TestCompletionRecordLocation:
    """Which directory a completion record lands in is decided by ``cwd``."""

    def test_completion_json_written_to_worktree_not_main_repo(self, tmp_path):
        """CRITICAL: Verify completion.json is written to worktree, not main repo.

        This is the exact bug that caused sessions to silently fail:
        - The agent ran in the main repo instead of the worktree
        - completion.json was written to main repo's .issue-orchestrator/
        - Orchestrator never detected completion (looking in worktree)
        - Reviews never ran, PRs never created

        The fix is that _setup_and_run must cd to working_dir FIRST.
        This test verifies that behavior end-to-end.

        KEY: coding-done uses Path.cwd() to determine where to write.
        Without cd to worktree, cwd is main repo, so completion goes there.
        With cd to worktree, cwd is worktree, so completion goes there.
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").touch()  # Mark as git root
        (main_repo / ".issue-orchestrator").mkdir()

        worktree = tmp_path / "worktree-issue-123"
        worktree.mkdir()
        (worktree / ".git").touch()  # Worktrees have .git file
        worktree_io_dir = worktree / ".issue-orchestrator"
        worktree_io_dir.mkdir()

        # Build environment like orchestrator does
        # NOTE: We do NOT set ISSUE_ORCHESTRATOR_COMPLETION_PATH - coding-done uses cwd!
        env = dict(os.environ)
        env["PATH"] = f"{SCRIPTS_DIR}:{env.get('PATH', '')}"
        # Clear any existing path to test cwd behavior
        env.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)

        # The key test: we cd to worktree, then run the completion command
        # This simulates what _setup_and_run does with the cd fix
        cmd = f'cd "{worktree}" && agent-done completed --implementation "test" --problems "none"'

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(30),
            env=env,
            # Start in main_repo to simulate the bug scenario
            cwd=str(main_repo),
        )

        # Log for debugging
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        print(f"return code: {result.returncode}")

        # Check completion.json is in WORKTREE (not main repo)
        worktree_completion = worktree_io_dir / "completion.json"
        main_completion = main_repo / ".issue-orchestrator" / "completion.json"

        assert worktree_completion.exists(), (
            f"completion.json NOT in worktree! "
            f"Worktree dir: {list(worktree_io_dir.iterdir())}, "
            f"Main repo dir: {list((main_repo / '.issue-orchestrator').iterdir())}"
        )
        assert not main_completion.exists(), (
            "completion.json incorrectly written to main repo instead of worktree!"
        )

        # Verify content
        completion = json.loads(worktree_completion.read_text())
        assert completion["outcome"] == "completed"

    def test_completion_json_written_to_wrong_place_without_cd(self, tmp_path):
        """Verify the BUG: without cd, completion.json goes to wrong place.

        This documents the bug behavior to ensure we don't regress.
        Without the `cd` fix, coding-done would use cwd (main repo).
        """
        # Same setup as above
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        (main_repo / ".git").touch()
        (main_repo / ".issue-orchestrator").mkdir()

        worktree = tmp_path / "worktree-issue-123"
        worktree.mkdir()
        (worktree / ".git").touch()
        (worktree / ".issue-orchestrator").mkdir()

        env = dict(os.environ)
        env["PATH"] = f"{SCRIPTS_DIR}:{env.get('PATH', '')}"
        env.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)

        # NO cd - simulates the bug
        cmd = 'agent-done completed --implementation "test" --problems "none"'

        subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=xdist_timeout(30),
            env=env,
            # cwd is main_repo - this is where the completion command will write
            cwd=str(main_repo),
        )

        # Without cd, completion goes to main_repo (the bug!)
        main_completion = main_repo / ".issue-orchestrator" / "completion.json"
        worktree_completion = worktree / ".issue-orchestrator" / "completion.json"

        # Document the bug: without cd, it goes to wrong place
        assert main_completion.exists(), (
            "BUG TEST: Expected completion.json in main_repo when no cd"
        )
        assert not worktree_completion.exists(), (
            "BUG TEST: Should NOT be in worktree when no cd"
        )
