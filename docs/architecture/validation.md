# Validation System

Validation is a **local lifecycle gate**, not a CI system.

## Model

- Run one quick local command while the coding/review loop is active
- Run one deeper publish command before push/publish
- Cache passing results by worktree + commit SHA + command + contract
- Reuse passing publish records across the callers that run the publish command
- Observe GitHub CI rather than reproducing it locally

### One gate kind, one command, one suite label

A gate is constructed from a **`ValidationGateContract`** — a single value
carrying the kind (`quick` or `publish`), the profile it came from, the command
and the timeout. The suite a record claims and the command it ran are two
projections of that one value, so they cannot disagree.

| Gate | Contract | Suite recorded | Who runs it |
|------|----------|----------------|-------------|
| Agent gate | `quick` | `agent_gate` | `coding-done` / `reviewer-done`, in the session |
| Quick gate | `quick` | `quick_gate` | `SessionController`, after completion |
| Publication gate | `publish` | `publish_gate` | `CompletionProcessor`, before any publish action |
| Manual publish gate | `publish` | `publish_gate` | `prepush-check` run directly, or `scripts/verify-pr.sh` |

The orchestrator-installed worktree `pre-push` hook is deliberately absent from
that table. It invokes `prepush-check --dirty-only`, which enforces the
configured dirty-tree policy and never reaches a validation command. The only
callers that execute the `publish` contract from `prepush-check` are the manual
ones above.

Records live at `.issue-orchestrator/validation/<kind>/<HEAD_SHA>.json`, one
location per **contract**, never per caller. The agent gate and the quick gate
run the same contract and share a location, so a result the agent already
produced is reused. The publish contract has its own location, so a quick run
cannot overwrite the publish gate's record — the only durable evidence of what
the publish contract actually did.

Cache reuse requires the same HEAD **and** command **and** profile **and**
contract. A quick result never satisfies a publish request.

Publication-gate evidence for a run is written to
`<run_dir>/publish-gate/`, separate from the run directory root, whose
`validation-record.json`, `validation-stdout.log` and `validation-stderr.log`
belong to the quick gate that runs later in the same tick.

The gate reports those paths back with its decision (`PublicationGateOutcome.
evidence`), and that is what the completion processor attaches to the run:
`validation_record_path`, `validation_stdout` and `validation_stderr` in the
manifest all name files from the *same* run of the *same* command. Naming
them at the attach site instead let a failing publish record be attached
beside the passing quick gate's logs.

This was issue [#25]: the post-completion gate executed `validation.quick.cmd`
while stamping `suite=publish_gate` onto the record, and the completion
processor's publish-gate seam was never wired in composition — so
`validation.publish.cmd` ran nowhere in the orchestrator path.

## Configuration (YAML)

```yaml
validation:
  quick:
    # Fast feedback for coding-done and local coder/reviewer exchanges.
    # Put cheap repo policy scans here too, for example rejecting new
    # test skips such as assumeTrue/assumeFalse/@Disabled/@Ignore.
    cmd: "make validate-quick"
    timeout_seconds: 300
  publish:
    # Authoritative local PR/pre-push gate.
    cmd: "make validate-pr-raw"
    timeout_seconds: 1800
    dirty_check: tracked

execution:
  isolation:
    mode: "standard"   # or "hardened"
```

`validation.quick.cmd` should be fast enough to run whenever an agent reports
`coding-done completed` and between local coder/reviewer rounds. It should catch
cheap correctness and policy failures early while the coding agent can still
respond immediately.

`validation.publish.cmd` should be the same command your repository treats as
its authoritative pre-push / pre-publish gate.

Keep quick and publish commands configured separately so active review loops can
stay responsive while push/publish actions still run the deeper repository gate.

If the user-facing gate command itself calls the cache-aware pre-push wrapper,
configure `validation.publish.cmd` to the underlying raw command instead. For
example, this repository exposes `make validate-pr` as the cache-aware entry
point and configures `validation.publish.cmd: make validate-pr-raw`, a public
non-recursive target that runs the same required suite without re-entering
`scripts/verify-pr.sh`, so the wrapper can seed and reuse the cache without
re-entering itself.

### Named validation profiles

Different workflow classes can require different validation contracts —
ordinary implementation, documentation-only work, or an authority-changing
workflow that must run extra invariants. `validation.profiles` names those
contracts, and a role selects one explicitly:

```yaml
validation:
  quick: { cmd: "make validate-quick" }        # the `default` profile
  publish: { cmd: "make validate-pr-raw" }     # ...

  profiles:
    foundation:
      quick:
        cmd: "make validate-quick && ./scripts/authority-guard.sh"
      publish:
        cmd: "make validate-foundation"
        dirty_check: all

agents:
  "agent:backend": { validation_profile: default }      # or simply omit
  "agent:foundation": { validation_profile: foundation }
```

Rules that make the choice auditable:

- The top-level `quick`/`publish` pair **is** the profile named `default`. A
  config that never mentions profiles behaves exactly as before; `default` is
  reserved and cannot be redefined under `profiles`.
- Selection is explicit and typed. Nothing is inferred from labels, branch
  names, or working-tree state, and an agent cannot change its own profile.
- An unknown `validation_profile` fails at **config validation**, naming the
  offending role and profile — not at first use.
- The choice is **frozen for the run**: it is resolved once at launch, written
  to the run manifest (`validation_profile`), exported to the session as
  `ISSUE_ORCHESTRATOR_VALIDATION_PROFILE`, and recorded in every validation
  record. Rework rounds, retries, and recovery after an orchestrator restart
  all read it back from that durable run state.
- The profile is part of the **validation cache key**. Two profiles never share
  a cached result, even when they happen to run the same command today.
- A run naming a profile the current config no longer defines fails closed
  rather than silently validating under a different contract.
- **Every** launch path states the contract. `SessionLauncher`, the
  review-exchange coder/reviewer pair, and the interactive debug session all
  freeze the same value through one owner call
  (`Config.validation_profile_for_run`) and export it to the agent. The
  exchange path reads the frozen value back off its `ReviewExchangeRun` rather
  than re-resolving it, so the profile the run records and the profile its
  agent executes cannot diverge. An AST guardrail
  (`run_creation_states_validation_profile` in `tools/ast_guardrails.yml`)
  fails the build if a new run-creation call site omits it, because the
  omission would otherwise be a silent default rather than an error.

This is the downstream implementation of upstream issue
[#7059](https://github.com/issue-orchestrator/issue-orchestrator/issues/7059);
the owning seam is `infra/validation_profiles.py`.

The old single-command shape (`validation.cmd`,
`validation.timeout_seconds`, and `validation.pre_push_dirty_check`) is rejected
at config load time. That keeps upgrades visible instead of silently disabling
both lifecycle gates.

When you install repo guardrails with `issue-orchestrator setup-guardrails`, the
generated `scripts/verify-pr.sh` captures the selected config filename. If you
switch the repo to a different `.issue-orchestrator/config/modes/<mode>/*.yaml`, rerun
`setup-guardrails` so pre-push validation and cache lookups continue to use the
same config.

The canonical **pre-publish** gate is the worktree's effective `pre-push` hook:

- project hook first (`make validate-pr`, `scripts/verify-pr.sh`, etc.)
- orchestrator hook second (Agent-Status trailer + dirty-tree policy)

The orchestrator runs that hook chain before the authenticated push so
push-time policy failures are discovered before publish. The real push still
keeps hooks enabled; when the commit and configured command match, the later
hook pass reuses the cached publish validation record instead of rerunning the
command. CI still mirrors the repo's required PR coverage in a clean
environment.

## Runtime Artifact Ignores

Dirty-tree guards ignore orchestrator-managed runtime files so agents are not
blocked by session state, local tool caches, or Claude Code scheduling locks.
Built-in ignores cover `.issue-orchestrator/` runtime state and
`.claude/scheduled_tasks.lock`.

Target repositories can add repo-local runtime artifacts in
`.issue-orchestrator/runtime-ignore`. See
[`docs/user/configuration.md`](../user/configuration.md#ignore-repo-local-runtime-artifacts)
for the supported format and operator guidance.

## Record Format

Location: `.issue-orchestrator/validation/<kind>/<HEAD_SHA>.json`, where
`<kind>` is `quick` or `publish`

Record fields:
- `schema_version`
- `suite` — names the contract that ran (`quick_gate`, `publish_gate`) or the
  agent-side caller of the quick contract (`agent_gate`). It is not evidence on
  its own; `command` is what the run actually executed, and the two are derived
  from the same contract so they agree by construction.
- `head_sha`
- `passed` + `exit_code`
- `command`
- `started_at` / `ended_at`
- `stdout`/`stderr` paths (optional but recommended)
- `profile` — the named validation profile the run executed (see below);
  records written before profiles existed read back as `default`
