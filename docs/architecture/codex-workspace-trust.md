# Codex workspace trust — seam measurement (#204)

**This is a measurement record, not a design.** It answers the eight questions
#204 asked and states the disposition they decide. Nothing here changes
permission, sandbox, or trust policy for any provider, and no code in this
repository was modified to produce it. Read it before proposing a fix; the
answers below are what any such proposal has to be consistent with.

## The blocker being measured

A fresh, non-restart Codex Tech Lead launch (#23, blocker evidence
`5385442396`) never began unattended execution. It parked on Codex's
interactive workspace-trust dialog for ~34 minutes at near-zero CPU:

```
codex --ask-for-approval never --model gpt-5.6-sol -c model_reasoning_effort="xhigh" --sandbox workspace-write

You are in /Users/astro3141/io-fork-worktrees/issue-orchestrator-tech-lead-23-b593e287049d
Note: You're in a subdirectory of a Git project. Trusting will apply to the repository root:
      /Users/astro3141/io-fork/issue-orchestrator
Do you trust the contents of this directory?
```

## How this was measured

- **Codex CLI 0.147.0** (Homebrew cask), macOS 25.6.0.
- **Host state was never written.** Every probe ran with `CODEX_HOME` pointed at
  a throwaway directory holding only a copy of `auth.json`. The operator's
  `~/.codex/config.toml` was read once and is byte-identical afterwards (same
  size, same mtime, same 120 `[projects.*]` entries, still no entry for this
  repository).
- **Fixture.** A scratch git repository `repo/` with a subdirectory `repo/sub`,
  two *linked worktrees* created as siblings (`wt-scratch/`, `wt-two/`), a
  symlink `repo-link -> repo`, and a plain non-git directory. This reproduces
  the shape of the failing launch: the agent runs in a disposable worktree that
  is not inside the repository it belongs to.
- Each probe ran the CLI under a PTY (40×120) and was killed after a fixed
  timeout. Whether the trust dialog painted is read directly off the captured
  terminal output.

### Probes

Unless a row says otherwise, the invocation is
`codex --ask-for-approval never --sandbox workspace-write '<prompt>'` and the
scratch `CODEX_HOME` starts with no `config.toml`.

| # | cwd | trust seeded as | variation | Dialog? | Ran unattended? |
|---|-----|-----------------|-----------|---------|-----------------|
| 1 | `wt-scratch` (linked worktree) | — | — | **yes**, names `repo` | no |
| 2 | `wt-scratch` | — | `exec` subcommand | no | **yes** |
| 3 | `wt-scratch` | — | `exec`, fresh home | no | yes — and **wrote** `[projects."repo"] trust_level="trusted"` |
| 4 | `wt-scratch` | `repo` (base config) | — | no | **yes** |
| 5 | `wt-scratch` | `repo` via `-c` override | — | **yes** | no |
| 6 | `wt-scratch` | `repo` via `-p` profile file | `-p iotrust` | no | **yes** |
| 7 | `wt-scratch` | — | `CODEX_NON_INTERACTIVE=1` | **yes** | no |
| 8 | `wt-scratch` | — | `--dangerously-bypass-approvals-and-sandbox` | **yes** | no |
| 9 | `plaindir` (non-git) | — | — | **yes**, no root note | no |
| 10 | `wt-scratch` | `wt-scratch` (the worktree itself) | — | no | **yes** |
| 11 | `wt-two` (2nd worktree, never visited) | `repo` | — | no | **yes** |
| 12 | `wt-scratch` | `lab` (common ancestor) | — | **yes** | no |
| 13 | `repo/sub` | `repo/sub` | — | no | **yes** |
| 14 | `repo-link/sub` (symlinked) | — | — | **yes**, names real `repo` | no |
| 15 | `wt-scratch` | — | `codex doctor --json` | no | n/a — reports no trust state, writes nothing |

## Answers

### 1. Who owns Codex workspace trust, and where is it stored?

The **Codex CLI owns it**, in **`$CODEX_HOME/config.toml`** (`~/.codex/config.toml`
by default), as a documented config table:

```toml
[projects."/absolute/path"]
trust_level = "trusted"
```

It is **host-global user state**, not repository state and not orchestrator
state. Nothing in this repository reads or writes it.

Two facts about the operator's live config are worth recording. It carries 120
such entries but **no entry for this repository or its main checkout** — which
is why the pilot parked. And 98 of those 120 name pytest temp directories from
this repository's own Codex tests
(`test_real_interactive_codex_re0/coder-wt`,
`test_foreign_repo_codex_agent_0/foreign-repo`): residue from before
`tests/codex_home.py` landed (#22/#24, 2026-08-13), which now points
`CODEX_HOME` at a throwaway home for every test that spawns the real CLI. The
leak is closed; the residue documents what an unisolated Codex spawn does to
host trust state, which is exactly the side effect any fix here has to own
deliberately.

There is **no separate trust store**. `~/.codex/.codex-global-state.json` is the
desktop app's UI state (sidebar, window bounds, project assignments) and carries
no trust level.

### 2. What is trust keyed to — the scratch worktree, or the repository root?

**The grant and the check are keyed differently, and that asymmetry is the bug.**

- **The grant** (what the dialog writes when a human answers "Yes") is keyed to
  the **canonical repository root** — the owner of the git *common* directory,
  not the worktree the agent runs in. Running in the sibling linked worktree
  `wt-scratch/`, the dialog names `repo/` and, when satisfied, `repo/` is what
  is persisted. Paths are canonicalised: entering through `repo-link/sub`
  resolves to `repo/`.
- **The check** accepts **either** an entry for the resolved repository root
  **or** an entry for the *exact* current directory. Trusting only
  `wt-scratch/` also suppresses the dialog for a launch in `wt-scratch/`.
- The check is **not** ancestor-based. Trusting `lab/` (the parent of both
  `repo/` and `wt-scratch/`) does not cover either.
- Repository-root trust **does** cover every worktree of that repository: with
  only `repo/` trusted, a launch in the second worktree `wt-two/` proceeds
  without a prompt.
- Outside a git project the dialog still appears, keyed to the directory itself,
  and the "repository root" note is absent.

Consequence: a disposable worktree is never covered on its own — nothing ever
writes a worktree-keyed entry, because the only writer (the dialog) escalates to
the repository root. So **every fresh worktree of an untrusted repository parks**,
and the first "Yes" grants trust across the operator's entire main checkout, not
just the scratch directory the agent was given.

The key a preflight would need is therefore `realpath(dirname(git rev-parse
--git-common-dir))` — *not* `git rev-parse --show-toplevel`, which returns the
worktree.

### 3. Does IO already have a non-interactive trust/bootstrap capability?

**No.** There is no trust concept anywhere in this repository — no port, no
adapter, no config field, no readiness state.

Worse, IO's existing Codex enforcement surface is **downstream of the very
prerequisite that is missing**. `adapters/hooks/codex.py` installs
`.codex/rules/orchestrator.rules` into a project root, and the dialog's own text
states what trust gates: *"Trusting the directory allows project-local config,
hooks, and execpolicies to load."* On an untrusted repository those rules do not
load. `adapters/worktree/_review_command_guard.py` already documents this as a
known gap ("the CLI disables project-local config, hooks and exec policies until
the project is trusted … Closing that needs the trust step"); this measurement
supplies the mechanism behind that sentence.

Two existing seams come close, and neither covers trust:

- `ProviderReadiness` (`ports/provider_readiness.py`).
  `CodexProvider.check_readiness` probes credentials only
  (`codex login status`) and has no notion of workspace prerequisites; its state
  set is `READY / NOT_INSTALLED / AUTH_EXPIRED / UNKNOWN`, and `UNKNOWN` is
  **launchable**. A trust-unmet launch passes this gate today.
- **`execution/agent_runner_providers/codex_config.py` — and this one is the
  find.** IO *already* has a read-only, pre-spawn validator that resolves
  `$CODEX_HOME` (`resolve_codex_home`), parses Codex's documented config layers
  including `$CODEX_HOME/config.toml`, and fails closed with a typed
  `SandboxUnsupportedError` when a loaded layer would silently disable the
  permission profile. It reads the *same file the trust table lives in*, for the
  *same reason* — a host-config prerequisite that would otherwise degrade a
  launch silently. It is simply blind to `projects.*`.

  Its one limit on this path: it runs from `build_codex_sandbox_argv`, so it
  only fires when a `SandboxScope` is supplied. The pilot's argv carries no
  `--strict-config` / `-a never` / `-C`, so that launch had no scope and the
  validator never ran.

### 4. Why are the trust dialog and `--ask-for-approval` / `--sandbox` independent?

They govern different things, and the trust gate is strictly upstream.

- Trust decides whether the **repository's own files may configure Codex** —
  project-local config, hooks, execpolicies. It is a supply-chain / prompt-
  injection boundary.
- `--ask-for-approval` and `--sandbox` decide the **capability ceiling of
  model-generated commands** once a session exists.

Measured precedence, not inferred: the dialog appears with
`--ask-for-approval never --sandbox workspace-write`, and it *also* appears
under `--dangerously-bypass-approvals-and-sandbox` — the strongest permission
bypass the CLI offers. No approval or sandbox value dismisses it. Conversely,
supplying trust through the config layer removes the dialog while those same
flags stay unchanged. The two axes never interact.

Two further negatives worth recording, because they are the obvious guesses:

- **`-c projects."<root>".trust_level="trusted"` does not work.** The trust gate
  reads the *file-based* config layers only; the `-c` CLI override layer is not
  consulted. This is the opposite of `check_for_update_on_startup`, which #205
  suppressed with exactly that mechanism — so the #205 precedent does not
  transfer.
- **`CODEX_NON_INTERACTIVE=1` does not work** on the interactive path.

### 5. Can trust be pre-established or proven through an official Codex surface?

**Pre-established: yes, through two file-based config surfaces.**

1. `$CODEX_HOME/config.toml` — the base user config. Seeding
   `[projects."<repo root>"] trust_level = "trusted"` suppresses the dialog.
2. `$CODEX_HOME/<name>.config.toml` selected with `-p <name>` — the documented
   profile layer ("Layer `$CODEX_HOME/<name>.config.toml` on top of the base
   user config"). A profile file carrying the same table suppresses the dialog
   with the base config left untouched. This is the narrower of the two: it is
   an orchestrator-owned file rather than an edit to the operator's config, and
   the launch that uses it names it in its own argv.

Both still write inside the operator's `$CODEX_HOME`.

**Proven: no.** There is no read-only "is this project trusted?" surface. There
is no `codex config` subcommand, no `--trust` flag, and `codex doctor --json`
(14 KB of checks) reports no trust state at all — it reports `cwd` and never
touches `projects.*`. A preflight would have to read and parse
`$CODEX_HOME/config.toml` itself, i.e. depend on Codex's config *file format*
rather than on a CLI contract.

**A third, unasked-for surface exists and should be recorded: `codex exec`
auto-trusts.** The non-interactive subcommand never prompts, and — measured on a
fresh `CODEX_HOME` with no config file — it *writes*
`[projects."<repo root>"] trust_level = "trusted"` itself, silently, on the way
past. So Codex's own non-interactive surface already materialises exactly the
grant a human would have given, for the same repository root, without asking.

This matters for IO because **no orchestrated Codex launch uses `exec`**.
`CodexProvider` defaults to `execution_mode="interactive"`, no call site in
`src/` passes `execution_mode`, and no shipped mode config sets it — so the
Codex reviewer, goal-pilot, and the pilot's Codex tech_lead all take the
trust-gated TUI path. The one surface that does not park is the one nothing uses.
(Switching to `exec` is *not* a recommendation here: `codex.py` documents that
interactive mode is load-bearing for persistent review-exchange sessions and for
what the terminal-recording pipeline captures. It is recorded as a fact about
where the prompt lives.)

### 6. Or should provider-launch readiness fail closed before spawn?

**Yes, and it is cheaper than it looks — because the seam already exists.**

`codex_config.py` (finding 3) is already a workspace-scoped, read-only,
fail-closed pre-spawn check over `$CODEX_HOME/config.toml`. Teaching it to read
`projects."<root>".trust_level` alongside `sandbox_mode`, and raising a typed
error naming the unmet prerequisite, is an extension of an established pattern
rather than a new one. That is the smallest honest fail-closed answer, and it
does not touch the `ProviderReadiness` port at all.

The `ProviderReadiness` route is the larger one, and it is escalation-shaped:

- it needs a **new state** — the existing four cannot express this. `UNKNOWN` is
  deliberately launchable, and `AUTH_EXPIRED` is credential-specific and feeds
  `ProviderErrorType.AUTH` into the resilience circuit, which a workspace
  prerequisite must not do;
- it needs the probe to become **workspace-scoped**.
  `check_launch_readiness(provider)` takes a provider name only, while trust is
  a function of *provider × repository root* — so the port signature changes.

Either way, note that failing closed **alone leaves P0 blocked**. It converts a
34-minute silent park into a fast, legible failure, which is strictly better,
but the planning lane still does not run unattended until trust is actually
established. Findings 5 and 6 are complementary, not alternatives.

One caution if the `-p` profile route from finding 5 is chosen: the same module
documents that IO "owns its CLI overrides and never emits `--profile`", and
Codex's precedence puts an explicitly selected profile *above* the user layer.
Introducing `-p` changes the precedence reasoning that
`validate_codex_permission_profile_compatibility` is built on, so the two have
to be designed together.

### 7. Does the same invocation proceed unattended once trust is satisfied?

**Yes — observed, not assumed.** With `[projects."<repo root>"] trust_level =
"trusted"` present in `$CODEX_HOME/config.toml` and *every other input held
identical* (same worktree, same `--ask-for-approval never --sandbox
workspace-write`, same prompt), the launch skipped the dialog, booted the TUI,
ran the prompt, and returned model output with no keystroke. The same held for
the `-p` profile-file variant and for a second, never-visited worktree of the
same repository.

So the trust prerequisite is the whole blocker on this path. Nothing else in the
observed argv parks.

### 8. Bootstrap of an existing contract, or a new security policy?

**Bootstrap of an existing contract. Disposition (a): bounded implementation, no
Human-A escalation.**

The reasoning, kept explicit because this question decides the disposition:

- IO never launches Codex against a repository the operator did not configure.
  The trust key is that repository's root. The proposal on the table is
  "materialise trust for the repository this orchestrator was configured to
  manage", never "trust arbitrary repositories".
- Codex's own non-interactive surface already writes that identical grant for
  that identical path without asking (finding 5). Establishing it deliberately
  is not a wider capability than the CLI already exercises on its own.
- The grant loads project-local config/hooks/execpolicies for the managed
  repository — which is what IO's own `.codex/rules` enforcement *needs* in
  order to be more than decorative (finding 3). The trust step moves that guard
  from inert to real.

Three things a bounded implementation must nonetheless decide in the open,
because the measurement shows they are real and not merely mechanical:

1. **Blast radius.** The grant lands on the operator's **main checkout**, not
   the disposable worktree — it covers every worktree of that repository,
   including ones IO did not create. The narrower worktree-keyed entry the check
   *would* accept has no writer today.
2. **Whose file.** Editing the operator's `~/.codex/config.toml` versus an
   IO-owned `-p` profile file are materially different acts on host state. The
   profile form keeps the decision auditable in the recorded argv — the same
   property #205 chose deliberately for its own override.
3. **Test isolation must survive it.** `tests/codex_home.py` exists precisely to
   keep spawned Codex processes out of the operator's `$CODEX_HOME`. A trust
   bootstrap is a *write* to that same store, so it has to be injectable and has
   to resolve its target home the way `CodexHomePolicy` already does — otherwise
   the fix reopens the leak #22/#24 closed.

## Adjacent observation: termination did not reap the parked agent

#23 recorded that `SIGTERM` to the orchestrator owner left the bash wrapper and
the `codex` process orphaned and holding the TTY. Measured here, on purpose,
without fixing it:

**The provider is not the cause.** A Codex parked on the trust dialog, launched
under a `bash -lc` wrapper in its own session on a PTY — the shape IO's launcher
produces — is fully reaped by a single `SIGTERM` to the process group: both the
wrapper and `codex` are gone within seconds, no `SIGKILL` needed. The parked TUI
does not swallow the signal.

So the gap is on IO's side, and the code shows two candidate seams:

- `Orchestrator.request_shutdown(force=False)` **deliberately does not kill
  active sessions** — it logs "waiting for N session(s)". A session that will
  never complete is waited on forever.
- The reap that does exist runs later, at loop exit:
  `_close_external_resources` → `_shutdown_runtime_owners` →
  `SessionRunner.on_orchestrator_shutdown`, which in
  `execution/terminal_subprocess.py` walks the session registry and kills each
  live pid's group. Anything absent from that registry, or any exit that does
  not reach that call, is never signalled. The fallback `_kill_process` path
  also sends `SIGTERM` only, with no `SIGKILL` escalation, unlike
  `AgentSession.kill`.

Localising which of those applied to the #23 run needs the run's own artifacts
and is **not** settled here. It is a separate leaf.

## What this measurement does not establish

- It does not prove the #109 sandbox-probe failures were trust dialogs. #109
  remains a cross-reference only.
- It does not merge with D1 (a Claude-specific trust-prompt race on a different
  provider surface). The one adjacent thing IO *does* have — the
  `execution.session_interactions.enabled` setting, off by default, whose own
  notes cite "Claude's initial trust confirmation" — is a runner-managed
  keystroke reply to a live session, not a prerequisite contract, and
  answering the Codex dialog that way was explicitly out of scope for this leaf.
- All findings are for Codex CLI **0.147.0** on macOS. Trust storage, the
  `-c` / file-layer precedence split, and `codex exec`'s auto-trust write are
  upstream behaviours that can change between releases; anything built on them
  needs its own version-sensitive test, not a comment.
- The `-p` profile-file route was verified to suppress the dialog. It was *not*
  verified against a base config that carries a conflicting `projects.*` entry.

## Related

- [Guardrails & Safety Model](../design/guardrails.md) — enforcement layers and
  trust boundaries
- [Hooks](hooks.md) — multi-layer hook enforcement
- `src/issue_orchestrator/execution/agent_runner_providers/codex.py` — Codex argv
  construction, and the #205 `check_for_update_on_startup` precedent
- `src/issue_orchestrator/adapters/worktree/_review_command_guard.py` — the
  "known gap" this measurement explains
- `src/issue_orchestrator/execution/agent_runner_providers/codex_config.py` —
  the existing fail-closed `$CODEX_HOME/config.toml` preflight
- `src/issue_orchestrator/ports/provider_readiness.py` — the larger seam a
  fail-closed preflight could extend instead
- `tests/codex_home.py` — the `CODEX_HOME` isolation any trust write must honour
