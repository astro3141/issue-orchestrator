# Validation System

Validation is a **local lifecycle gate**, not a CI system.

## Model

- Run one quick local command while the coding/review loop is active
- Run one deeper publish command before push/publish
- Cache passing results by commit SHA + command + contract — against the
  candidate's durable `Attempt(issue, A)` where the caller can name the
  candidate, and against the worktree's own records where it cannot
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

Receipts accumulate. `Attempt.completed_evaluations` is an ordered,
append-only history — order is list position, so nothing inside the receipt
had to change to carry it — and every gate run that reaches a verdict appends
to it. Nothing rewrites or drops an earlier entry. Each entry names the
contract that produced it, so a reader asks for the contract it cares about
rather than trusting whichever entry is last:
`Attempt.latest_publication_evaluation` walks the history newest-first for a
receipt from the publication contract, which is why the quick gate appending
its own evaluation cannot make a publication pass unreadable.

The sidecar's `schema_version` is `2`. A `v1` sidecar's single
`publication_verdict` slot migrates on read to a one-element history; it is
real durable evidence about a real candidate, so refusing it would erase a
gate result the orchestrator itself wrote.

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
- The **manual resume** route (`POST /api/issues/{n}/resume`) starts from an
  issue *number* in a URL path, so it fetches the authoritative issue and
  derives the canonical key from that. It re-drives ordinary completion
  processing — the same processor the live path runs, and a path that can end
  in a review — so a run it drives has to be able to leave a receipt. When the
  issue cannot be read it declines the request rather than proceeding
  key-less.

No path writes a receipt under a *derived* identity. Reversing a work-item
number back into a key is the drift [#40] removed, and a receipt filed under a
key nothing else uses is worse than no receipt at all. That is why resume asks
the repository instead of reusing the display-title lookup, which answers with
the placeholder `Issue #<n>` when nothing responds.

### The receipt is also the cache

The publication gate does not only *file* verdicts on `Attempt(issue, A)`; it
**consults** the same history before executing anything. Both directions go
through one owner, `CandidateEvaluations`, which is the only writer of
`Attempt.completed_evaluations` and the only reader of it that a gate uses.

That closes issue [#159]. Until then, the publication gate's only cache was the
publish record store *inside the worktree it was running in*, so the reuse it
could see died with the worktree. The observed sequence was:

```text
eval[0] FAILED   # the original gate run
eval[1] PASSED   # #139's bounded same-SHA revalidation, in a detached checkout
eval[2] FAILED   # a fresh continuation worktree re-ran the whole publish gate
```

The third evaluation displaced the legitimate PASS as latest authority and
locked the candidate out of review. It should never have existed: nothing about
the candidate had changed, and the contract had already decided about it.

The bounds are the ones the history already carries, not new ones:

- Only a **PASS** is reusable. A FAIL or a timeout re-runs, which is what makes
  [#139]'s revalidation route possible at all.
- The **latest** matching evaluation decides. An older PASS never hides a newer
  non-PASS.
- Reuse is matched on the exact `head_sha` and on `result_mismatch` over
  suite + command + profile, so `A'` never inherits `A`'s receipt and a drifted
  contract is a miss rather than a reuse.
- Reuse **appends nothing**. A cache hit executed no contract, and an
  append-only history that recorded lookups would claim the gate ran more times
  than it did.
- A caller with no canonical issue key derives no attempt identity at all: it
  neither reads nor writes the durable history, and keeps the worktree-local
  cache semantics it has always had.

`Attempt.validation_record_path` is a single slot that every contract filing
here writes, so the last one to run owns it. That pointer is **not** authority:
it exists so a surviving record can be materialised into a run directory, and a
pointer naming another contract's record — or a reaped one — reads the same
way, as a cache hit with nothing to materialise.

### A failed verdict also has to leave its explanation

The receipt says *what* the gate decided. It deliberately carries no output, so
after cleanup a FAILED verdict named a candidate nobody could diagnose: the
run's stdout and stderr were in the run directory, inside the worktree, and
ordinary cleanup reaped them seconds after the verdict was reached. Issue [#94]
is that loss, observed twice on one issue — once 14 s after the verdict, once
4 s after — where the second failure did not reproduce in a clean detached
environment, so the deleted output was the only thing that could ever have
explained it.

So a publish run that does **not** pass also writes its output to a durable
destination in the primary checkout:

```
.issue-orchestrator/diagnostics/publish-gate-failures/
    <issue-scope>--<issue-id>--<HEAD_SHA>--<timestamp>/
        failure.json     # issue key, verdict receipt fields, exit code, timings
        stdout.log       # the run's stdout, verbatim
        stderr.log       # the run's stderr, verbatim
```

Three properties are the whole design:

- **Written at gate-execution time**, from the bytes the runner already holds
  in memory, in the same step that writes them into the run directory. It is
  *not* copied out of the worktree afterwards: that copy is a race against
  cleanup, and the race has been lost twice, including once by a human trying
  to win it by hand.
- **Bound to exactly one candidate**, under the same `(issue, HEAD_SHA)`
  identity in the same spelling the attempt sidecar uses — so the directory
  name for a candidate's diagnostic and the filename of its attempt sidecar
  share a stem, and a reader holding the receipt can find the explanation
  without following a pointer. There is no pointer by design; see below.
- **Diagnostic, never authority.** Nothing points at it from the attempt
  record, and no predicate takes its path or its existence as input.
  `Attempt.completed_evaluations` remains the only thing that decides what the
  gate decided. A losing gate run must not be able to write anything that
  admits work.

A pass writes no such artefact — its output stays where every passing run's
output has always stayed — and the trigger is the *verdict* rather than the exit
code alone, so a timeout is covered by the same seam with no timeout-specific
rule. A candidate with no canonical issue key (the manual-reprocess route)
files no diagnostic, for the same reason it files no receipt, and says so in the
log rather than skipping silently.

### What the receipt authorizes

The receipt exists to be read, and [#45] is what reads it: review admission
requires that the PR's **current** head SHA have a receipt certifying the
publication contract passed for that exact commit. `PublicationVerdictReader`
is the one collaborator the PR scanner, startup recovery and the launcher all
consult, so none of them can admit a candidate on evidence the others would
reject. See [Review workflow](../development/REVIEW_WORKFLOW.md) for the
admission rules themselves.

Which makes "did a receipt get filed for the commit we published" a
publication-side obligation, and two shapes have to be closed there rather
than at the reader:

- A **non-fast-forward push retry** rebases before it pushes, so the commit it
  publishes is not the commit the gate ran against. The retry re-runs the
  publish contract on the rewritten HEAD before pushing. Passing files the
  receipt for the published commit; failing refuses the publication, and
  nothing is pushed.
- A run whose **own profile defines no `publish.cmd`**, in a repository where
  another profile does, could never file a receipt while admission would
  always demand one. The gate refuses such a candidate — naming the profile —
  instead of publishing something no review could ever be launched for. This
  is only ever asked of a completion that offers its work as a change, so a
  reviewer or tech lead profile with no publish command is unaffected.

### One bounded re-evaluation of an unchanged candidate

A candidate can fail the publication gate for a reason that is not about the
candidate — the observed case was a live subprocess timing out inside the
suite. Structured evidence cannot distinguish that from a real defect: the
receipt reads `verdict: "failed"` either way. Before [#139] the only exits
were moving the SHA, which destroys the artifact the evidence is about, or
editing durable state by hand, which is forbidden.

`control/publication_revalidation.py` admits exactly one mechanical
re-evaluation instead, and every part of it is a bound:

- **Identity.** Its only input is a durable canonical `Attempt`. There is no
  issue number, URL, title or reconstructed key on the surface, and a
  candidate the store does not hold is refused before anything else happens.
- **Admission.** The latest completed publication evaluation must be non-PASS,
  and its suite + command + profile must still be what the contract requires —
  compared with `ValidationGateContract.result_mismatch`, the same predicate
  the cache and review admission use. A drifted contract is a different
  question, not a retry of the same one.
- **Allowance.** `Attempt.revalidation_budget_used` bounds it to one, ever, per
  candidate, and it is durable in the attempt sidecar. It is a *start* budget:
  it is spent before any external gate work begins, so an interruption between
  reservation and verdict leaves the allowance spent and the prior completed
  evaluation authoritative. There is deliberately no refund.
- **Artifact.** The worktree is materialised at the exact recorded commit
  through `CandidateCheckouts` (`git worktree add --detach <sha>`), never at a
  branch — a branch is a moving name, and one that has advanced would evaluate
  other work under this candidate's identity. The run scaffold freezes the
  profile *name taken from the prior receipt* rather than resolving one afresh.
- **Environment.** Materialising source bytes does not make the contract
  runnable against them: the gate's command resolves its tools out of the
  worktree it runs in, and a detached checkout that has never been provisioned
  answers `.venv/bin/pyright: No such file or directory` ([#153]) — an
  environment gap wearing the same `verdict: "failed"` this route exists to
  disambiguate. So the checkout is made runnable by the operator-pinned
  `worktrees.setup` recipe every managed worktree already uses, through
  `control/worktree_runnability.py`, *after* the allowance is reserved and
  *before* the gate is asked anything. The same core proves the recipe left
  HEAD at the recorded commit and the candidate's tracked content untouched. A
  checkout that cannot be made runnable reaches no gate, appends no
  evaluation, leaves the prior non-PASS authoritative, and does **not** get its
  allowance back — a repeatably broken environment is not a supply of retries.

`WorktreeRunnability` is deliberately the provisioning **core** and not
`WorktreeProvisioner`: the launch provisioner bundles a per-issue
consecutive-failure ledger and a `needs-human` escalation with the recipe, and
a second retry predicate over [#139]'s single start allowance is precisely
what the policy forbids. The recipe and the candidate-integrity proof are
shared; the bound around them is not.

The gate itself is untouched: the route composes `PublicationGate.check`
whole, through the same `build_publication_gate` every other composition
calls, and the verdict reaches the history through the gate's own receipt
writer — appended beside the failure it re-ran. Appended only when the gate
*reached* that verdict: a run that reused an earlier passing record executed
nothing, and an append-only history that recorded reuse would claim the
contract ran more times than it did.

`entrypoints/bootstrap.py` composes the route — through
`entrypoints/bootstrap_revalidation.py`, which holds the wiring itself — and
holds it on `OrchestratorDeps.publication_revalidation`, where a consumer
reaches it as it reaches the other owners there. The field is required, so
neither composition root can build an orchestrator without one.

### What happens to a candidate after it is re-evaluated

Re-evaluation is not continuation. A candidate whose publication failed has
usually lost its worktree to cleanup, and with it the **completion record** —
the only place the agent said whether it wanted a pull request and what it
claimed to have built. A later PASS then proves something about the artifact
and nothing about what to do with it, so [#149] adds the missing half:

| Fact | Where it lives | Written by |
|------|----------------|------------|
| Recorded intent (`requested_actions`, `implementation`, `problems`) plus the contract identity | `Attempt.continuation_descriptor` | `control/continuation_descriptor_writer.py`, at the gate's verdict, **only when the verdict refuses** |
| The exact-`A` review outcome | `Attempt.continuation_review_verdict` | `control/continuation_runner.py`, promoted from the run's own verdict binding before its worktree is discarded |
| What the continuation run produced — the pull request, or that none was asked for | `Attempt.continuation_settlement` | `control/continuation_finalize.py`, from the `ProcessingResult` the run's own completion pipeline returned |
| How many runs the continuation has opened for this candidate | `Attempt.continuation_runs_used` | `control/continuation_runner.py`, spent before a run is opened |

Every field is **copied** from an authoritative producer. Nothing is derived
from issue text, labels, logs, diagnostics, URLs or branch names, and an
absent descriptor means *no recorded intent* — never empty intent, and never a
continuation. Only a refused verdict records one: a candidate that PASSED is
still being driven by its live session, and a descriptor there would invite a
second driver to race it. One descriptor exists per issue, because filing a
newer candidate's intent clears the older one — supersession the durable record
states rather than one a reader has to infer.

From those three facts plus the evaluation history, the allowance, and the
`pr-pending` label the tick has already fetched,
`domain/continuation_phase.py` derives which phase a candidate is in, and
`control/continuation_live_truth.py` turns the live ones into the
`live_operations` set `ControlOperationOwnership.reconcile` consumes. Four
phases hold the issue (`EXECUTING`, `RETRY_PENDING`, `PASS_PENDING_REVIEW`,
`APPROVED_PENDING_PR`); the rest release it. No lease row is read to decide
liveness, so a row that outlived its operation can never exclude ordinary work
on its own authority — and a durable record that cannot be READ keeps the
projection already standing rather than publishing "nothing is live".

Two of those phases exist because a durable record alone answers the wrong
question:

- **`EXECUTING`** is derived from `control/continuation_in_flight.py`, the one
  non-durable input. The [#139] allowance is a *start* budget spent before any
  gate work, so for the whole duration of a revalidation the record reads
  "allowance spent, latest evaluation still the failure" — indistinguishable
  from a revalidation that ran and failed, and therefore `EXHAUSTED`. Without
  this phase the lease of a *running* operation is released and ordinary rework
  is re-admitted mid-run. The registry is process-local by design: it cannot
  survive a crash, so a restart falls back to the durable facts and [#139]'s
  fail-closed direction is preserved rather than replaced by a deadlock.
- **`SETTLED_PR` via the recorded settlement** exists because the continuation
  creates no session and its pull request carries no code-review label, so none
  of the three writers of `pr-pending` ever observe it. Waiting on the board
  would leave `APPROVED_PENDING_PR` live forever, re-running a full reviewer
  exchange on every reconciliation while holding the issue's lane.

A settlement is not the only way the two run-opening phases end. A run that
reaches a terminal result *without* discharging the intent — a halted exchange,
an exhausted no-progress budget, a PR that could not be created — changes no
durable fact, so the same phase is derived again and another run opens. That
retry is right for a transient failure and unbounded for a permanent one, and
the exchange's own no-progress budget cannot bound it: that budget is read from
the cache under `<worktree>/.issue-orchestrator/sessions`, which goes with the
checkout each closed run removes, so every retry starts it afresh.
`Attempt.continuation_runs_used` is therefore the continuation's own allowance,
in the shape [#139]'s is — durable, beside the candidate, and spent *before* a
run opens so an interrupted one cannot refund itself. When it is gone the phase
is `RUNS_EXHAUSTED`, which returns the candidate to ordinary rework with its
descriptor, its evaluations and any review verdict intact.

`control/continuation_scheduling.py` is the one hydration path: it derives,
reconciles, publishes, advances what this engine owns, and only then lets
`QueueCache` evaluate eligibility. Derivation runs inside the ownership owner's
own lock (`reconcile_derived`), which is what makes a stale snapshot unable to
release a newer claim. `control/continuation_runner.py` executes: it hands a
`RETRY_PENDING` candidate whole to [#139] — no second admission predicate and
no second allowance — and drives a passing one through the ordinary
`CompletionProcessor`, in a worktree verified to stand at exactly `A`.

That worktree belongs to a **run**, not to a pass, and
`control/continuation_runs.py` owns it. `CompletionProcessor.process` does not
necessarily finish when it returns: with a background job supervisor wired —
the only configuration in which the continuation executes at all, since its own
job goes through the same supervisor — the review exchange becomes its own job
and the result reports `review_exchange_deferred`. A pass that disposed of its
checkout on the way out would delete the working directory of the exchange still
running in it, along with the `summary.json` the resume path reads; and because
`run_id` is part of the exchange's job identity, the next pass would mint an
identity no dedupe could recognise and start another exchange. So the run stays
open across passes while the pipeline reports it unfinished, and is disposed of
exactly once — when the pipeline reaches a terminal result, when the candidate is
retired, or when the operation leaves the live set entirely. It hands
the resulting `ProcessingResult` to `control/continuation_finalize.py`, the
continuation's analogue of `control/publish_retry_finalize.py`: the finalizer
announces the pull request on the board and *then* records the settlement, so a
board signal that could not be applied leaves the operation retryable rather
than terminating it while the issue's lane reopens with an approved PR already
open.

### A paused engine reconciles but starts nothing

Pause is a barrier to new work, not a cancellation ([#161]). A paused engine
still hydrates from a refreshed board, still reads durable continuation truth,
still reconciles control-operation ownership and still publishes the exclusion
projection — without those, pausing would hand a running control operation's
issue back to ordinary rework. What it does not do is *start*: while
`state.paused` is established, `control/continuation_runner.py` submits no
continuation job, reserves no [#139] revalidation or [#149] run allowance, cuts
no checkout and opens no reviewer exchange or pull request. The barrier sits
there — the one place a control operation's work begins — rather than in
`control/continuation_scheduling.py`, precisely so the read side keeps running.

Withholding is not undoing. An operation stays owned, its recorded intent and
allowances are untouched, and a run already open stays open; a job submitted
before the pause is neither cancelled nor duplicated and settles on its own
terms, as does an ordinary agent session, which `Orchestrator.tick()` processes
before it consults the paused health gate at all. After a resume, the next
reconciliation starts whatever is still live.

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

What "provisioned" *means* — running the operator-pinned recipe and proving it
left the candidate alone — is `WorktreeRunnability`
(`control/worktree_runnability.py`), and the two rules below marked
*(enforced by `WorktreeRunnability`)* are enforced there. The provisioner is
that core plus the launch policy around it: how often a launch may re-ask, and
what happens when it stops being worth asking. Two callers consume the core
directly rather than through the provisioner, because each already carries its
own start budget and a launch ledger over it would be a second bound:

- the same-SHA revalidation route ([#153]), bounded by [#139]'s single start
  allowance;
- the control continuation's `continuation-*` coder worktree ([#160]), bounded
  by [#149]'s continuation-run allowance. That checkout is not a read-only
  carrier for a cached PASS: the persistent review exchange opens on it as the
  **coder's** worktree, and a `CHANGES_REQUESTED` round asks that coder to edit
  and validate in it. It is provisioned after the run allowance is reserved and
  before any run asset exists, and a checkout that cannot be made runnable
  opens no run, records no verdict or settlement, leaves the durable PASS
  latest, is removed, and gets no refund. A *persistently* broken environment
  therefore spends that allowance one run at a time with only a warning per
  pass — no escalation of its own, by design. It still reaches a human, one hop
  later: exhaustion derives `RUNS_EXHAUSTED`, the candidate returns to ordinary
  rework, and the launch provisioner escalates the same broken environment to
  `needs-human` there.

The provisioner is the single owner of provisioning for launches, and **every
session launch path** goes through it: coding, validation retry, rework, review
and retrospective review. It used to be invoked from the coding and validation-retry
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
- **Do not touch the candidate** *(enforced by `WorktreeRunnability`)*. Setup
  commands install tooling. The core checkpoints `HEAD` and the worktree's
  dirty state before running them and re-reads both afterwards — **whether or
  not the commands succeeded**, because a failing command and an altered
  candidate are separate facts and a command that edits the candidate and then
  dies must not go unreported. A
  moved `HEAD` or a clean-to-dirty transition is a loud failure rather than a
  silent edit to the change under test. The prerequisites themselves are build
  output and are git-ignored, so an honest setup run leaves a clean worktree
  clean.
- **The recipe is pinned to operator configuration**
  *(enforced by `WorktreeRunnability`)*. Which commands run comes
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

The table is framed per session launch, but a same-SHA revalidation ([#153])
pays the same recipe in its exact-SHA checkout, which is then force-removed
once the gate has run. That cost is bounded the same way the revalidation
itself is: [#139]'s single start allowance means at most one such run per
candidate, not one per tick. A continuation coder worktree ([#160]) pays it on
the same terms, bounded by [#149]'s continuation-run allowance.

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
`control/worktree_runnability.py`, which is where every caller gets them from:
a session launch through `WorktreeProvisioner`, a same-SHA revalidation
directly.

#### Operator-authorized host execution — the contract (decided in #55)

Running a repository's own validation and provisioning code **is an intended
capability of this orchestrator**, not an accident. What that permission is, and
what it is not, is stated here.

**The authority comes from outside the candidate.** It is the execution
configuration the operator started the orchestrator with, and the repository and
command selectors that configuration resolves to. It does **not** come from the
repository's contents, and it does **not** come from who wrote them. A candidate
cannot grant itself this permission by containing anything, which is what
`WorktreeRunnability._require_pinned_recipe` enforces mechanically: a recipe
sourced from inside the worktree being provisioned is refused.

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

"Not created by a session launch" is not itself the exemption. Its sibling —
the `continuation-*` **coder** worktree the same exchange opens on — is not
created by a launch either, and it *is* provisioned ([#160]), because a
`CHANGES_REQUESTED` round runs the candidate's own edit-and-validate work
there. What is exempt is the worktree that only reads.

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
