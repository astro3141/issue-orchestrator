# Self-hosting runbook — astro3141/issue-orchestrator fork

Operating notes for running Issue-Orchestrator against this fork. Everything
here was measured on 2026-08-11, not assumed. Where a number appears, it came
from an actual run on this machine.

## The boundary

The **trusted pinned runtime** at `~/io-tools/issue-orchestrator` orchestrates
this repository as a **candidate**. The candidate's own IO source — modified or
not — never runs itself.

Promoting this fork to runtime is a separate decision, taken only after a PR
passes independent review and the publish gate, and followed by its own
smoke/self-host test. Until then `~/io-tools` is not touched.

## Environment requirements

Four things live outside the repository. Miss any one and startup or the gate
fails, usually in a way that does not name the real cause.

| Requirement | Why | Install |
|---|---|---|
| GNU Make 4.x as `gmake` | macOS ships GNU Make 3.81, which rejects `--output-sync=target`. `Makefile:6` prefers `gmake` and silently falls back to `make`. | `brew install make` (installs as `gmake`) |
| Playwright Chromium **at `~/.cache/ms-playwright`** | `Makefile:77` pins `PLAYWRIGHT_BROWSERS_PATH ?= $(HOME)/.cache/ms-playwright`. Installing to the default macOS cache leaves the gate looking somewhere else. | `PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" .venv/bin/playwright install chromium` |
| `packages/vscode/node_modules` | `test-vscode` self-skips only when `GITHUB_ACTIONS` is set. Locally it runs and fails closed without deps. | `make install-vscode-extensions` |
| `LC_MESSAGES=C` on the runtime | One test asserts on English git stderr. Under a non-English locale it fails on an unmodified checkout. | Export when starting the orchestrator |

`LC_MESSAGES=C` is sufficient — `LC_ALL` is not needed. Verified: the test fails
with the variable unset and passes with it, and git switches to English.

**Do not put the locale workaround in tracked files or in the config.** It is a
property of the host, not of the project. It also would not work there: pre-push
goes through `scripts/verify-pr.sh`, which selects `main.yaml`, so a locale
setting in `selfhost.yaml` never reaches the gate.

## Config selection

Use the named config `.issue-orchestrator/config/selfhost.yaml`. Never create
`default.yaml` and never edit `main.yaml`.

Creating `default.yaml` makes `setup-guardrails` rewrite `scripts/verify-pr.sh`
— a tracked upstream file that selects `main.yaml` by name — and the repository's
own `test_checked_in_verify_pr_matches_portable_generated_output` catches it.

Selection is not uniform across the codebase, which is worth knowing before
debugging a "config not found":

- `ISSUE_ORCHESTRATOR_CONFIG_NAME` reaches only the **validation** loader
  (`load_runtime_validation_config`).
- The orchestrator's own startup discovery uses `find_config_file`, which looks
  for `DEFAULT_CONFIG_NAME` (`default.yaml`) and ignores that variable.
- So `start` needs **`--config <path>`**. The CLI takes a path, not a name.

## Starting

```sh
cd ~/io-fork/issue-orchestrator
LC_MESSAGES=C ISSUE_ORCHESTRATOR_CONFIG_NAME=selfhost.yaml \
  ~/io-tools/issue-orchestrator/.venv/bin/issue-orchestrator \
  --config .issue-orchestrator/config/selfhost.yaml \
  start --issue N --ui-mode web --port 8080 --debug
```

Start with the dashboard, not `--no-dashboard`. Long Actor/Reviewer runs are
otherwise observable only by someone tailing logs and relaying them, and a
stalled session looks identical to a working one from the outside.

`--port` was not honoured in practice: the dashboard bound to an arbitrary port
and the log printed only the control API URL. Find the real one with:

```sh
lsof -nP -iTCP -sTCP:LISTEN | grep -i python
```

The control API is always up regardless of UI mode; the dashboard is the *other*
python port.

## Dashboard ports change on every restart

`--port` was not honoured: the dashboard bound to an arbitrary high port both
times. Each restart picks new ones, and the previous browser tab becomes a dead
page that still looks alive.

Two ports are opened, adjacent, by the orchestrator process:

```sh
PID=$(pgrep -f "issue-orchestrator .*--issue N" | head -1)
lsof -nP -a -p "$PID" -iTCP -sTCP:LISTEN     # note the -a: without it lsof ORs the filters
```

The lower one serves **Sign in / Control Center**; the higher one is the
**dashboard**. Confirm by title:

```sh
curl -s http://127.0.0.1:PORT/ | grep -o '<title>[^<]*</title>'
```

Log in at the Sign-in port first, using the admin token at
`~/.issue-orchestrator/api-token`, then open the dashboard port.

**"Engine restarting... waiting for service to recover" usually means the
session cookie is gone, not that the engine died.** Cookies are per-port, so a
restart invalidates the old login. Check the engine directly before believing
the message:

```sh
curl -s -H "Authorization: Bearer $(cat ~/.issue-orchestrator/api-token)" \
  http://127.0.0.1:<signin-port>/api/status
```

A healthy engine returns `active_sessions`, the session name, agent type,
runtime minutes and branch. `[SSE] No subscribers` in the orchestrator log is
the same symptom from the server side: nobody is attached to the event stream.

## Reading the dashboard

The timeline is available at three granularities, and they are not equivalent.
Pick deliberately — the one you want when work disappears is **ops**.

| View | Shows | Use it for |
|---|---|---|
| **ops** | `Claim acquired`, **`Worktree reset to main`**, `Coding agent started`, `Apply step applied` | Diagnosing lost work and lifecycle side effects |
| story | `Worktree reset to main`, `Coding agent started`, **`Coding Recording`** | A readable summary, and the only practical way to watch the agent |
| raw events | `claim.acquired`, `labels changed`, `started` | Correlating with the orchestrator log |

**Use `Coding Recording` in the story view to watch the agent's TUI.** It replays
`sessions/<run>/terminal-recording.jsonl`, which is otherwise only readable by
decoding base64 out of the JSONL by hand — enough to spot a stall, but not
enough to follow what the agent is doing. A blocked session (the trust dialog)
and a working one are immediately distinguishable here, where they look
identical from the orchestrator log.

Two things about the dashboard have already cost work here.

**Control Center's close button terminates the orchestrator process**, and the
running Actor session dies with it. It is easy to hit while exploring. The
process exits 0 with `Cancel 0 running task(s), timeout graceful shutdown
exceeded` — it looks like a clean shutdown, not an accident.

**The session prompt shown in the dashboard is a pointer, not the contract.** It
is IO's built-in template (`domain/models.py:875`), rendered as *"Work on issue
#N: <title>. Follow the instructions in .prompts/backend.md. When done, use
coding-done to report completion."* The actual behavioural contract lives in the
issue body and in `.prompts/backend.md`. Reading the dashboard alone will not
tell you what the Actor was asked to do.

## Restarting destroys uncommitted work

A new run **resets the worktree to the seed ref**. The ops timeline records this
as `Worktree reset to main`, a few seconds before the coding agent starts.

This is not theoretical: a Run-2 session had produced 83 lines across four files
plus a new module, uncommitted, and the Run-3 restart deleted all of it. The
files had been copied out beforehand, so nothing was lost — but only by luck of
timing.

**Before stopping or restarting a run, back up uncommitted work**, without
touching the repository:

```sh
W=~/io-fork-worktrees/issue-orchestrator-N
git -C $W diff > /tmp/actor-work.patch
git -C $W status --porcelain | grep '^??' | awk '{print $2}' | \
  while read f; do mkdir -p "/tmp/actor-untracked/$(dirname $f)"; cp "$W/$f" "/tmp/actor-untracked/$f"; done
```

Restoring a backup into a live worktree is usually the wrong move: the Actor
then finds code it did not write, and the authorship trail gets muddled. Prefer
letting the agent redo the work and keep the backup for comparison.

## Before the first run on a new machine

**Preseed Claude Code trust for every worktree path.** This is the failure that
costs the most time because it does not look like a failure: the orchestrator
reports `active=1`, the loop keeps ticking, and the Actor sits at
`Security guide / 1. Yes, I trust the ...` producing nothing. One run burned
55 minutes this way with zero file changes.

```python
import json
p = "/Users/astro3141/.claude.json"
d = json.load(open(p)); prj = d.setdefault("projects", {})
paths = ["/Users/astro3141/io-fork",
         "/Users/astro3141/io-fork/issue-orchestrator",
         "/Users/astro3141/io-fork-worktrees"]
paths += [f"/Users/astro3141/io-fork-worktrees/issue-orchestrator-{n}" for n in range(1, 41)]
for path in paths:
    prj.setdefault(path, {})["hasTrustDialogAccepted"] = True
json.dump(d, open(p, "w"), indent=2)
```

Trust is keyed by **exact path**, so the worktree directories must be listed,
not just the repository. IO names them `issue-orchestrator-<issue number>`.

**Enable issues on the fork.** GitHub disables them on forks by default, and IO
is issue-driven:

```sh
gh api -X PATCH repos/astro3141/issue-orchestrator -f has_issues=true
gh issue list --repo astro3141/issue-orchestrator   # should not error
```

**Commit prompts to the seed ref.** Worktrees are seeded from `origin/main`, and
startup refuses when `.prompts/*` is absent from that ref.

## Validation gates

| Gate | Command | Measured |
|---|---|---|
| quick | focused pytest on the change area + `make typecheck` | — |
| publish | `make validate-pr-raw` | **490s, all green** on the clean pinned baseline |

`make validate-pr-raw` is upstream's own gate — `main.yaml` uses it — and it
already runs typecheck, lint-arch, lint-complexity, test-unit,
test-simulated-core, test-integration-core-local, test-simulated-agent,
test-integration-agent and test-vscode. Adding any of those separately just
duplicates them.

`make test` is **not** the project's gate. It carries `-x`, and on a fresh fork
it aborts in ~128s inside `tests/e2e`.

### tests/e2e is a separate live contract

It requires `gh auth` and `E2E_TEST_REPO`. With that variable unset,
`tests/e2e/fixtures/github_utils.py:140` falls back to the current git
repository — so running it writes real issues into this fork. Two
`[E2E-CLAIM] Coordination test issue` items (#1, #2) were created that way
before this was understood. Upstream CI does not run e2e either. If live E2E is
needed, point `E2E_TEST_REPO` at a throwaway repository.

## Baseline health

The clean pinned baseline is **green**. Nine apparent failures were all
environmental:

- 7 × `test_control_center_lifecycle.py` — needs `GITHUB_TOKEN`; fails at setup
  in 0.11s, so timing and permissions are not involved
- 1 × `test_fast_forward_checkout_failure_preserves_git_context` — locale
- 1 × `test_checked_in_verify_pr_matches_portable_generated_output` — caused by
  onboarding rewriting a tracked file (see Config selection)

Re-run a suspicious failure in isolation before believing it. Several
"regressions" here reproduced only under one specific invocation.

## What the events mean, and which ones prove progress

The timeline lives in `.issue-orchestrator/state/timeline.sqlite`:

```sh
sqlite3 -separator '  ' .issue-orchestrator/state/timeline.sqlite \
  "select timestamp, event from timeline_events order by timestamp;"
```

| Event | Meaning | Proves work is happening? |
|---|---|---|
| `claim.acquired` | This orchestrator took the issue. One per orchestrator start. | No |
| `claim.renewed` | Lease heartbeat, **every ~5 minutes** (measured: 5m01s–5m11s). Stops when the process dies, letting another instance take over a stale claim. | **No** |
| `worktree.reset` | Worktree returned to the seed ref — uncommitted work is gone | — |
| `agent.coding_started` | Session launched | No |
| `apply.step_applied` | The agent applied a step | Weakly |
| `issue.labels_changed` | Label transition | No |
| `session.no_completion_record` | A session ended without reporting completion | Signals a lost run |

**Claim renewal proves liveness, never progress.** One session here renewed its
claim twelve times over 55 minutes while producing nothing at all — it was
blocked on the Claude Code trust dialog the whole time. The orchestrator
reported `active=1` and the timeline looked healthy.

The signals that actually mean work: growth of
`sessions/<run>/terminal-recording.jsonl`, and `git status --porcelain` in the
worktree.

`claim.acquired` appearing three times in one timeline means the orchestrator
was started three times — useful for reconstructing what happened after a
confusing session.

## Diagnosing a stalled Actor

`active=1` in the orchestrator log means a session is registered, not that work
is happening. Check, in order:

1. Worktree changes — `git -C ~/io-fork-worktrees/issue-orchestrator-N status --porcelain`
2. Session record — `.issue-orchestrator/session-latest.json` in the worktree
   gives `run_dir` and `log_path`
3. **Terminal recording** — `sessions/<run>/terminal-recording.jsonl`. Its
   modification time is the honest signal: if it stopped minutes ago while the
   process lives, the agent is blocked on something interactive. Payloads are
   base64 under `data_b64`; decode them to see the actual screen.

A recording of ~2KB that has not grown is a session that never got started.

## Recovering from a stalled run

1. Kill the agent process, then the orchestrator
2. Remove the blocking-class label (`in-progress`) from the issue
3. Check for leftover claim refs: `git ls-remote origin "refs/issue-orchestrator/claims/*"`
4. Remove the worktree so the next run seeds fresh:
   `git worktree remove --force <path>` then `git worktree prune`
5. Fix the actual cause before restarting

## Known upstream defects observed here

- **Locale-dependent assertion** — `test_fast_forward_checkout_failure_preserves_git_context`
  asserts on English git stderr and fails under a localized environment on an
  unmodified checkout. Worth reporting upstream; does not block work here.
- **Global validation configuration** — one `validation.quick` / `validation.publish`
  per repository, so agent roles and workflow classes cannot select different
  safety contracts. Filed upstream as
  `issue-orchestrator/issue-orchestrator#7059`; fork issue #3 implements it.
