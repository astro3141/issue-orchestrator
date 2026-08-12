# Self-hosting runbook — astro3141/issue-orchestrator fork

Operating notes for running Issue-Orchestrator against this fork. Everything
here was measured on 2026-08-11, not assumed, and re-checked against the code
on 2026-08-12 after the fork's issue #6 landed and the runtime was promoted.
Where a number appears, it came from an actual run on this machine.

## Where these files live, and why not under `.issue-orchestrator/`

`.issue-orchestrator/` is a guarded runtime-artifact root. `runtime_artifacts.py`
allows exactly three tracked things under it — `allow-no-verify-dry-run`,
`runtime-ignore`, and `config/**` — and rejects everything else on an agent
branch by default, so the guard fails safe as the runtime surface grows.

This runbook first lived there. It blocked issue #6's publish with
`Forbidden artifacts: .issue-orchestrator/SELFHOST_RUNBOOK.md` the moment an
agent touched it. The guard was right; the file was in the wrong place. Keep
self-hosting documentation and tooling under `docs/selfhosting/`.

## The boundary

A **trusted pinned runtime** — a separate checkout, held at a fixed commit —
orchestrates this repository as a **candidate**. The candidate's own IO source
— modified or not — never runs itself.

| | Runtime | Commit |
|---|---|---|
| **R1**, current | `~/io-runtime-r1/issue-orchestrator` | `81c11ae1` |
| R0, predecessor | `~/io-tools/issue-orchestrator` | `74575869` |

R1 was promoted on 2026-08-12, after #6's PR passed independent review and the
publish gate and the canary of fork issue #8 exercised it end to end.
`~/io-runtime-r1/TRUSTED_RUNTIME.md` — outside the repository, next to the
runtime it describes — records what that canary established; it is not
restated here.

**The pin is `81c11ae1`, not current `main`, deliberately.** What makes a
runtime trusted is the evidence attached to a specific artifact, and R1 is the
build that actually ran the canary. Rebuilding from current `main` would
promote a commit no canary exercised, on the strength of a run some other
artifact performed, which breaks the bootstrap chain that the pin exists to
keep. So product `main` running ahead of the runtime (it was at `6d478b23`
when this was written) is expected and is not drift, however far it advances.
Moving the pin is its own decision, taken the same way: review, publish gate,
canary, then promote.

R0 stays on disk untouched, so the chain remains auditable and a rollback needs
no rebuild.

## Promotion: ship the runtime you verified

The trusted runtime is pinned to a **specific commit that was actually
exercised**, not to whatever `main` holds.

When a runtime candidate passes its canary, promote **that build**. Rebuilding
from a newer `main` — even one that merely adds the canary's own PR — promotes
something no canary ever ran, and breaks the chain of evidence that the
promotion rests on. Product `main` moving ahead of the runtime pin is expected
and is not drift.

**The predecessor stays on disk.** R0 is not deleted after promotion: it keeps
the chain auditable and makes rollback a path change rather than a rebuild. A
rebuilt "equivalent" of a predecessor is not the predecessor.

Current pins are in the table under "The boundary".

## Environment requirements

Five things live outside the repository. Miss any one and startup or the gate
fails, usually in a way that does not name the real cause.

| Requirement | Why | Install |
|---|---|---|
| GNU Make 4.x as `gmake` | macOS ships GNU Make 3.81, which rejects `--output-sync=target`. `Makefile:6` prefers `gmake` and silently falls back to `make`. | `brew install make` (installs as `gmake`) |
| Playwright Chromium **at `~/.cache/ms-playwright`** | `Makefile:77` pins `PLAYWRIGHT_BROWSERS_PATH ?= $(HOME)/.cache/ms-playwright`. Installing to the default macOS cache leaves the gate looking somewhere else. | `PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" .venv/bin/playwright install chromium` |
| `packages/vscode/node_modules` | `test-vscode` self-skips only when `GITHUB_ACTIONS` is set. Locally it runs and fails closed without deps. | `make install-vscode-extensions` |
| `LC_MESSAGES=C` on the runtime | One test asserts on English git stderr. Under a non-English locale it fails on an unmodified checkout. | Export when starting the orchestrator |
| `NO_COLOR=1` **and** `TERM=dumb` | `test_ai_gate_cli.py` matches plain substrings against Rich-formatted output. `NO_COLOR` alone is not enough: it drops colour but leaves bold/dim sequences mid-string and the assertions still fail. | Export both when starting the orchestrator, and when pushing |

These last two share one root: tests assert on substrings of human-facing
output, and that output changes with the environment. Neither names its own
cause — both surface as an assertion diff that looks unrelated to the change
under test, on an unmodified checkout.

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

Full precedence for the validation loader, highest first:

1. `ISSUE_ORCHESTRATOR_CONFIG_PATH` / `ORCHESTRATOR_CONFIG_PATH` — an explicit file
2. `ISSUE_ORCHESTRATOR_CONFIG_NAME` / `ORCHESTRATOR_CONFIG_NAME` — a name resolved in the config dir
3. the repo-local `default.yaml` search

Passing `--config` and exporting `ISSUE_ORCHESTRATOR_CONFIG_NAME` together is
not redundant: the first steers startup, the second steers validation, and a run
where they disagree validates under a contract the orchestrator did not start
with.

## Starting

```sh
cd ~/io-fork/issue-orchestrator
LC_MESSAGES=C NO_COLOR=1 TERM=dumb ISSUE_ORCHESTRATOR_CONFIG_NAME=selfhost.yaml \
  ~/io-runtime-r1/issue-orchestrator/.venv/bin/issue-orchestrator \
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

**Which validation contract a run executed is on disk, not only in the
timeline.** Every validation run writes one record per HEAD SHA to
`<worktree>/.issue-orchestrator/validation/<sha>.json`
(`control/validation_record_cache.py`), and that record carries a `profile`
field naming the validation profile the run executed, alongside `suite`
(`agent_gate` or `publish_gate`), `command`, `passed` and `exit_code`. Read it
when you need to prove *which* gate a SHA actually satisfied: reuse is
profile-aware, so a cached record only counts for a later gate when the SHA,
the command and the profile all match, and a record written before profiles
existed reads back as `default`. The dashboard's **Open Validation Record**
action opens the run-scoped copy in the session run directory, which carries
the same fields.

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

### The gate must read the commit it reports on

A gate result is evidence about a commit only if the tree it read *is* that
commit. Self-hosting is the one case where that can quietly stop being true:
`src/issue_orchestrator/entrypoints/cli_tools/` is orchestrator runtime in
every other repository, but product source here. Worktree setup used to plant
its own copies there and mark them `--skip-worktree`, so `git status` looked
clean while pytest and static analysis read files the branch did not contain
(fork issue #6; hit on #3 and #5).

Setup now refuses to plant over a path the target repository tracks, and undoes
any overlay an older run left behind. The same change corrected the other
direction: an *untracked* file under that directory is no longer assumed to be
a planting, because here it is a CLI tool the candidate is adding, and hiding
it let a branch be pushed without source the validated tree contained.

Both directions ask one question of the index — does the target repository
track `src/issue_orchestrator/entrypoints/cli_tools/`? — and every surface
takes the answer from the same seam (`execution/git_planted_paths.py`, or the
worktree adapter's `repo_owns_cli_tools` for the planting step itself). The
practical consequence for reading a managed worktree of *this* repository:

- **`git status` is trustworthy for those paths.** Nothing is planted there,
  no `--skip-worktree` bit is applied there, and no dirty guard filters what
  git reports there — nor does `.issue-orchestrator/runtime-ignore`, the one
  repo-local hide list that could name such a path, and which this repository
  does not have. What it shows is the candidate's, and what it does not show
  is not there.
- `coding-done`, `prepush-check` and the orchestrator's publish gate read that
  same status, so a clean tree at completion means the branch carries every
  CLI tool the gate graded.

Confirm a managed worktree before trusting its gate:

```sh
git -C <managed-worktree> ls-files -v -- src/issue_orchestrator/entrypoints/cli_tools
# every line must start with H; an S means the tree is not the commit
```

`.claude/settings.json` is still `--skip-worktree` by design — the Stop hook is
runtime configuration, and nothing in the gate reads it as source. It and
`.issue-orchestrator/session-latest.json` are the whole of
`WORKTREE_TRACKED_RUNTIME_PATHS`, and the second is untracked in this
repository, so `.claude/settings.json` is the only `S` a managed worktree here
should show:

```sh
git -C <managed-worktree> ls-files -v | grep '^S'
```

A cheaper check is available on every run, and it costs nothing to read:
`coding-done` logs `CODING_DONE_SOURCE_ID` on the startup diagnostic of each
invocation, so the log says which copy of that module executed. Its unit tests
carry `planted` in their names because the quick profile selects that keyword —
a canary the gate deselects proves nothing — and they fail on a copy whose id
is absent, stale or different. Fork issue #8 armed and ran this against a
candidate that deliberately modified `cli_tools/coding_done.py`; the result is
in `~/io-runtime-r1/TRUSTED_RUNTIME.md`.

#### When setup refuses a worktree over a hidden CLI tool

Only a worktree an older run set up can reach this. Nothing applies
`--skip-worktree` to those paths any more, so the hidden state this repairs is
always a leftover, never something the current runtime created — and once
repaired it does not come back.

Undoing the overlay only reverts files the orchestrator can prove it planted
(byte-identical to the copy in its own package). `--skip-worktree` never made
those paths read-only, it only hid writes to them, so a session that died
mid-edit can leave real work behind that `git status` called clean. Setup
preserves anything it cannot explain, clears the bit so git reports it, and
then fails rather than running an agent on it.

Reuse asks this **before** it rebases or hard-resets, so the run that reports
the problem is also a run that has not touched the worktree: uncommitted work
elsewhere in the tree is still there too. The message names the files:

```
Repo-owned CLI tool(s) in <worktree> diverge from the index and no orchestrator
copy explains them: <paths>
```

Decide per file — the content is on disk and now visible:

```sh
git -C <managed-worktree> diff -- <path>   # keep it: commit on the branch
git -C <managed-worktree> checkout -- <path>   # discard it
```

Either way the path is no longer hidden, so the next setup on that worktree
proceeds normally; the failure does not repeat on its own.

Deciding is the whole of the deadline. From the escalation onward the content
is ordinary uncommitted work: the bit is cleared, so the *next* run finds
nothing hidden, passes the same check, and reaches the `git reset --hard` and
`git clean -fd` that reuse runs before setup ("we prioritize success over
preserving uncommitted work") — which discards it. That is still the
improvement over the old behaviour — the loss is now reported by `git status`
beforehand and logged as a discard when it happens, instead of being invisible
in both directions — but it is a deadline, not a reprieve.

### …and it must read the code that changed

Same rule, one level up. `validation.quick` in
`.issue-orchestrator/config/selfhost.yaml` is a `-k` filter, so a `passed=true`
recorded by a run that deselected every test the branch added is evidence about
somebody else's code.

`-k` matches the names in a test's node id — file name, class, function — and
**not** the directory segments above it, so selection is per test, not per
file. The profile's `cli_tools` keyword takes all of
`test_worktree_cli_tools_ownership.py` because the string is in that file's
name; in `tests/unit/test_agent_done.py` it takes only the individual tests
whose own names carry it. Issue #8's first canary lived in that second module,
under class and function names matching none of the profile's keywords: the
gate ran most of that file and never the canary, and recorded green. A module
being selected says nothing about your test in it. Check what a profile
actually executed before believing it:

```sh
# what the recorded gate actually executed
grep -c 'PASSED\|passed' <session>/validation/validation-stdout.log
.venv/bin/python -m pytest tests/unit tests/integration -q -p no:cacheprovider \
  --collect-only -k '<the profile expression>' | grep <your test name>
```

When an issue's change area falls outside the current keywords, the cheaper
move is often to name the new tests for a keyword the profile already selects
— that arms them with no config churn at all. Otherwise add the keywords to the
tracked config in the same commit — but **that edit does not take effect for the
branch that makes it.** `load_runtime_validation_config` gives
`ISSUE_ORCHESTRATOR_CONFIG_PATH` precedence over any repo-local search, and the
launcher exports it as the *main checkout's* config file:

```sh
echo "$ISSUE_ORCHESTRATOR_CONFIG_PATH"
# /Users/<you>/io-fork/issue-orchestrator/.issue-orchestrator/config/selfhost.yaml
```

So the live gate is operator-owned and outside every agent worktree. A branch's
own edit governs runs only after it lands there. To make a round's record carry
the branch's own gate, point one run at the worktree copy:

```sh
ISSUE_ORCHESTRATOR_CONFIG_PATH=$PWD/.issue-orchestrator/config/selfhost.yaml \
ORCHESTRATOR_CONFIG_PATH=$PWD/.issue-orchestrator/config/selfhost.yaml \
  coding-done completed --implementation "…" --problems "…"
```

Only ever to *widen* the selection — an override that narrows it is the agent
grading its own paper. Publish (`make validate-pr-raw`) running the whole suite
later is a backstop against merging something broken, not a substitute for
validating the change in review.

### tests/e2e is a separate live contract

It requires `gh auth` and `E2E_TEST_REPO`. With that variable unset,
`tests/e2e/fixtures/github_utils.py:140` falls back to the current git
repository — so running it writes real issues into this fork. Two
`[E2E-CLAIM] Coordination test issue` items (#1, #2) were created that way
before this was understood. Upstream CI does not run e2e either. If live E2E is
needed, point `E2E_TEST_REPO` at a throwaway repository.

## Changing the quick profile mid-issue: widen only

The quick gate selects by keyword. During #6 the selection was widened, and the
direction matters more than the specific keywords.

**Widening is safe. Narrowing is not.** A `passed=true` from a profile that
deselected the tests covering the change is worse than no result at all: it
carries the authority of a gate while having examined nothing relevant. If a
rework round shows the profile misses the change area, add keywords — never
remove them to make a run go green.

This is also why the canary tests carry `planted` in their names. The keyword
list selects them; a canary the gate deselects proves nothing.

## Hidden work has a preservation deadline

When setup repairs a worktree — clearing `--skip-worktree` bits, restoring files
from the index, dropping stale `info/exclude` entries — it is touching files an
agent may have edited while they were hidden from `git status`.

**Repair must therefore establish ownership before doing anything destructive,
not partway through.** #6's first rework round was spent exactly here: the fix
preserved hidden work but ran the check inside the setup step rather than as a
precondition of reuse, leaving a window where the repair could discard an
agent's edits. The ordering is the requirement, not an implementation detail.

The general rule: any step that can destroy uncommitted agent work must be
preceded — not accompanied — by the check that decides whether it may run.

## test-web is timing-sensitive

Playwright dashboard tests can time out under load without anything being wrong
with the change. Observed: `Locator.select_option` on an `e2e-run-row` exceeding
its 30s budget during a push whose commit touched only documentation, then the
same target passing 56/56 in an isolated rerun 74s later.

Re-run `gmake test-web` alone before treating a lone failure there as a
regression. If it reproduces, it is a real finding; if it does not, note the
flake rather than silently retrying until green.

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
