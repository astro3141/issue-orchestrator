# Foundation validated-work disposition — minimal domain contract (rev 5)

**Status: FROZEN, 2026-08-12; amended 2026-08-14 (rev 4, §2/§4/§11 — execution
principal).** This file is the authority for the contract.
Contract only — no implementation, none proposed. Persistence, labels and UI are
decided only where the contract cannot be stated without them.

Amendments are contract changes: revise here first, and say why. An
implementation that disagrees with this file is wrong until this file says
otherwise.

**Boundary, measured 2026-08-12 (spike, issue #12):**

```
first publish-side mutation = remote branch push, ahead of PR creation
```

**Verdict at freeze.** Architecture PASS. Domain contract PASS. Capability gaps
explicit and bounded (§10). No unresolved architecture decision blocks
implementation. Reached over three revisions; each correction is recorded inline
where it changed a rule, so the reasoning survives rather than only the result.

---

## 1. Ownership boundary — this state machine starts at HELD

The existing IO lifecycle owns `BUILDING → VALIDATED → REVIEWED`. This contract
does **not** re-own them; treating them as states of a new machine would create
a second source of truth for work already tracked.

```
existing IO lifecycle
  Actor → validation → independent review
                     │
                     │ ADMISSION: exact A + valid evidence  (§4)
                     ▼
        ┌─────────────────────────┐
        │ HELD_FOR_APPROVAL       │  ← new disposition owner begins here
        └───────────┬─────────────┘
                    │ Gate 2: human approves
                    │ (repo, target_ref, base, A, evidence)
                    ▼
              ┌──────────┐
              │ APPROVED │
              └────┬─────┘
                   ▼
            ┌─────────────┐
            │ FINALIZING  │  deterministic B; parent(B)=A
            └────┬────────┘   diff(A,B) ⊆ allowed finalizer surface
                 ▼
            ┌─────────────┐
            │ PUBLISHING  │  expected remote state recorded BEFORE the write
            └────┬────────┘   conditional (CAS) remote ref transition
                 ▼
            ┌─────────────┐
            │ PUBLISHED   │  terminal
            └─────────────┘

  stale A / evidence / spec        → HELD_FOR_APPROVAL   (approval invalid)
  operational or publication fail  → DISPOSITION_FAILED  (approval retained;
                                       retry-validity derived, see I11)
  restart while PUBLISHING         → reconcile remote → PUBLISHED | DISPOSITION_FAILED
```

Two distinct failure destinations. Collapsing them loses the question a human
actually needs answered: *was my approval invalidated, or did execution fail?*

---

## 2. Terms

- **A** — the candidate commit. Exactly one commit.
- **base** — the published tip A was built on.
- **B** — the finalization commit. Content is a function of `(A, base)`.
- **target_ref** — the remote ref publication writes.
- **evidence set** — the complete admitted evidence whose content establishes
  I2a–I2c: validation evidence and review evidence, **including the actor and
  reviewer execution principals**. Commit-scoped evidence names or is bound to
  A. Execution provenance not used by §4 is not required to be part of this set.

*Restated rev 4.* This was previously "validation record and review record, each
naming a SHA" — an enumeration of two artifacts. Once I2c compares principals,
that enumeration leaves the principals outside §5's `evidence_digest`, so an
approval could survive replacing the actor/reviewer principals with a different
still-distinct pair: I2c would still pass, the digest would be unchanged, and
the human would remain bound to an approval whose authority basis had moved.
Defining the set by *what it establishes* rather than by which files carry it
closes that, and keeps the contract free of any particular artifact layout.
- **execution principal** — the orchestrator-assigned logical agent identity
  under whose authority a role executes. This is what I2c compares.
- **execution provenance** — attributes describing how a principal's execution
  happened, such as provider, model, process, session and run identifiers.
  Recorded provenance is retained for audit; provenance does not define
  principal identity and is not compared for I2c.

*Added rev 4.* I2c previously said "reviewer identity is distinct from the
actor" without saying what makes two identities the same, and an implementation
supplied the answer by accident: a fingerprint over `(agent label, provider,
model)`. That is provenance mixed into identity, and it fails in both
directions. Two roles configured as separate reviewing principals but running
the same provider and model would read as one identity — which is precisely the
arrangement this fork operates under, so independent review would be
unrepresentable. Conversely one principal whose model changed between the two
runs would read as two, admitting work that reviewed itself.

The distinction is deliberately stated without naming any implementation
concept: what plays the part of a principal is an implementation question, and
the contract does not care, so long as the thing compared is authority and not
execution detail.

---

## 3. Structural invariants

**I0a — `parent(A) == base`.** A is built directly on the base the approval will
name. Without this, "base moved" is undetectable by inspecting A.

**I0b — `parent(B) == A`.** B is a real commit whose parent is A.

**I0c — `target_ref` never points to A.** Publication performs no intermediate
`base → A` ref transition. The only successful publication transition is:

```
target_ref:  base → B        with parent(B) == A
```

Stated over ref transitions, not over object presence: whether A's object exists
in the remote object database mid-push is not the contract's concern, and cannot
be controlled anyway.

**Corollary — B is never skipped.** If `(A, base)` produces no content change, B
is still created, with an empty tree delta and its canonical message. One path,
one authority. (Rev 1 permitted "publish A alone" here; that contradicted I0c
and is removed.)

---

## 4. Admission — evidence validity, not merely coherence

A disposition may enter `HELD_FOR_APPROVAL` only if **all** hold:

**I2a — coherence.** `validation.head_sha == review.reviewed_sha == A`.

**I2b — validation validity.** `validation.passed == true`, and
`validation.profile` equals the profile frozen for the role at run start.

**I2c — review validity.** The review outcome is approval with no blocking
findings, and the **reviewer execution principal is distinct from the actor
execution principal**.

**Review authority is the admitted review evidence bound to the exact candidate
A.** It is not a label. A scheduler-facing label such as `code-reviewed` is a
*projection* of that evidence for scheduling, and carries no commit identity; it
cannot establish I2c on its own, and its presence on a work item whose candidate
has since changed says nothing about the current candidate. Where a projection
and the bound evidence disagree, the evidence is authoritative and the
projection is stale.

Distinctness is a comparison of principals only. Two executions of the same
principal are the same principal however much their provenance differs, and two
distinct principals stay distinct however identical their provenance is.

Coherence alone admits a `passed=false` record whose SHA happens to match. Three
matching SHAs are not evidence that anything succeeded.

---

## 5. Approval binding and invalidation

An approval record binds **all** of:

```
(repository, issue, target_ref, base, expected_remote_head, A,
 evidence_digest, finalizer_spec_digest, approver, granted_at)
```

`expected_remote_head` is what `target_ref` pointed at when approval was granted.

**Evidence is bound immutably, not by reference.** `evidence_digest` is a
content hash over the admitted evidence set (§4), not a set of record ids. An id
can be re-pointed at a later record; a digest cannot. If the evidence content
changes, the approval no longer matches what was approved.

**`finalizer_spec_digest`** pins the finalizer contract version and its declared
allowed surface (§6). A human approving publication is approving *what will be
constructed*, not only *what was reviewed*. If the finalizer changes, B changes,
and the approval no longer describes the outcome.

**An approval is valid only while all of these remain true:**

```
A                        == approved_A
resolve(target_ref)      == expected_remote_head == base
evidence_digest          == approved evidence_digest
finalizer_spec_digest    == approved finalizer_spec_digest
evidence                 still satisfies §4
```

Stated as a standing predicate rather than a list of events, because that is
what it is: validity is re-derived whenever it matters, not tracked by
remembering what happened.

Two corrections this makes explicit. `base` and `expected_remote_head` are SHAs
recorded in the approval — they cannot move; what moves is `resolve(target_ref)`.
And a changed finalizer spec invalidates the approval: rev 2 bound
`finalizer_spec_digest` into the record but omitted it from the conditions, so a
finalizer could be swapped under a live approval.

**Invalidation is not revocation.** The human withdrew nothing; the thing they
approved no longer exists.

**A is immutable within one disposition instance.** If rework produces `A'`, the
disposition for `(issue, A)` remains historical and cannot authorize `A'`.
Admission of `A'` creates or activates a distinct `(issue, A')` disposition,
beginning at `HELD_FOR_APPROVAL`. The old approval record is neither destroyed
nor inherited:

```
(issue 42, A1) → approval X → invalidated, retained as history
(issue 42, A2) → HELD_FOR_APPROVAL → approval Y
```

**The same identity rule governs review authority.** A review bound to A is not
revoked, deleted, or rewritten when the candidate becomes `A'`. It remains
attributed to A as history, and it does not apply to `A'`. `A'` earns review
authority only through its own admitted evidence. Removing a past review to
"clear the way" for a successor destroys history to change a scheduling
condition, which the projection rule above already covers: the projection may be
recomputed, the evidence may not.

**Corollary.** Any rework after approval invalidates it. There is no
"approved, then adjusted" path — that property is what makes Gate 2 mean
anything.

---

## 6. Deterministic finalizer

**May:** construct B from `(A, base)`; perform the single publication
transaction; refuse, moving to `DISPOSITION_FAILED` with a reason.

**May not:** modify A; choose B's content by judgement, model call or heuristic;
retry with different content; publish when any precondition is unmet.

**I4 — publication authority is exclusive.** Neither the actor that produced A
nor any agent may publish. Only the finalizer transitions toward `PUBLISHED`.

**I5 — determinism.** Same `(finalizer_spec_digest, A, base)` ⇒ **same B commit
object, same SHA.** Keyed on the digest the approval binds (§5), so the property
and the authorization are stated over the same inputs. This requires author and committer metadata
to be derived deterministically from `(A, base)` — a wall-clock committer
timestamp alone breaks it while leaving tree and message identical. If a project
cannot fix that metadata, it must instead declare the weaker property (same tree
+ canonical message) explicitly; it may not claim SHA determinism.

**I6 — finalizer surface.** `diff(A, B)` ⊆ the project-defined allowed finalizer
surface. "The finalizer does not modify A" is insufficient: B could re-modify
semantic source and the result would still satisfy it. The allowed surface is
declared per project; this contract requires only that one exist and be checked.

---

## 6a. Publication-side mutation and offer-for-review

**Every remote branch push is a publication-side mutation.** Pushing changes
state other parties observe; nothing about the pusher's intent makes it local.

**Offering a candidate for review is a separate act.** The two are not the same
event and must not be inferred from each other.

A completion that ends `blocked` or `needs_human` pushes to preserve work and
asks a human a question. That push is permitted **without** a publish-gate PASS:
holding a question to the publish contract would replace the question with a
validation failure. So for such a push:

| | |
|---|---|
| publication-side mutation | **yes** |
| offer-for-review | **no** |

**A candidate arriving this way holds zero positive review authority.** The
resulting head has no admitted review evidence bound to it, and a projection
left by an earlier candidate is not evidence for it (§4). It becomes
review-eligible only when a **fresh publication-gate PASS exists for that exact
SHA**. The absence of a recorded refusal is not a grant.

This holds regardless of whether a pull request was already open when the push
landed. "Opens no PR" describes the pusher's request, not the state of the
branch it wrote to.

## 7. Publication and crash recovery

The largest omission in rev 1. A remote write is not atomic with the local
record of having made it.

**Gate 2 freshness.** At approval time, and again as the write's own
precondition, the following must hold as a single equality:

```
target_ref == expected_remote_head == base
```

Three-way, not pairwise. `target_ref == expected_remote_head` alone permits an
approval whose base is some other commit; `expected_remote_head == base` alone
says nothing about where publication will actually land. Together with
I0a (`parent(A) == base`) this makes A a fast-forward of the ref it publishes to.

**I7 — conditional publication (CAS).** Publication succeeds only if
`target_ref` still equals `expected_remote_head` at the moment of the write. A
read-then-write sequence — `ls-remote`, compare, push — leaves a window and does
not satisfy this. The precondition must be carried *by* the write, naming the
expected value explicitly.

**I8 — durable intent before the write.** Before any remote mutation, the
disposition durably records `(target_ref, expected_remote_head, A, B)`. This
record is what makes recovery decidable.

**I9 — recovery is by remote observation, and fails closed.** Entered after a
restart in `PUBLISHING`, **and after any ambiguous write outcome** (I11):

| Observed `target_ref` | Conclusion |
|---|---|
| `== B` | publication succeeded → `PUBLISHED` (recovered) |
| `== expected_remote_head` | no publication occurred → safe retry, if §5 still holds |
| anything else | **fail closed** → `DISPOSITION_FAILED` |

The third row is the important one. An unexpected value means another writer
intervened; guessing is how a partial state becomes a wrong one.

**I10 — no partial publication, stated as reachability.**

After a successful publication:

```
target_ref == B
parent(B)  == A
```

Foundation publication never performs an intermediate `target_ref → A`
transition.

If publication does not succeed, the orchestrator makes **no claim that
`target_ref` is unchanged** — another writer may have moved it, which is
precisely the case a CAS rejection reports. The observed remote state is
classified exclusively by I9.

In all cases this workflow never establishes `target_ref == A`. The only
successful ref transition it performs is:

```
expected_remote_head (= base) → B
```

"Visible" was too loose: a commit can exist in the remote object store, or be
reachable from some other ref, without being reachable from the ref being
published. Reachability from `target_ref` is the property that decides whether
publication happened.

**I11 — failure classification: a transport result is not an outcome.**

The decisive rule: **once a remote write has been started, the transport's
result may not be read as the write's result.** A lost response, a timeout, or
the process dying says nothing about whether the CAS was applied. The remote is
the only thing that knows.

```
client                       GitHub
  │                            │
  ├── CAS  base → B ─────────> │
  │                            │  applied
  │        response lost   X   │
  │ <──────────────X────────── │
```

The client observes a network error; `target_ref` is `B`. Concluding "failed,
nothing was written" leaves a published B recorded as a failure — the exact
outcome this contract exists to prevent.

| Situation | Disposition | Approval |
|---|---|---|
| Failure **before** any remote write — finalizer refuses, determinism or surface violated, preconditions unmet | `DISPOSITION_FAILED` | retained; retry-valid if §5 still holds |
| **Definite expected-value / CAS-precondition rejection** — the server answered, and refused *because the precondition did not hold* (distinct from permission denied, rate limiting, or any other refusal, which are ordinary failures) | `DISPOSITION_FAILED` | retained as history; **not retry-valid** — `target_ref` moved, so §5 no longer holds |
| **Ambiguous** outcome — network loss, timeout, process death after the write was attempted | **remain / recover as `PUBLISHING`** | undecided until reconciliation |

Ambiguity is not a failure category. It routes to I9, which asks the remote.

**I11a — retained ≠ retry-valid.**

```
approval retained  ≠  approval still valid
```

`DISPOSITION_FAILED` always retains the approval as historical evidence. Whether
it remains **retry-valid** is derived from §5, never assumed. A definite CAS
rejection means another writer moved `target_ref`; a retry under the same
approval would publish against a base no human approved.

**I12 — durability.** `HELD_FOR_APPROVAL`, `APPROVED`, `PUBLISHING` and
`DISPOSITION_FAILED` survive restart, crash, and worktree removal, reconstructed
from storage rather than re-inferred from labels or branch names.

---

## 8. Deliberately not decided here

- **Claim/lease behaviour during hold.** A human may take a day to answer Gate 2.
  An active execution lease and durable disposition ownership plausibly have
  different lifetimes. The contract requires only that **no new claim concept is
  introduced**; whether the existing claim is held, released, or renewed during
  `HELD_FOR_APPROVAL` must be decided by reading the actual claim lifecycle, not
  asserted here. (Rev 1 asserted "held work is still claimed work" — premature.)
- Label spelling, UI surface, where the finalizer runs.
- How B's content is computed for any particular Foundation kind.

---

## 9. Decisions the contract does force

- **Durable disposition state** keyed by `(issue, A)`, extending the store that
  already holds run/task state. A second store answering "what is the
  disposition of this work" would be a second source of truth.
- **A blocking-class, human-visible signal** while held: scheduling must not
  resume, and a human must see that a decision is owed. Blocking-class semantics
  are required; a new label vocabulary is not.

---

## 10. Checked against existing IO primitives

Source-checked against the trusted runtime (`81c11ae1`).

| Requirement | Existing primitive | Verdict |
|---|---|---|
| Gate 1 scope authorization | `proposed-tech-lead` blocking disposition | **satisfied** — spike measured blocking before `claim.acquired` |
| Role routing | agent label → agent config | **satisfied** — measured |
| Role-specific validation contract | `validation_profile` (#7059) | **satisfied** — measured |
| Validation evidence bound to a commit (I2a, I2b) | validation record carries `head_sha`, `passed`, `profile` | **satisfied** |
| **Conditional remote write (I7)** | Publication pushes use `--force-with-lease` **with no expected value** (`adapters/git/git_cli.py:193`, `execution/git_push_operations.py:150`). Bare `--force-with-lease` compares against the local remote-tracking ref — whatever the last fetch happened to record — not against the `expected_remote_head` the approval named. `ref_claim_adapter` does implement ref CAS, but for **claim refs**, and it is not a publication port | **GAP** |
| Durable state across restart (I12) | run/task state stores, validation records keyed by SHA | **satisfied in mechanism** |
| Blocking-class human signal (§9) | existing blocking-class labels | **satisfied** |
| Exact-SHA verdict binding (I2a, I2c) | closed after the freeze by `613c66d8` (#15 / PR #16): `execution/review_exchange_records.py` writes `review-verdict.json` pairing the verdict with `reviewed_sha`, observed live. The orchestrator, not the reviewer's own JSON, names the SHA | **satisfied in mechanism** |
| **Whole-evidence durability (I12)** | the verdict binding and the validation record are written inside the session worktree and do not survive its cleanup; the attempt record in the primary checkout survives and carries `candidate_sha`. Evidence that cannot be read back cannot be admitted | **GAP — #33 prerequisite 1** |
| **Held-before-publish disposition state** | `awaiting-merge` exists but begins *after* publication (`post_publish_*`, reconciler) | **GAP** |
| **Approval bound to exact A** | no approval record of any kind | **GAP** |
| **Deterministic finalizer** | no post-approval deterministic step; `merge_queue` orders merges, it does not construct commits | **GAP** |

### Two findings worth stating precisely

**The review gap is no longer the absence of an exact-SHA verdict binding; it is
that the binding does not outlive the worktree that wrote it.** Not
"review is PR-bound" — that framing describes an association IO happens to
maintain and invites the wrong fix (decouple from PRs). The contract's
requirement is narrower and harder: a verdict must be durably paired with the
exact commit it was rendered against, so that admission (§4) and approval (§5)
can check it and so that any change to A demonstrably invalidates it.

Validation already has this shape — its record names `head_sha` and `passed`.
Review has no equivalent. The materials exist (`persistent_session_exchange`
computes `current_head_sha`, and reads `(head_sha, passed)` from validation
records); what is absent is the record.

**The CAS gap was mis-scored in rev 2 and is corrected above.** A CAS primitive
existing *somewhere* in the codebase does not satisfy an invariant about the
publication path. `ref_claim_adapter` guards claim refs; the publication push
uses bare `--force-with-lease`, whose expected value is the local
remote-tracking ref rather than the approved `expected_remote_head`. Under
concurrent movement those differ, which is exactly the case I7 exists for.

---

## 11. Minimal deterministic acceptance tests

One per invariant. Each decidable without a live model.

| # | Test | Passes only if |
|---|---|---|
| I0a | Admit with `parent(A) != base`. | Refused. |
| I0b/I0c | Finalize where `(A, base)` yields no content change. | B exists with `parent(B) == A`; the only observed ref transition is `base → B`. `target_ref` is never observed pointing at A. |
| I2a | Admit with `validation.head_sha != review.reviewed_sha`. | Refused, naming the mismatch. |
| I2b | Admit with `passed=false` but matching SHAs; separately, with a profile other than the frozen one. | Both refused. |
| I2c | Admit with reviewer principal == actor principal; separately with a blocking finding present. | Both refused. |
| I2c | Admit with reviewer principal == actor principal while provenance differs (different provider, model, session or run). | Refused — provenance does not create a second principal. |
| I2c | Admit with distinct reviewer and actor principals sharing the same provider/model configuration. | Admitted — matching execution configuration does not collapse two principals. |
| §5 | After approval, replace the actor/reviewer principal evidence with a different still-distinct pair while A and the verdict are unchanged. | Approval invalid — the evidence digest changed. |
| I4 | Actor role attempts publication while `APPROVED`. | Refused on authority, not on ordering. |
| I5 | Run the finalizer twice on identical `(A, base)` in isolated trees. | **Identical B SHA**, not merely identical tree. |
| I6 | Finalizer produces a B touching a path outside the declared surface. | Refused. |
| I7 | Advance `target_ref` between approval and the write. | Publication refused by the write's own precondition — not by a preceding read. |
| I8/I9 | Kill the process after the remote write but before the local record. Restart. | Remote observation drives the outcome: `==B` → `PUBLISHED`; `==expected` → retry; anything else → `DISPOSITION_FAILED`. |
| I10 | Fault-inject an **ambiguous** transport failure around publication — response lost after the write may have been applied. | The implementation does **not** infer success or failure from the transport result. Reconciliation determines the outcome from `resolve(target_ref)` per I9. Assert on the decision path, not only the final state. |
| I11 | Three cases, and they must not collapse: (a) finalizer refuses before any write; (b) **definite CAS-precondition rejection** after moving `target_ref` concurrently — not a permission or quota refusal; (c) response lost after the write was attempted. | (a) `DISPOSITION_FAILED`, retry-valid while §5 holds. (b) `DISPOSITION_FAILED`, approval retained but retry **refused**. (c) **`PUBLISHING`**, reconciled per I9 — not classified as failure. |
| I12 | Reach `HELD_FOR_APPROVAL`, restart, remove the worktree. | State reconstructs from storage; nothing inferred from labels or branch names. |

**Three tests that matter more than the rest:**

- **I0c/I2b can rot silently.** A regression looks like success — work is
  published, a human sees a PR. Assert on a mutation-recording remote and on
  *zero* ref transitions, not on "no PR appeared": a pushed branch would still
  pass that.
- **I5 must compare SHAs.** A finalizer producing semantically equal but
  textually different B has already left determinism; the drift surfaces much
  later.
- **I8/I9 is the only test of the crash window.** It is also the one most likely
  to be skipped as awkward. Without it the recovery table is decoration.
- **I10/I11(c) is the one that looks like pedantry and is not.** An
  implementation that treats a lost response as "the write did not happen" will
  pass every other test here while leaving a published B recorded as a failure.
  The assertion has to be on the decision path — that the transport result was
  never consulted as an outcome — because the final state can look correct by
  luck.

---

## 12. Out of scope

- Foundation *semantics* — what makes a change authority-bearing. Per project.
- Gate 1 policy — who may approve scope, on what evidence.
- What a given project's B contains.
- Upstream submission. Upstream #7024 covers the general capability; track separately.

---

## Revision history

**rev 5** — review authority is the admitted evidence bound to exact candidate
A; scheduler-facing labels are projections and cannot establish I2c alone (§4).
The §5 identity rule is extended to review authority: a review bound to A is
retained as history and never applies to `A'`. New §6a separates
publication-side mutation from offer-for-review, permits the `blocked` /
`needs_human` preservation push without a publish-gate PASS, and states that the
head it produces holds zero positive review authority until a fresh
publication-gate PASS exists for that exact SHA. §10's exact-SHA verdict-binding
row is corrected — that gap was closed by `613c66d8` after the freeze — and the
remaining review-side gap is restated as whole-evidence durability (#33
prerequisite 1). Decided in #50.
