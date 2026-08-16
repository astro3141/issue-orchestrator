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

### The verdict outlives the run directory

Every path named above lives inside the coder worktree, so all of it is gone
once the worktree is reaped. The publication gate therefore also files a
**verdict receipt** on `Attempt(issue, HEAD_SHA)` — the record already keyed by
exactly that pair, whose sidecar lives in the primary checkout under
`.issue-orchestrator/attempts` and survives both worktree removal and an
orchestrator restart.

The receipt is not a copy of the record. It carries only what a later reader
needs in order to decide whether *this exact candidate* passed *the
publication contract*: the suite, the exact `head_sha`, the verdict
(`passed` / `failed` / `timed_out`), and the `command` + `profile` that
identify the contract that actually executed — the same three values cache
reuse compares. `Attempt.publication_validation_passed` asks all of it at
once.

Three states stay distinguishable after cleanup, which is what issue [#85]
existed to restore: no receipt means the gate never ran, a receipt means it
ran and says what it decided, and a receipt that does not parse raises rather
than reading as either. Two runs write no receipt, and both are honestly "the
gate never ran": one whose profile configures no publish command, and one the
gate refused before executing because it could not determine HEAD — the latter
has no candidate commit to file a verdict under in the first place.

Which entry points can file a receipt is a question about *identity*, not
about the gate: a receipt lands on `Attempt(issue, A)` only when the caller
holds the candidate's canonical issue key.

- The live completion path carries the session's own key, the one its claim
  and every other attempt-scoped record already use.
- The **republish** path carries that same key on its durable
  `PublishRetryLocators`, so a retried publish's verdict lands on the same
  `Attempt(issue, A)` as the first attempt's evidence. Locators persisted
  before [#85] have no key on them and republish receipt-less, as they did
  before.
- The **manual-reprocess** route holds only an issue *number* from a URL path.
  It runs the gate, records no receipt, and logs that it did not.

No path writes a receipt under a *derived* identity. Reversing a work-item
number back into a key is the drift [#40] removed, and a receipt filed under a
key nothing else uses is worse than no receipt at all.

## Worktree readiness is a precondition of a meaningful verdict

Every gate above runs *inside a worktree*. A worktree that lacks the
repository's runtime prerequisites — its virtualenv, its node modules, its
browser binaries — still runs the command, still fails, and the failure is
recorded against the candidate commit. An environment gap then reads as
"this change failed validation".

Two collaborators decide whether a worktree can run anything:

| Step | Owner | What it guarantees |
|------|-------|--------------------|
| Create/reuse the worktree | `WorktreeManager` (adapter) | The checkout, its branch, its hooks, and that the worktree's `.venv` is the worktree's own — never a link to another checkout's |
| Provision the worktree | `WorktreeProvisioner` (`control/worktree_provisioning.py`) | Everything `worktrees.setup` installs — the whole runtime environment, `.venv` and `packages/vscode/node_modules` alike |

The provisioner is the single owner of provisioning, and **every session launch
path** goes through it: coding, validation retry, rework, review and
retrospective review. It used to be invoked from the coding and validation-retry
paths only, so whether a worktree was runnable depended on which path had
created it: a rework or review worktree — the reused ones — reached the publish
gate unprovisioned, and the run died on a late, unrelated gate target. That was
issue [#48].

Provisioning holds four rules:

- **Fail closed, where the failure is.** A failing or timing-out setup command
  aborts the launch at provisioning — before a terminal exists — instead of
  letting an unprovisioned worktree reach a validation command. Where the claim
  sits relative to provisioning differs by path: the rework, review and
  retrospective-review paths provision before the claim is held, while the
  fresh coding and validation-retry paths hold the claim first and release it
  when provisioning fails.
- **Failing closed is bounded.** Failing the launch is not the same as being
  finished with it. A provisioning failure is usually environmental and
  persistent — a missing toolchain, a broken lockfile, an unreachable package
  registry — so retrying it forever re-ran the recipe and spent a session slot
  every tick while raising no human-visible signal at all: busy, making no
  progress, healthy from every signal except the tick log. That was
  issue [#54]. The provisioner therefore counts **consecutive** failures per
  issue against `PROVISIONING_ATTEMPT_LIMIT` (3). Under the bound the launch
  fails and the next tick may try again; a success clears the count, so a
  genuinely transient blip still recovers with no human involved. At the bound
  the issue is escalated to `needs-human` (shared block, `SESSION_LIFECYCLE`
  cause) with an operator comment and an `issue.needs_human` event, and every
  later launch refuses **before running the recipe**. The count is
  process-local; the escalation is not, and the label is what ends the refusal
  — a human clearing it is read as the retry request and restores the budget.
- **Do not touch the candidate.** Setup commands install tooling. The
  provisioner checkpoints `HEAD` and the worktree's dirty state before running
  them and re-reads both afterwards — **whether or not the commands succeeded**,
  because a failing command and an altered candidate are separate facts and a
  command that edits the candidate and then dies must not go unreported. A
  moved `HEAD` or a clean-to-dirty transition is a loud failure rather than a
  silent edit to the change under test. The prerequisites themselves are build
  output and are git-ignored, so an honest setup run leaves a clean worktree
  clean.
- **The recipe is pinned to operator configuration.** Which commands run comes
  from `worktrees.setup` in the configuration file the orchestrator was started
  with. That file must resolve outside the worktree being provisioned;
  otherwise provisioning refuses, so the worktree under test never supplies the
  list of commands run on it.

A repository that declares no `worktrees.setup` commands provisions nothing;
its worktrees must be runnable from the checkout alone.

### Each agent worktree gets its own runtime environment

**They do not share one.** This is stated because the opposite arrangement was
in place and was destructive: worktree creation planted a `.venv` symlink to
the repository's, so one environment served the primary checkout and every
worktree at once.

That sharing is what made a single provisioning run break things. Provisioning
runs the repository's *own* setup recipe inside the worktree, and any recipe
that populates `.venv` — `uv sync`, `pip install -e .` — writes through the
link into the shared environment. `uv sync` rewrites the editable install's
source path to the syncing worktree on **every** run, whether or not the
lockfile changed; when that worktree was later removed, the shared environment
imported nothing, and the primary checkout could no longer run its own pre-push
gate. The failure surfaced at an unrelated moment, because it was discovered by
the *next* thing that needed a Python environment rather than by the session
that caused it. That was issue [#53], fired by an ordinary prose-only session
at `max_concurrent_sessions: 1` — neither a dependency change nor concurrency
was required.

So the manager removes such a link (including one an older orchestrator left in
a reused worktree) and never creates one, and building the environment is
`worktrees.setup`'s job — the same division of labour as everything else in the
table above. The E2E worker worktree (`infra/e2e_worktree.py`) already worked
this way: it has always synced a real `.venv` of its own.

The cost was measured on this repository rather than assumed, since paying a
full install per session would be its own regression:

| Measured on a fresh worktree, warm caches | |
|---|---|
| `make worktree-setup` end to end (venv + `uv sync --frozen --all-extras` + `.venv-semgrep` + `npm ci` + `playwright install`) | 5.5 s |
| `uv venv` + `uv sync --frozen --all-extras` alone | ~0.3 s |
| Disk actually consumed by the second environment | ~7 MB — 307 MB apparent, but the package files are copy-on-write clones of the shared `uv` cache |

Reusing a worktree costs less still — `venv-fast` keeps the existing `.venv` and
re-syncs it. The first sync on a machine pays the download once, into the
shared `uv` cache, which is where the disk actually goes. So no per-session full
install is being paid: what a session pays is a sync against a cache it shares
with every other checkout, and `npm ci` — which the previous arrangement paid
too, and which dominates.

**Concurrency needs no coordination primitive here.** Two sessions provisioning
at once have no shared environment to race over, so nothing has to be trusted
to hold a lock — the correctness argument is disjointness, not mutual
exclusion.

What remains shared is caches, not environments: the `uv` package cache, the
Playwright browser cache (`PLAYWRIGHT_BROWSERS_PATH`), and the VS Code test
cache. Each is content-addressed or install-once, managed by the tool that owns
it, and already shared before this change; a run reads from them and adds to
them rather than repointing them at itself. That is the property the `.venv`
symlink did not have.

#### A directory is not evidence that the environment is this worktree's

Removing the link was not sufficient, because a `.venv` that was a **real
directory** was trusted for being a directory — no check that it belonged to
this worktree, none that it was usable. A worktree carrying what a previous
failed run left behind therefore passed setup untouched, and the recipe's own
reuse test was `[ -d .venv ]`, which a shell of a virtualenv satisfies. `uv
sync` was handed an environment it could not use, found the project *installed,
but mismatched* against an install record that lived in another checkout, and
reconciled it by reinstalling editable there — moving that checkout's `.pth`.
Same damage as the symlink, with no symlink involved. That was issue [#61].

The rule is therefore stated as an invariant of the worktree rather than as a
question about links: **a worktree's `.venv` is either this worktree's own
healthy environment, or absent.** `adapters/worktree/_worktree_venv.py` owns it
and asks two things of a `.venv` before it may be reused:

| | What is checked | Why |
|---|---|---|
| Provenance | Every install record inside it (`.pth`, `.egg-link`, `direct_url.json`) names a path inside this worktree | A record naming another checkout is the incident's own evidence. The base interpreter is exempt: every virtualenv points at one, it is outside every worktree by construction, and it is read rather than rewritten |
| Health | `pyvenv.cfg` is present and an interpreter resolves | The absence of `pyvenv.cfg` is *why* the interpreter resolved into a different environment. This is the shape the incident left behind: a `bin/python` and nothing else |

Anything failing either is removed, and `worktrees.setup` rebuilds it. Removal
never follows a link out of the worktree — a symlinked `.venv` is unlinked,
never emptied — because deleting the contents of another checkout's environment
would be the same defect with a larger blast radius.

**The recipe's reuse test is sound because both halves hold.** `make venv-fast`
no longer reuses on `[ -d .venv ]` alone: it requires `pyvenv.cfg` and a usable
`bin/python`, and replaces the directory otherwise. That is all a recipe can
check, since it cannot tell one checkout from another; provenance is the
caller's guarantee, and worktree setup makes it before the recipe runs. Both
are needed — the orchestrator is not `venv-fast`'s only caller.

An environment that records paths outside its own checkout on purpose — a
sibling package installed editable from a monorepo — is rebuilt every session
under this rule. That is a decided trade-off: it is the one legitimate shape the
rule rejects, and the alternative is trusting the shape the incident produced.

`tests/integration/test_worktree_runtime_isolation.py` holds the
failure-direction proof: a worktree provisions, is removed, and the primary
checkout still resolves the package from its own source. It holds the same
proof for the reuse shape — a worktree carrying a real-directory `.venv` whose
install record names the primary checkout provisions without touching that
checkout — and a reused worktree's *own* environment is proved to survive
provisioning, so isolation is not being bought with a full install per session.

#### What `.deps-synced` claims

**`<venv>/.deps-synced` states that the environment beside it is usable by this
checkout — not that a setup recipe ran to the end.** The distinction is the
whole of issue [#60]. `venv-fast` wrote the marker with a bare `touch` in a
`;`-separated shell block, so a `uv sync` that failed, or one that succeeded
having installed nothing, still reached the `touch`; `make` still exited 0,
because the last statement in the block had succeeded. A provisioning run
measured during [#53] left the marker present next to three entries in
`site-packages` and no editable `.pth` — an environment that could not import
the project it claimed to have installed. A marker that cannot fail is worse
than no marker, because it invites exactly the reliance that makes the false
positive load-bearing.

So no recipe writes it. `scripts/deps_marker.sh` owns the rule, and every
writer goes through it:

| Step | What it does | Why |
|---|---|---|
| `clear` before the sync | Removes the marker | The claim is withdrawn before the work that would re-establish it, so a sync that aborts halfway cannot leave yesterday's marker asserting today's environment |
| the sync itself | `set -e` in the recipe block | A failed `uv sync` fails the recipe instead of being swallowed by the `;` separator |
| `record` after the sync | Verifies, then writes | `python -I -c "import issue_orchestrator"` must resolve inside this checkout. Isolated mode is required: agent sessions export a `PYTHONPATH` pointing at the Control Centre snapshot, which would otherwise answer for a different checkout |

**The ordering is the rule, so the owner owns the ordering too.** A writer that
`record`s without having `clear`ed is not obviously wrong to read, and it is
exactly the half-bracket that leaves a failed sync standing behind an earlier
run's marker. `deps_marker.sh guard <venv> <root> -- <cmd…>` performs the whole
bracket — withdraw, run, re-establish — and passes the command's exit status
through, so a writer that can express its sync as one command cannot do half of
it. `install`, `upgrade-deps`, `deps-batch`, `sync-deps` and the Control Centre
launcher each make one `guard` call. Only the `venv*` recipes, which interleave
venv creation and timing logs, still bracket by hand. The `semgrep-venv` step
several of these targets run afterwards now sits outside the bracket: the marker
speaks for the main environment, so a Semgrep failure should not withdraw a
claim the main sync earned.

`guard` judges the command by its own exit status, and capturing that status
suppresses `errexit` inside it — so what is handed over must be one command or
an `&&` chain. A `;`-separated sequence would report only its last statement,
which is the original defect wearing a different hat.

The Semgrep tool environment carries a marker of the same name
(`.venv-semgrep/.deps-synced`) and is deliberately outside this rule: it is
synced with `--no-install-project`, so there is no project install in it to
probe. Its recipe writes the marker only on a successful sync, and its reuse
test asks for an executable `bin/semgrep`.

The Control Centre launcher has asked that same question of its own environment
since before the marker did (`verify_project_install`), and now sources the
shared rule rather than keeping a second copy of it — one marker, one meaning,
one owner. That matters because the marker it writes is not private: its
`install_mode` selects the uv path only when its venv *is* `<root>/.venv`, so it
writes the same file `sync-deps` reads.

`tests/unit/test_deps_synced_marker.py` holds the failure-direction proof: a
`uv sync` that fails, one that installs nothing, and one that installs another
checkout each leave the recipe non-zero with no marker — for `venv-fast` and
`install` alike, and including when an earlier healthy run had left a marker
behind. It also pins the rule statically: no `touch` of the marker under any of
its spellings in either writer file, and no `record` in a Makefile target that
did not `clear` first. `tests/unit/test_start_control_center_script.py` covers
the launcher's side of the same failure.

### What authority provisioning runs at

Provisioning executes the configured commands at orchestrator host authority,
with the worktree as the working directory, and those commands resolve to the
repository's own build files. That is stated here rather than left implicit,
because it is the same authority, in the same worktree, under which the
configured validation gate already runs the repository's build and test code —
`validation.quick.cmd` and `validation.publish.cmd` are shell commands run in
the worktree too.

Routing the rework, review and retrospective-review launches through the
provisioner therefore adds no class of executed code and no authority that the
gate in those same worktrees did not already carry; it makes that gate's
verdict mean what the record says it means. The two bounds above — a recipe
pinned outside the worktree, and a candidate the run may not alter — are the
bounds that are actually enforced, and both are checkable in
`control/worktree_provisioning.py`.

#### Operator-authorized host execution — the contract (decided in #55)

Running a repository's own validation and provisioning code **is an intended
capability of this orchestrator**, not an accident. What that permission is, and
what it is not, is stated here.

**The authority comes from outside the candidate.** It is the execution
configuration the operator started the orchestrator with, and the repository and
command selectors that configuration resolves to. It does **not** come from the
repository's contents, and it does **not** come from who wrote them. A candidate
cannot grant itself this permission by containing anything, which is what
`_require_pinned_recipe` enforces mechanically: a recipe sourced from inside the
worktree being provisioned is refused.

**Call it operator-authorized host execution.** Do not call it a "trusted
repository". That phrasing locates the authority in the repository, which is
exactly where it does not live, and it invites the inference that repository
contents can be assessed for trustworthiness. The distinction this contract
draws is only whether explicit operator authorization exists.

**Validation and provisioning are one execution-authority class.** They run the
same kind of code, in the same worktree, at the same authority. The extra guards
each carries — the pinned recipe and the unaltered-candidate checkpoint above —
are **integrity bounds, not authority bounds**: they constrain what a run may do
to the candidate, not what the run is permitted to be.

**This is a bootstrap/legacy posture, not a security boundary.** The permission
is currently unbounded once granted: the executed code runs at host authority
with no isolation substrate. It is permitted because an operator explicitly
authorized it, not because anything constrains it. A bounded execution substrate
is separate hardening and is tracked as its own capability; nothing here claims
one exists.

**ADR-0034 does not govern this.** `ADR/0034-sandbox-scope.md` addresses the
**agent-session** sandbox. Repository commands the *orchestrator* runs through
its own subprocess path are not automatically inside that scope. If ADR-0034 or
a successor is ever to claim end-to-end bounded execution, it must either
explicitly include this subprocess path or record it as a limitation. It does not
precede this decision and this decision does not settle it.

**No rule keys on repository authorship.** A repository the operator did not
write is not treated differently from one they did. The only distinction is
whether explicit operator authorization is present.

### The one worktree that is exempt

Not every worktree an agent sits in is created by a session launch. The
persistent review-exchange reviewer worktree
(`execution/reviewer_worktree.py`, `<coder-worktree>-review-<timestamp>`) is
created with a raw `git worktree add --detach`, outside `WorktreeManager`. It
therefore gets neither guarantee in the table above: nothing `worktrees.setup`
installs, and no runtime environment at all.

That is deliberate, and it is the only such exemption. The reviewer reads the
candidate's code; it does not run gates, and provisioning it would pay
`worktrees.setup` — for this repository an `npm ci` and a browser install — per
exchange to support a command that cannot produce a verdict there anyway.

**A barrier, not an instruction, is what makes the exemption safe.**
`docs/architecture/hooks.md` is explicit that policy documents and prompts are
suggestions while hooks are enforcement, so the exemption cannot rest on the
reviewer choosing to comply. Creating the worktree installs a `PreToolUse`
Bash guard into it (`adapters/worktree/_review_command_guard.py`), and that
guard **refuses** build, test and validation commands before they execute
(`infra/hooks/review_command_guard.py`). Two properties make it trustworthy:

- **Pinned.** The registered command names the running orchestrator's own
  interpreter and `src` root, so the policy that decides is the orchestrator's,
  never a copy the guarded worktree contains.
- **Outside the candidate.** It is written to `.claude/settings.local.json`,
  the never-tracked local settings layer, and hidden from the worktree's `git
  status`. Nothing the candidate commit tracks is modified. A guard that
  should have been installable but could not be written rolls the worktree
  back, so an I/O failure cannot quietly produce a worktree that was meant to
  be guarded and is not.
- **Installed for the provider that will actually run there, or not at all.**
  The guard is registered through one provider's hook mechanism, and
  `create_reviewer_worktree` is given the provider the exchange launches
  (`launch_config`, the same derivation the execution-identity record reads).
  For a provider outside `GUARDABLE_PROVIDERS` — today, anything but
  `claude-code` — **nothing is written**: a `.claude/settings.local.json` in a
  worktree whose agent never reads it is a claim of enforcement, and this
  worktree has had enough of those. The installer reports `guarded=False` and
  logs it at WARNING.

**Known gap: a Codex reviewer is unguarded.** `main.yaml`, the default mode,
configures `agent:reviewer` on `codex`, and no guard mechanism is implemented
for it, so in that configuration `REVIEWER_WORKTREE_IS_UNPROVISIONED_NOTE` is
still the only thing between the reviewer and a gate command — the
prompt-only arrangement `docs/architecture/hooks.md` rules out. It is named
here rather than papered over. Codex does have project-local exec policies
(`adapters/hooks/codex.py`, `prefix_rule`/`execpolicy`), but the CLI disables
project-local config, hooks and exec policies until the project is *trusted*,
and a reviewer worktree is a directory nothing has trusted — so planting a
rules file there would produce another decorative guard. The two real closures
are (a) making the reviewer worktree trusted at creation so a Codex exec policy
loads, or (b) dropping the exemption for unguardable providers and routing the
worktree through `WorktreeProvisioner`, which costs `worktrees.setup` per
exchange. Both are product decisions larger than the installer, and neither is
made here.

Every reviewer prompt still carries `REVIEWER_WORKTREE_IS_UNPROVISIONED_NOTE`
(`domain/review_exchange.py`), unconditionally — `review.exchange.loop.
require_validation` decides only whether a validation *record* gates approval,
so with it false the reviewer would meet a refusal with no idea why. Where the
guard is installed the note is the explanation and the guard is the invariant;
where it is not (see the gap above) the note is all there is. A change that
lets the reviewer run gates must remove the guard *and* route this worktree
through `WorktreeProvisioner`.

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
