# SELF_HOSTING_READY — 2026-08-12

Issue-Orchestrator can develop Issue-Orchestrator on this fork, through the
ordinary Actor → validation → review → PR → human-merge path.

This is a milestone record, not a claim about everything. Read the boundary at
the end before relying on it.

## Pins at declaration — 2026-08-12

| | Path | Commit |
|---|---|---|
| **Trusted runtime at declaration** | `~/io-runtime-r1/issue-orchestrator` | `81c11ae1` |
| Predecessor (preserved) | `~/io-tools/issue-orchestrator` | `74575869` |
| Product `main` at declaration | `astro3141/issue-orchestrator` | `7f16c3ed` |

**Every value in this table is the 2026-08-12 milestone's, not the current
state.** `81c11ae1` was the trusted runtime *at that declaration*; the trusted
pin has moved since, and this record deliberately does not follow it. For the
current trusted runtime, read issue #18,
<https://github.com/astro3141/issue-orchestrator/issues/18> — the single source
of truth for pins. Do not copy a current SHA into this file.

**At that declaration the runtime pin was behind product `main`, and that was
correct.** `81c11ae1` is the build that actually ran the Stage 6 canary.
Rebuilding from a newer `main` would have promoted something no canary
exercised. See the runbook — now issue #18 — "Promotion: ship the runtime you
verified".

R0 stays on disk. Rollback is a path change, not a rebuild.

## What was established

### The invariant

```
validated candidate filesystem == candidate Git HEAD
```

Before #6 this did not hold in IO-managed worktrees of this fork. The
orchestrator planted its own copies of
`src/issue_orchestrator/entrypoints/cli_tools/*.py` into every worktree and
marked them `--skip-worktree`. In a foreign repository those paths are runtime
and nothing else; here they are product source, so the planted copies shadowed
the files a candidate was changing while git reported the tree clean.

Fixed in #6 (PR #7): ownership is decided **before** planting, once for the
directory, by asking whether the target repository tracks the path.

### Stage 6 — canary, issue #8

A deliberately self-proving probe: the candidate modifies tracked
`cli_tools/coding_done.py`, so a planted copy and the candidate version differ
and the comparison is not vacuous.

| Check | Result |
|---|---|
| candidate modifies tracked `cli_tools` source | 1 file |
| `skip-worktree` bits on repo-owned paths | 0 |
| stale `cli_tools` entry in `info/exclude` | none |
| worktree disk SHA == candidate HEAD blob (`C`) | equal |
| every tracked `cli_tools` file vs its HEAD blob | equal |
| reviewer read that tree | review-exchange session, actor tree |
| validation record `head_sha` == candidate HEAD | equal |
| canonical publish gate | `passed=true`, `exit=0` |
| R1 pin / R0 HEAD / R0 dirty / R0 `src/` | unchanged |

The strongest single piece of evidence is the agent's own test:

```
tests/unit/test_agent_done.py::TestPlantedCodingDoneCopyIsDetected
    ::test_a_planted_copy_fails_the_source_id_value_check  PASSED
```

It pins the expected id as a **literal in the test file** rather than importing
it from the module under test — a planted copy would otherwise supply both
sides of the comparison and agree with itself. A green gate therefore witnesses
that the graded tree is the tree the branch contains.

Reproduce with `sh docs/selfhosting/canary-verify.sh <issue>`.

### Stage 7 — promotion

R1 promoted with provenance recorded before and after; all values identical, and
the promoted commit equals the commit that ran the canary.

### Stage 8 — ordinary work, issue #10

One unremarkable task (runbook update) on the promoted runtime, start to finish:
Actor → validation → review-exchange → PR #11 → human merge. Publish gate
`passed=true`, `exit=0`. Nothing needed manual recovery.

Stage 6 proved the invariant. Stage 8 asked whether routine operation is boring,
and it was.

## Boundary — what this does NOT establish

- **Not a Foundation-lane verdict.** No privileged Foundation workflow, Gate 1 /
  Gate 2 approval path, or rebaseline finalizer has been exercised here. That is
  the next milestone, not this one.
- **Not upstream-ready.** Nothing here has been prepared for or submitted to
  `issue-orchestrator/issue-orchestrator`. The #6 fix is downstream only.
- **`tests/e2e` remains outside the gate.** It needs `gh auth` and
  `E2E_TEST_REPO`, and with that unset its fixtures write into the current
  repository. Live E2E paths are unverified here.
- **Single operator, single machine.** Concurrency is 1 and every requirement in
  the runbook's environment table is host state, not repository state.
- **The canonical gate is not fully deterministic.** Closing this batch, a
  docs-only commit failed `test-web` with a 30s Playwright timeout
  (`Locator.select_option` on a dashboard run row); the same commit passed the
  same target 56/56 on an isolated rerun. Treat a lone `test-web` failure as a
  candidate flake and re-run it before reading it as a regression — and do not
  read a single green gate as proof of determinism.

- **Two runs completed through the lifecycle** (#8, #10). #3 also did; #5 and #6
  were human-recovered under a bootstrap exception and are not lifecycle
  evidence.
