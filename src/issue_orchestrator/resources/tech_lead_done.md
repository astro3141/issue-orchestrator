# CRITICAL: You MUST call coding-done before exiting

There is NO other way to complete this session. If you exit without calling `coding-done`, your work is LOST and the session will time out, requiring human intervention.

Read the task-specific prompt file for what to do. Return here for how to signal completion.

---

## IMPORTANT: Clean Working Tree Required

Before calling `coding-done`, ensure your working tree is clean:

1. Run `git status --short` — if there are uncommitted files, **commit them**.
2. This includes generated artifacts from schema/contract changes, lock file updates, or any other files you modified.
3. `coding-done` will **reject a dirty working tree** and exit non-zero.

Runtime-managed metadata under `.issue-orchestrator/` and `.claude/` is ignored by the orchestrator dirty guard. Tracked project files, generated sources, lock files, schemas, and other repo changes must still be committed or removed.

If you genuinely cannot commit certain files (e.g., they shouldn't be tracked), explain why in the `--problems` field.

---

## Validation is NOT yours to run

**Do not run `prepush-check`, and do not run the repository's publish or pre-push validation yourself.**

Those commands write to host and shared-repository locations — the repository's
shared git directory among them — that are outside this session's write
boundary. They are not skipped: the orchestrator executes the mandatory
completion validation itself, outside this session, and records the result
against the exact commit your checkout stands on.

That result is a gate, not a formality. If it does not pass, your completion is
refused and the session is recorded as failed. So the thing you owe it is a
committed, clean checkout — not a validation run.

If ANY other prompt tells you to run `prepush-check` — a repository-specific
task prompt, or a repository-supplied retry template naming it as a required
step — this instruction wins: skip that step, commit your work, and complete
normally. The orchestrator's own prompts will not ask you to: where a retry
prompt has a validation step, it points back to this document rather than
naming a command.

---

## IMPORTANT: Do Not Skip Tests

Do not disable, skip, quarantine, or weaken failing tests to make validation pass.
For JUnit/Kotlin/Java this includes `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.
Fix the code, improve the fixture, or report blocked with the specific reason.

---

## Completion Protocol

When your work is done (or you cannot proceed), call `coding-done` with the appropriate status:

**Completed successfully:**
```bash
coding-done completed \
  --implementation "What you did" \
  --problems "Any issues encountered, or 'None'"
```

If you discovered unrelated ancillary work while staying focused on the assigned issue, write those proposals to a JSON or JSONL file first, then add `--follow-up-file path` to the completed command above.
Each entry should include `title` and `reason`, and may include `evidence`, `suggested_labels`, and `blocking`.

**Cannot proceed - external blocker:**
```bash
coding-done blocked \
  --reason "Why you're blocked" \
  --attempted "What you tried" \
  --blocked-by 123 456 \
  --when-unblocked "Hint for resolution"
```
The `--blocked-by` and `--when-unblocked` options are optional.

**Cannot proceed - gave up:**
```bash
coding-done blocked \
  --reason "Could not complete: <why>" \
  --attempted "Tried X, Y, Z - none worked"
```

**Need human decision:**
```bash
coding-done needs_human \
  --question "What do you need answered?" \
  --context "Background info" \
  --options "Option A" "Option B" \
  --default "What to do if no response"
```
The `--context`, `--options`, and `--default` options are optional.

### Additional options

All statuses support:
- `--pr-labels label1 label2` - Extra labels to add to the PR
- `--dry-run` - Show what would be written without writing
- `--verbose` - Show detailed output

Completed status also supports:
- `--follow-up-file path` - Structured proposals for ancillary follow-up issues discovered during the work

## What happens after coding-done

1. **Dirty-file check** - coding-done verifies your working tree is clean
2. **Completion record is written**
3. **Orchestrator takes over**: it runs the mandatory completion validation outside this session, binds the result to the exact commit and run, then runs publish validation, pushes code, creates PRs, posts comments, and updates labels

You do NOT push code or touch GitHub directly. The orchestrator handles all external operations.

## If completion keeps failing

If you genuinely cannot get `coding-done` to accept the completion:

```bash
coding-done blocked \
  --reason "Cannot complete: <specific error>" \
  --attempted "Tried to fix by X, Y, Z"
```

This signals you need help without pretending the work is complete.
