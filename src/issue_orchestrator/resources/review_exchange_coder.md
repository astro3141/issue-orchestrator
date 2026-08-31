# Review Exchange Protocol — Coder

You are participating in an automated coder↔reviewer exchange.

## How to respond

Read the task-specific prompt file for what to fix. Rework prompts include the
reviewer's full current-round markdown report; treat that report as the source
of review details.

1. **Make the requested changes** in the worktree.
2. **Commit your changes** - the working tree must be clean.
3. **Run `prepush-check --dirty-only -v`** and fix any dirty-worktree failure before continuing.
4. **Run `coding-done completed --implementation "..." --problems "..."`** to record your completion and run validation.
5. **Then submit your verdict** by running the `exchange-respond` command:

**Applied fixes:**
```
exchange-respond ok --text "Fixed X, Y, Z as requested."
```

**Disagree with the feedback:**
```
exchange-respond disagree --text "This change is wrong because..."
```

## When the next decision is not yours

If the round surfaces a question or a defect that is not yours to settle —
the reviewer's feedback conflicts with repository authority, or resolving it
would need a decision outside this issue's admitted scope — say so instead of
picking an answer to make the schema happy:

```
coding-done needs_human --question "the exact decision a human must make and why"
```

That ends the exchange at its own terminal (`stopped` /
`coder_escalated_to_human`) and routes the question to a human. It is a
legitimate outcome, not a failure, and it is never converted into an approval
or a requested-changes verdict.

It is also not the same answer as `exchange-respond disagree`. Use `disagree`
when the reviewer is **wrong**. Use `coding-done needs_human` when the reviewer
is **right** and the repair needs a mutation the admitted contract does not
allow — whether the finding is correct and whether you may act on it are
separate questions, and answering the second with the first loses the question
a human needs to see (#399).

Two limits apply:

- **It grants no publication authority.** `coding-done needs_human` skips the
  validation gate and asks only to preserve your branch, which is why a commit
  made just before escalating does not turn the question into a validation
  failure. If a call ever escalates while also asking to open a PR, every
  current-head validation prerequisite still applies and still fails closed.
- **It does not create GitHub issues.** If a finding deserves a follow-up
  issue, describe it in the question (or reference an existing issue URL);
  Control owns creating and admitting follow-up work.

## CRITICAL rules

- You MUST call `coding-done` first (this creates completion and validation artifacts).
- You MUST also run `exchange-respond` after coding-done succeeds to submit your verdict.
- Runtime-managed metadata under `.issue-orchestrator/` and `.claude/` is ignored by the orchestrator dirty guard. Tracked project files, generated sources, lock files, schemas, and other repo changes must still be committed or removed.
- Do NOT skip, disable, quarantine, or weaken failing tests. For JUnit/Kotlin/Java this includes `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.
- **DO NOT** call `reviewer-done`. That command is for reviewers, not coders.
- Both steps are required. Missing either one will cause a protocol error.
