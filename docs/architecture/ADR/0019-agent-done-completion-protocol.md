# ADR 0019: Structured completion protocol (agent-done)

**Status:** Accepted
**Date:** 2024-12-21

## Context

When an agent finishes work, the orchestrator needs to know:
- Did it succeed, fail, or get blocked?
- What actions does it want (create PR, add label)?
- What was implemented? What problems occurred?
- What ancillary work was discovered but intentionally deferred?

Options considered:
1. **Exit codes** - Too limited (success/fail only)
2. **Parse terminal output** - Fragile, unstructured
3. **Agent calls API directly** - Security risk (ADR-0016)
4. **Structured completion file** - Agent writes, orchestrator reads

## Decision

**Agents signal completion by writing a structured JSON file via the `agent-done` command.**

### The `agent-done` Command

```bash
# Success - work completed
agent-done completed \
  --implementation "Added user authentication with JWT" \
  --problems "None"

# Success - work completed, with ancillary follow-up proposals already written to a file
# Add --follow-up-file <existing-path> to the completed command above.

# Blocked - can't proceed
agent-done blocked \
  --reason "Depends on issue #122 which isn't merged" \
  --attempted "Tried to import auth module"

# Review outcomes
agent-done approved --summary "Code looks good, tests pass"
agent-done changes_requested --issues "Missing error handling in login.py"
```

### Completion Record Format

```json
{
  "schema_version": "1.0",
  "session_id": "issue-123-abc",
  "outcome": "completed",
  "requested_actions": ["create_pr", "add_label:ready-for-review"],
  "implementation": "Added JWT authentication",
  "problems": "None",
  "follow_up_issues": [
    {
      "title": "Isolate env-sensitive logging test",
      "reason": "Discovered while validating the assigned issue, but unrelated to the core fix",
      "suggested_labels": ["bug", "tests"],
      "blocking": false
    }
  ],
  "timestamp": "2024-12-21T10:30:00Z"
}
```

`follow_up_issues` are advisory only. Agents do not create GitHub issues directly; they report ancillary work and the orchestrator decides how to persist or surface it.

### Validation Runs First (Fast Feedback)

Before writing the completion record, `agent-done` runs the validation gate:

```
agent-done completed
         │
         ▼
    Run validation (tests, linting, type checks)
         │
    ┌────┴────┐
    │         │
  PASS      FAIL
    │         │
    ▼         ▼
Write      Print errors
completion  Agent can fix
record      and retry
```

**Why validate in agent-done:**
- **Fast feedback**: Agent sees failures immediately, can fix and retry
- **No wasted cycles**: Don't signal "completed" if tests fail
- **Clear contract**: Completion means "validated and ready"
- **Agent learns**: Repeated failures teach the agent what's expected

If validation fails, `agent-done` exits non-zero and the agent can:
1. Read the error output
2. Fix the issue
3. Run `agent-done` again

### Why a Command (not direct file write)

1. **Runs validation** - Tests/linting before completion (see above)
2. **Validates input** - Catches malformed completions early
3. **Consistent format** - Schema enforced at write time
4. **Audit trail** - Command logged in session history
5. **Extensible** - Add fields without breaking agents
6. **Discoverability** - `agent-done --help` shows options

### Orchestrator Processing

```
Agent writes completion.json
         │
         ▼
Orchestrator observes file (FactGatherer)
         │
         ▼
Validates as untrusted input
         │
         ▼
Planner decides actions based on outcome
         │
         ▼
ActionApplier executes (push, create PR, labels)
```

### Which File the Orchestrator Reads

`agent-done` writes to the run's canonical completion path. Two things can
put a second file beside it: a legitimate second review after rework, and a
crash inside `agent-done` itself, which leaves an **error placeholder** at
the canonical path (it carries `agent_done_error` and by construction no
`summary`) and sends the successful retry to a `-2`, `-3`, ... sibling.

Two questions, two owners, one chain. `completion_record_path` in
`domain/models.py` owns **where** a run's completion lives — the single join
of a worktree and the stored relative hint, so no caller re-derives it.
`select_completion_record` in `control/completion_record_validation.py`
starts from that answer and owns **which** file is authoritative. The
observer, the session controller, the run-scoped audit copy, and cleanup all
ask, so none of them can act on — or report — a record another cannot see:

- canonical missing or **valid** → canonical, always. Siblings are ignored;
  authority is never re-assigned between two valid completions.
- canonical is an error placeholder → exactly one valid sibling, named with
  the producer's own numeric suffix, in the same run directory, carrying the
  same `session_id`, may take over. The placeholder's `agent_done_error` is
  logged at INFO and travels on the lookup event either way.
- anything else → canonical, unchanged. Several valid siblings is ambiguity,
  not a race to resolve: there is no newest-wins or suffix-order rule, so the
  placeholder stays authoritative and fails closed onto the existing
  rejected-record diagnostic path.

Every candidate is read through `load_completion_record_result`, so the
file-size gate and field bounds apply to siblings exactly as they do to the
canonical record. Selection itself moves, renames, and deletes nothing.

Processing resolves the selection **once**, then hands that same object to
the run-scoped audit copy and to cleanup. Cleanup clears every path the
selection says the run occupies — the record that was acted on and any
placeholder it superseded — and names the acted-on one in its log. Both
halves matter: leaving the acted-on record behind would report a completion
as cleaned while it survived on disk, and leaving the placeholder behind
would block `restore_completion_record` on the publish-retry path, which
no-ops when something already occupies the canonical path. Siblings the
owner refused to choose between are left alone; unresolved evidence is not
cleanup's to delete.

## Consequences

### Positive
- **Structured**: Machine-readable, schema-validated
- **Auditable**: Clear record of what agent reported
- **Secure**: Agent reports intent, orchestrator executes
- **Extensible**: New outcomes/fields without breaking changes

### Negative
- Agents must learn `agent-done` command
- Extra step vs implicit completion
- File-based IPC has latency

## Validation

Completion records are **untrusted input**:
- Validate schema version
- Sanitize string fields
- Verify session_id matches expected
- Reject unknown outcomes

## Related

- ADR-0016: Orchestrator as mediator
- `AGENT_PROTOCOL.md`: Full protocol specification
- `entrypoints/cli_tools/agent_done.py`: Command implementation
