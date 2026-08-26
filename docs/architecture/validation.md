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

### Live-agent assurance is not publication validation

There is a **third** lane, and it is deliberately not in the table above.

Some tests drive a real provider CLI: the OS-sandbox boundary proof in
`tests/integration/test_sandbox_os_boundary.py` launches `claude` and `codex`
with the generated sandbox argv and asserts on what the operating system did.
Whether such a test executes at all depends on an external model choosing to
issue a tool call. While it sat inside the pinned publish command, a model that
declined to issue one was recorded as **the candidate failing**, three times
([#109]) — most recently against a candidate whose changes had nothing to do
with sandbox behaviour.

Segregation is by **marker**, not by filename. A module declaring
`pytest.mark.live_agent` is deselected from every blocking target
(`-m "... and not live_agent"`, the way `live_codex` already worked) and
collected by `make test-live-assurance`. There is no second list: adding a
fourth live-agent module requires no other edit, and
`tests/unit/test_makefile_validation_phases.py` fails if the publish gate ever
names one by path again.

**Being deselected by a marker is not the same as being out of the gate**
([#227]). `live_codex` is the case that proved it: every blocking *selector*
deselected the marker, and `validate-pr-raw`'s live-web phase then named
`test-integration-core-live-codex` — whose selector is `-m "live_codex"` —
directly. So the one real-Codex test in `tests/integration` was segregated from
one lane and run by another, and a candidate with nothing of its own at fault
failed publication on `prompt_not_accepted` after 120 s idle. Marker selection
and target membership are two separate facts about a test, and the guardrails
pin both, for both markers.

The lane is still there, as `make test-integration-core-live-codex` —
provider/model compliance evidence for the exchange round trip, run
deliberately. Unlike the assurance lane it files no record at all, so there is
nothing it could produce that any gate would read.

**The marker is module scope, so what it takes out has to be checked test by
test.** `pytestmark` applies to the whole file: mark a module and every case in
it leaves every blocking gate, and the lane that collects them files a record
rather than failing a candidate — so a deterministic assertion carried along by
the marker ends up in *no* gate at all. That is not hypothetical; it is how
`TestShellEscaping` and the `agent-done` cases went unrun until they were moved
to `tests/integration/test_agent_invocation_surface.py`. The rule has an owner
now: `tests/live_agent_reach.py` states *every test in a `live_agent` module
must reach a live provider* structurally — the body must name a provider CLI, a
registered production seam that builds one, a registered live-provider probe, or
one of the lane's `assert_no_breach` / `require_probe_ran` helpers — and
`tests/unit/test_makefile_validation_phases.py` fails on any test that does not.
Reach means *the provider CLI*, not *the model*: `codex --version` needs no
model but does need the operator's installed CLI, and a CLI upgrade must not be
able to fail an unrelated candidate.

The lane's result is one of exactly three, and the middle one is the point:

| Outcome | Meaning |
|---------|---------|
| `PASS` | The required operation was actually issued and the allow/deny boundary was proven. |
| `SECURITY_FAIL` | The boundary was really exercised and a security assertion failed. |
| `INCONCLUSIVE` | The provider was unavailable, the run timed out, the model never issued the required operation, or nothing was selected. |

`INCONCLUSIVE` is neither a candidate failure nor a security pass; the failed
observation is preserved in the record's `detail` rather than reinterpreted.
Precedence is `SECURITY_FAIL` > `INCONCLUSIVE` > `PASS`, so a proven breach
never hides behind an unrelated provider hiccup and an empty selection never
reads as a vacuous pass. Re-running the *lane* after an `INCONCLUSIVE` is
availability handling for assurance evidence; it is not a retry of any
candidate's validation, and nothing re-runs a gate on a candidate's behalf.

Records live at `.issue-orchestrator/live-assurance/<HEAD_SHA>.json` —
a separate directory from `.issue-orchestrator/validation/`, keyed by the
**artifact commit alone**. What the lane proves is a property of a build, not
of somebody's candidate for an issue.

**The artifact is the checkout, not a SHA passed beside it.** The lane is given
one root (`--live-assurance-root`, `LIVE_ASSURANCE_ROOT`) and resolves the
commit *and* the working-tree state from it, through
`execution/assured_artifact.py`. Computing the SHA in the Makefile recipe
instead would take it from `make`'s cwd while the record was written under a
separately overridable root, so the two were free to name different checkouts
with nothing downstream able to notice. A root that is at no commit is a usage
error, before any probe runs.

**A dirty tree assures nothing.** Running the lane mid-change is normal — it is
when sandbox work happens — but the probes then exercise a tree the commit does
not name. The record carries `working_tree_dirty` and `assures()` refuses it, so
the lane stays usable during development while a promotion cannot be admitted on
evidence gathered from uncommitted edits. This is the same SHA↔tree discipline
`validate-pr-raw` follows when `deps-batch` avoids seeding the SHA-keyed
pre-push cache from an uncommitted tree.

**Blocking validation still *imports* live-agent modules.** `-m "... and not
live_agent"` deselects after collection, unlike the `--ignore=` it replaced, so
every module under `tests/integration` is imported on every publish — once per
xdist worker. A module-scope provider probe would therefore put a real `claude`
call inside the publish gate for tests that gate is about to deselect. Readiness
probes belong in an autouse fixture — a `skipif` condition is module scope, since
a decorator expression is evaluated on import — and
`tests/unit/test_makefile_validation_phases.py` proves by AST that no
integration module calls one at import time.

That fixture reports an unusable provider through `require_probe_ran(...)`, so
the answer arrives as `INCONCLUSIVE` by the same route as a model that declined
to issue the tool call. Both leave the boundary equally unexercised, and a
missing prerequisite that fails is one somebody notices — which is also what
the orchestrator's own newly-added-test-skip guard requires of any diff.

**No evidence crossover, in both directions.** The record carries its own suite
label, `live_assurance`, and `LiveAssuranceRecord` refuses any suite
`ValidationGateKind` defines — so a `publish_gate` payload dropped into the
lane's directory raises rather than admitting anything. Symmetrically, a
`ValidationVerdictReceipt` carrying `live_assurance` certifies nothing, because
`ValidationGateKind.PUBLISH.produced` does not recognise the label. This is the
same discipline that stops an `agent_gate` pass reading as a publication pass
([#25]), extended to a third lane.

**What the assurance record authorizes.** `control/trusted_runtime_promotion.py`
admits a trusted-runtime promotion only for an artifact with a `PASS` record
naming that exact commit. Before this the rule was prose — "ship the runtime
you verified" — with nothing that could refuse. `trusted-runtime-promote
--head-sha <sha>` is the command form: exit `0` admitted, `1` refused with the
reason, `2` malformed request. It moves no pin; the pin and the promotion
procedure stay in issue #18.

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

So a gate run that does **not** pass also writes its output to a durable
destination in the primary checkout:

```
.issue-orchestrator/diagnostics/gate-failures/
    <issue-scope>--<issue-id>--<HEAD_SHA>--<suite>--<timestamp>/
        failure.json     # issue key, verdict receipt fields, exit code, timings
        stdout.log       # the run's stdout, verbatim
        stderr.log       # the run's stderr, verbatim
```

One store for every gate, not one per contract: the question a reader arrives
with is "why did *this candidate* fail", and the suite in the name is what
keeps two contracts' explanations for one candidate from erasing each other.
`failure.json`'s `type` is taken from the record's own suite
(`publish_gate_failure`, `agent_gate_failure`), so a diagnostic cannot name a
contract other than the one that ran.

The directory was `diagnostics/publish-gate-failures/` while the publish gate
was the only writer (#173 de-scoped it). Nothing migrates: the store is
diagnostic-only and nothing reads it programmatically, so an install that
predates the rename keeps its older publish-gate explanations under the old
name and writes new ones under this one. Look in both if you are chasing a
failure from that era.

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
  record, and nothing reads it to decide what the gate DECIDED.
  `Attempt.completed_evaluations` remains the only thing that decides that. A
  losing gate run must not be able to write anything that admits work.

There is exactly one reader, `CandidateGateDiagnostics.latest_failure`, added by
[#297] so the rework handed a failed publish candidate back carries the actual
failing test rather than a pointer into a reaped worktree. It finds the newest
bundle filed under `(issue, HEAD_SHA, suite)` — by name, because there is still
no pointer — re-checks the bundle's own receipt against the candidate and the
contract asked for, and returns the tail of each stream together with the
directory holding the whole of it. The read is **monotone in the refusing
direction**: finding a bundle authorizes nothing the receipt did not already
authorize, and failing to find one can only refuse a handoff everything else
admitted. So a hand-planted artefact still makes no work happen, and a deleted
one strands the candidate loudly instead of degrading the correction.

"Newest" means newest bundle that actually explains the failure, and the reader
is what decides that rather than its caller. Two kinds of bundle explain
nothing and both fall through to the one before them: one that cannot be read
at all, and one that reads cleanly but carries no output on either stream — the
latter repeats the receipt and adds nothing, which is exactly as useful to a
correction agent as a bundle that was never filed. A retried publish files one
bundle per run under the same `(issue, HEAD_SHA, suite)`, so stopping the search
on an empty newest one would discard an older run's real output and strand the
candidate for a human with the evidence sitting unread beside it. `None` from
`latest_failure` therefore means "nothing filed for this candidate explains
anything", and the handoff's fail-closed refusal is built directly on it with no
second predicate of its own to forget.

A pass writes no such artefact — its output stays where every passing run's
output has always stayed — and the trigger is the *verdict* rather than the exit
code alone, so a timeout is covered by the same seam with no timeout-specific
rule. A candidate with no canonical issue key (the manual-reprocess route)
files no diagnostic, for the same reason it files no receipt, and says so in the
log rather than skipping silently.

Whether a gate files here is a property of its **caller**, not of the contract:
a caller that holds the candidate's canonical issue key supplies the
destination, and one that does not cannot. So an agent's own `coding-done`
files nothing (it knows only its worktree, and its run directory outlives the
gate anyway), while the continuation's quick gate — orchestrator-side, holding
`(issue, A)` — files here. That one needs it most: it deletes its checkout the
moment the gate refuses, so it is not racing cleanup but running ahead of it.

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
| The exact-`A` review outcome | `Attempt.continuation_review_verdict` | `control/continuation_review_evidence.py`, promoted from the run's own verdict binding before its worktree is discarded |
| What the continuation run produced — the pull request, or that none was asked for | `Attempt.continuation_settlement` | `control/continuation_finalize.py`, from the `ProcessingResult` the run's own completion pipeline returned |
| How many runs the continuation has opened for this candidate | `Attempt.continuation_runs_used` | `control/continuation_run_open.py`, spent before a run is opened |

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

### Releasing the issue is not the same as handing the candidate back

Three phases say in their own documentation that the candidate returns to
ordinary rework — `EXIT_TO_REWORK`, `EXHAUSTED` and `RUNS_EXHAUSTED` — and
`ContinuationPhase.exits_to_rework` is where each one says so, declared per
member exactly as `live` is. Until [#297] the derivation dropped every non-live
phase from ownership and produced nothing else, so the lease was released and
no rework was ever admitted: a PR-backed candidate that failed canonical
publication validation and spent its same-SHA allowance sat stranded until a
human intervened, in an engine whose ordinary rework lane was idle beside it.

`control/continuation_rework_handoff.py` is the missing producer, and it owns no
policy of its own. The phase stays a derived predicate; the PR identity and
branch come from the existing open-PR owner (`look_up_open_pr_for_issue`); the cycle
number and the ceiling come from `control/rework_cycle_policy.py`, the single
rework-cycle owner `PRScanner` now decides through as well. What the handoff
adds is assembly: it files a `DiscoveredRework` (or a `DiscoveredEscalation`
when the ceiling is passed) carrying the failed candidate's SHA, the publish
gate's command and verdict, the intent the agent recorded, and the gate's own
failing output — so the correction needs no human relay — and the planner turns
that fact into the same `QueueReworkAction` the ordinary lane produces.
Composing that correction prompt out of the resolved evidence is a separate
concern and lives in `control/continuation_rework_feedback.py`; the handoff
decides *whether* a candidate may take a cycle, the feedback module decides what
the agent taking it is told.

**Reconciliation visibility is not work-admission authority.** The exits the
handoff receives come from a derivation that is board-wide by design and must
stay that way — ownership release names every lease the derived live set does
not, so an engine that looked at less than the whole board would report other
issues' running operations as finished and free them. [#297] attached this
work-admitting producer to the end of that sequence with no scope predicate, and
[#303] measured the consequence on a live engine: started with `--issue 301`, it
admitted, queued and *launched* ordinary rework for held issue #293, created its
worktree and rebased its branch, purely because reconciliation could see it. So
the first question the handoff asks about any exit is the engine's own actuation
scope, through `control/issue_scope.py`'s `EngineIssueScope` — the same owner
`QueueCache.is_outside_engine_scope` delegates to, so the two cannot drift about
what `--issue` means, and the handoff cannot form its own opinion from labels or
branch names. An out-of-scope exit is refused before any GitHub read and before
any collection the handoff can write to is touched, reported as an outcome with
reason `outside_engine_scope`, and — unlike the three strands — *not* published:
it names no stuck candidate, and announcing `rework.skipped` for a foreign issue
on every reconciliation would put back exactly the cross-issue traffic [#304]
removes. The narrowing binds the admission step and nothing before it: [#304]
requires that the held issue still be derived and still have its ownership
reconciled, which is what keeps the repair from trading a cross-issue mutation
for a cross-issue deadlock.

The output is the part that has to survive cleanup, and the receipt deliberately
carries none. `Attempt.validation_record_path` is no help either: it points into
the coder's run directory, inside a worktree that is usually already reaped by
the time an exit is derived. So the handoff resolves [#94]'s durable bundle for
that exact `(issue, SHA, suite)` through its owner and copies the failing output
into the correction context, with the bundle's path for the rest of the log.
`Attempt.publication_refusal` is what says an explanation is owed — a receipt in
which the publication contract refused this candidate — so an exit that reached
rework by another route (a reviewer asking for changes on a commit that passed)
owes nothing here and is not held to it. When an explanation IS owed and none
can be resolved, the handoff **strands the candidate** — `rework.skipped` with
reason `missing_failure_evidence` — rather than queueing a cycle whose prompt
would say "publication failed, go and find out why". That prompt is the human
relay [#297] exists to remove, and spending a rework cycle on it arrives back at
the same place one cycle poorer.

That evidence gate guards the *spending* of a cycle, so it is the **last**
question the handoff asks: after `ReworkCycleBudget.admit` has granted one, not
before. The ceiling keeps its precedence — [#297] requires that at exhaustion
today's escalation path fires and no new budget is introduced, so a candidate
that is simultaneously at the ceiling and missing its explanation escalates,
exactly as it would have with the explanation in hand. Asking the two questions
the other way round would swap an escalation a human is waiting on for a strand
nothing produces. Below the ceiling nothing changes: the refusal still happens
before any `DiscoveredRework` is filed and before any principal is spawned, so
no cycle is spent, and the same cycle is offered again on the next pass once the
evidence gap is closed.

The transition is `continuation -> ordinary rework on the same PR lineage`,
never `continuation -> issue release`: [#195]'s PR-backed shield is untouched,
the session-history claim stands, `QueueCache.abandoned_candidates()` still
excludes the issue, and no fresh coding session is created for it. Repetition is
bounded by what already bounds rework — the cycle owner refuses while anything
holds the issue, `OrchestratorState.record_discovered_rework` admits at most one
fact per issue per tick, and the `rework-cycle-N` label the launcher writes is
the durable counter a restart re-reads. No new budget exists.

Two rules keep that bound from costing GitHub reads. Every refusal decidable
from facts the caller already holds is reached before any read *unless something
outranks it*: `ReworkCycleBudget.already_held` takes whichever label set its
caller has for free — the PR's, for the sweep that found it by label; the
issue's, for the handoff that arrives holding a board issue — so a blocked
candidate is refused without a read on every pass. (The one free refusal
deliberately asked after the read is `missing_failure_evidence`, because the
ceiling outranks it; see the paragraph above and the one below.) The refusal
that genuinely needs a read, "there is no open PR", is a negative answer
`AdapterCache` does not cache, so the handoff remembers it per candidate and
drops the memo when the exit stops being derived.

What may enter that memo is deliberately narrow, and it is `OpenPrLookup` that
makes the distinction available: the PR port can *fail to answer* — a
rate-limited `/search/issues` call, a timeout — and `get_open_pr_for_issue`
collapses that into the same `None` it uses for "there is no open PR". A caller
that caches the answer must not, so the handoff reads through
`look_up_open_pr_for_issue` instead. A failed read refuses that pass only, as
`pr_read_failed`, and settles nothing: the handoff is built once per engine and
the exit keeps being derived, so a memoised outage would take the silent
short-circuit forever and re-open the [#296] gap from a recoverable error. **A
read that failed is not a fact**, which is this repo's fail-fast rule applied to
a cache.

The once-per-issue-per-tick rule belongs to the collection, so every producer
inherits it: the `needs-rework` sweep, the post-publish reconciler and this
handoff all write through `record_discovered_rework` /
`record_discovered_escalation`, and all four ask "is this issue already
claimed?" through `OrchestratorState.issues_with_claimed_rework`. Which fact
survives is decided by content rather than arrival order — the steady-state
refresh sweeps before it hydrates and startup hydrates before it sweeps, so a
fact carrying correction context supersedes one that does not, and nothing
supersedes a fact that already carries it. The three refusals that strand a
candidate with nothing downstream to retry it — `no_open_pr`, `no_agent_label`
and `missing_failure_evidence` — are published as `rework.skipped` rather than
only logged, with the same payload shape for all three (and for
`pr_read_failed`) so a consumer never has to branch on which one it got. The
third is the one refusal that deliberately follows the PR read, because the
ceiling outranks it; the read it pays for is a *positive* PR answer, which
`AdapterCache` does cache, so a permanently unexplainable candidate costs what
an admitted one costs rather than the uncached search `no_open_pr` avoids by
memo.

Those refusals are permanent by construction while the log line about them is
not, so each is **logged every pass and published once**: the exit is
re-derived on every reconciliation for as long as the durable facts stand, and
an event per tick would tell a consumer something changed when nothing has. The
announcement is remembered per `(candidate, reason)` and dropped by the same
pruning as the `no_open_pr` memo, so a candidate that leaves the exit and comes
back is news again — and a restarted process meets it for the first time. Only
the *announcement* is remembered: the decision itself is re-made every pass, so
a durable bundle that is restored, or written by a gate that ran after the
strand, is picked up on the next reconciliation rather than being locked out by
a cached refusal.

`control/continuation_scheduling.py` is the one hydration path: it derives,
reconciles, publishes, advances what this engine owns, admits the rework its
exits imply, and only then lets `QueueCache` evaluate eligibility. The handoff
runs last because it depends on the release that precedes it, and an unreadable
durable record admits nothing for the same reason it releases nothing.
Derivation runs inside the ownership owner's
own lock (`reconcile_derived`), which is what makes a stale snapshot unable to
release a newer claim. `control/continuation_runner.py` executes: it hands a
`RETRY_PENDING` candidate whole to [#139] — no second admission predicate and
no second allowance — and drives a passing one through the ordinary
`CompletionProcessor`, in a worktree verified to stand at exactly `A`. Opening
that run is its own owner, `control/continuation_run_open.py`: allowance,
checkout, provisioning, run assets, quick-validation evidence and the intent
that names it, in that fixed order, with one disposal rule for every refusal.

### The continuation's first reviewer needs evidence a coder turn never wrote

On the ordinary path the first reviewer is handed validation evidence its
**coder turn** produced: `coding-done` runs the profile's quick contract
through `AgentGate`, writes the record into the run directory, and names it on
the completion record. Everything downstream is a data dependency on that one
file — the completion record starts the review exchange, the pair mirror copies
the named record into pair and run scope, and a round cannot advance while
`review.exchange.loop.require_validation` is on and the mirrored record is
missing, stale or failing.

A continuation has no coder turn. Its completion record is synthesised from the
durable descriptor, whose fields are the agent's recorded intent and nothing
else — so before [#173] the exchange reached the reviewer pointing at a file
nothing had written, and the reviewer, told to trust that file, answered
`changes_requested` about the missing file rather than about the code.

`control/continuation_quick_validation.py` produces it instead, as **system
preparation with no model turn**, between the run assets and the intent that
names it:

- **The owner is the existing one.** It composes `AgentGate` over the contract
  `RunValidationContracts` resolves from the profile frozen onto this run's
  manifest — the same resolver the publication gate uses. Nothing there runs a
  command of its own and nothing writes a record.
- **Nothing is reused and nothing is synthesised.** `AgentGate` consults no
  cache and no durable evaluation history, so a candidate whose past quick
  verdict survives while its record died with the worktree gets a fresh run
  rather than a record invented from a receipt. The preparation executes or it
  refuses.
- **Nothing is retyped.** The gate carries no attempt identity, so it files no
  durable evaluation: the candidate's publication history is exactly what it
  was, and a `publish_gate` receipt still cannot satisfy a quick requirement.
- **The candidate is left as it was found.** The same
  `CandidateIntegrity` (`control/candidate_integrity.py`) checkpoint
  `WorktreeRunnability` takes around the operator's recipe is taken around the
  gate, so a preparation that moves `HEAD` or dirties tracked content is
  refused — as is one whose tracked dirt could not be *enumerated*, on either
  side of the gate: an unreadable read leaves the candidate unprovable, and an
  unprovable candidate opens no run. Independently of that, the record names
  the commit the gate *read*,
  which is the binding the exchange's pair mirror re-checks against the coder
  worktree's current `HEAD` before every round — evidence that does not name
  the candidate reads as stale there and refuses the round rather than passing
  silently.
- **The run is told what its gate found.** Producing evidence and recording
  that it exists are two steps, and the second goes through the same
  `ValidationEvidenceRecorder` an agent's `coding-done` records its own gate
  through: the typed outcome, the record path, the two logs and any JUnit
  reports, onto this run's manifest. A run whose outcome is unrecorded is not a
  run with less detail — `load_validation_failure_summary` reads the outcome
  first and returns nothing without it, so the session-diagnostics dialog, the
  run audit and the artifact list would all show a continuation run as one that
  never validated anything.

A refusal costs the whole run: no exchange starts, no pull request is created,
the checkout is removed, the durable publication history is untouched and the
[#149] run allowance stays spent — a start budget, for the reason provisioning
failures do not refund one either. Once the allowance is gone the ordinary
`RUNS_EXHAUSTED` derivation returns the candidate to rework.

What a coder finds waiting there is the durable gate-failure artefact described
above, filed under this candidate's `(issue, HEAD_SHA)` in the primary checkout
— **not** the run directory, and not a log line. Every path the gate wrote is
inside the checkout the refusal deletes, immediately and unconditionally, so
without that artefact a candidate that exhausted its allowance on a failing
suite would return to rework with an exit code and nothing else. The refusal
reason names the command that ran and the store the output went to; the output
itself is in `stdout.log` and `stderr.log` there.

A repository whose run profile configures **no** quick contract has nothing to
produce, so the record names no evidence — exactly what an ordinary coder turn
writes there. Whether a review may proceed without it stays
`require_validation`'s question, unchanged; config validation already refuses
`require_validation` with no `validation.quick.cmd`.

The step is assembled by `entrypoints/bootstrap_continuation.py` and reaches
the runner on `OrchestratorDeps`, as [#139]'s revalidation route does, so both
composition roots build the same one.

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

It reaches that finalizer only through
`control/continuation_review_evidence.py` ([#178]). A run whose completion
actually completed a review exchange settles **only** once that exchange's
exact-`A` `BoundReviewVerdict` has been read and promoted onto the attempt; a
binding that is missing, unlocatable, does not parse, or covers `A'` withholds
the settlement instead of being logged past. Before that, promotion could refuse
in any of those ways while the settlement was recorded anyway — the shape
observed in production as `continuation_review_verdict = null` beside
`continuation_settlement = pull_request_opened`, a terminal outcome asserting a
review nothing could evidence. A refusal is not a failure: the recorded intent
stays undischarged, so the operation stays live and the next reconciliation
re-enters the pipeline, bounded like every other fruitless run by [#149]'s run
allowance. Under the collision strategies that permit it — `new_branch` (the
default) and `reuse_open` — that re-entry finds and reuses whatever pull request
already exists, so the retry costs a run rather than a duplicate; a repository
configured `pr_collision: fail` refuses the second publication and reaches the
same allowance bound one pass later. A completion that ran **no** exchange is
unaffected — it never held review evidence, and settles from what it produced
exactly as before. That carve-out is read off the result's own
`review_exchange_completed`, not off the absence of a named run, so a result
claiming a finished exchange without naming one cannot reach the one outcome
that settles without evidence.

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

### A paused engine still disposes of what finished

An ordinary session that was already running when the pause took effect may
still reach terminal, and the disposal it earned — close its terminal tab,
remove its worktree under the configured cleanup policy — belongs to that
session's lifecycle, not to the new work the pause blocks ([#167]). That
disposal is planner-owned, and planning does not run while paused, so a paused
engine used to hold a finished session's worktree until an operator either
resumed (reopening continuation execution) or edited state by hand.

`run_tick` therefore runs one phase while the health gate reads `paused`:
`control/terminal_disposal.py`, the owner that also supplies the planner's
immediate-cleanup actions, so both paths dispose on identical terms. Its input
is the immediate-cleanup fact the completion handoff files for a terminal
session, never the deferred PR-reviewed queue — "has this PR been reviewed
yet?" is a live review-workflow decision and stays behind the pause gate, which
is also why the paused pass reads nothing from the repository host. Tech-lead
artifact holds still withhold disposal, an ordinary coding worktree is still
removed non-forced, and the fact is consumed only when its disposal actually
happened, so a resume finds neither a duplicate nor a silently dropped cleanup.

Disposal is still not cancellation. Ordinary cleanup tears down the issue's
review exchange on its way past; while paused, anything still live for that
issue predates the pause and is finishing on its own terms, so
`ActionApplier.dispose_terminal_session` refuses rather than killing it —
asking `has_live_issue_review_exchange`, the non-mutating counterpart of the
cancellation it guards. A refusal defers the disposal rather than failing it.

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
  silent edit to the change under test, and so is a read that *failed* on
  either side of the recipe: an enumeration that could not be performed leaves
  the candidate unprovable, which is refused rather than read as "nothing
  changed". The prerequisites themselves are build
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

### The other principal that must not run the gate

A `planning_investigation` Tech Lead is refused the same commands, for an
unrelated reason. Its scratch worktree *is* fully provisioned — the gate would
run — but the run's job is to prepare one bounded issue from source and staged
governing evidence, and the code-candidate publication gate produces no
planning verdict. R22 Pilot 4 was told that in its prompt, ran
`make validate-pr-raw` anyway, spent about seventeen minutes inside a gate its
sandbox could not satisfy, and returned BLOCKED without the bounded
`create_issue` it was launched to produce (#289).

The barrier is the same shape as the reviewer's, through Codex's mechanism
rather than Claude Code's:

- **One classifier.** Which entry points count as a gate is declared once, in
  `infra/hooks/gate_commands.py`. The reviewer's hook renders it as command
  regexes; the planning installer renders it as Codex `prefix_rule` argv
  patterns. Adding a gate entry point teaches both; removing one breaks both,
  which is how the shared link is testable.
- **Launch-scoped.** Codex resolves a linked worktree as its own project root,
  so `.codex/rules/planning-gate.rules` inside the run's *disposable* scratch
  worktree is read by that run and by nothing else. The product checkout's
  rules, the repository's shared Codex policy, and the operator's `~/.codex`
  are all untouched, so an ordinary Codex Actor's validation still runs. No new
  repository root is trusted: the policy loads under the existing #215 grant,
  which names the common repository root.
- **Composed, not substituted.** The installer places the shipped
  `orchestrator.rules` beside the planning policy, and Codex loads every
  `.rules` file in that directory, so `git push --no-verify`, commit-hook
  bypass, `gh pr merge` and `gh api` stay denied. The copy is not taken on
  trust: `git push --no-verify` and `gh pr merge` are put to the checker
  against that file too, in the same pass, so a safety policy that arrived
  empty or superseded fails the launch.
- **Established, not assumed.** Before the session spawns, the installer asks
  `codex execpolicy check` to classify pinned samples — `make validate-pr-raw`
  and a pytest-shaped command must come back `forbidden`; `git log`, `rg` and
  `cat` must not. A policy that does not verify, or a checker that cannot
  answer, raises `PlanningCommandGuardError`, and the ADR-0031 launch owner
  (`control/tech_lead_session_policy.py`) turns that into a failed launch. A
  Codex planning session never spawns unguarded.
- **Codex only, and it says so.** `GUARDABLE_PLANNING_PROVIDERS` is the one
  place both the installer and the launch owner read. A planning run on another
  provider — or on an agent configured with a raw `command` and no provider at
  all — gets no policy file and a WARNING naming the gap, rather than a
  decorative one.

**What a guarded planning launch leaves on your machine.** The policy files
themselves go away with the disposable worktree. One thing does not: to keep
them out of `git status`, the launch adds two lines to the repository's
**shared** `.git/info/exclude` in the product checkout —

```
.codex/rules/planning-gate.rules
.codex/rules/orchestrator.rules
```

`info/` is a common-dir path in git, so a linked worktree's own
`.git/worktrees/<name>/info/exclude` is never read and the shared file is the
only one that takes effect. The write is idempotent, so this is two lines once,
not two per launch, and both name orchestrator-owned untracked files the
repository does not track (`orchestrator.rules` is what `io hooks install`
already writes at the product root; `planning-gate.rules` never exists there).

They are **not removed** at teardown, deliberately: the file is shared, so
dropping the entries when one scratch worktree is deleted would unhide a
concurrently running planning launch's policy files, and would still leave them
behind whenever a run dies before teardown. If you want them gone, delete the
two lines by hand — nothing depends on them once no planning run is live.
Nothing else outside the worktree changes: no tracked file, no
`.codex/rules` in the product checkout, no `~/.codex`, no trust, sandbox,
approval or credential state.

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

**These ignores classify by path, and one guard therefore does not use them.**
`CandidateIntegrity` (`control/candidate_integrity.py`), the postflight that
proves provisioning and continuation quick validation left the candidate alone,
asks `WorkingCopy.list_dirty_files(..., "tracked")` and judges every path it
gets back. Untracked runtime output — a suite's JUnit XML, a coverage database,
a setup step's `.venv` — never reaches it, which is the concession
[#153]'s exact-candidate contract makes. A *tracked* modification is the thing
that contract forbids, so it stays visible there even when its path matches a
built-in prefix or an operator pattern: dropping it by path would admit a
preparation that mutated the candidate. `runtime-ignore` is unchanged
everywhere it is consulted — the completion dirty guard, the pre-push check and
agent `git status` — and this postflight simply asks a narrower question.

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

### Live-assurance record format

Location: `.issue-orchestrator/live-assurance/<HEAD_SHA>.json` — a different
directory, because it is a different kind of evidence.

Record fields:
- `schema_version`
- `suite` — always `live_assurance`. Any suite `ValidationGateKind` defines is
  refused on both write and read, so this file can never be a publication
  receipt and a publication receipt can never be read as one of these.
- `head_sha` — the exact artifact the lane ran against, resolved from
  `--live-assurance-root` itself so it cannot describe another checkout
- `outcome` — `pass`, `security_fail` or `inconclusive`
- `detail` — why, preserved verbatim; never empty. An `inconclusive` reason is
  reduced to its first line; a `security_fail` keeps its full multi-line
  evidence, because that record is what an operator reads after the probes are
  gone
- `working_tree_dirty` — whether the checkout had uncommitted changes when the
  lane ran. Required, never defaulted, and `true` makes the record assure
  nothing: the probes exercised a tree the SHA does not name
- `probes_executed` — how many live-agent probes completed a call phase.
  Required, an `int` and never a `bool`, and a `pass` naming `0` is refused on
  both write and read. A narrowed selection (`make test-live-assurance
  PYTEST='… -k codex'`) files an honest record, and this is the field that lets
  a reader tell it from a full run; deciding what "the full probe set" is for an
  artifact is a separate question this record deliberately does not answer
