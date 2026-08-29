# Tech Lead Data Sources Contract

This document defines the data sources available to tech lead agents, how to
access them, and safety rules for their use.

Tech Lead agents operate on **local files only**. The orchestrator pre-fetches
everything you need before the session starts; you never call `gh` or the
GitHub API, and you never mutate GitHub state (comments, labels, issues, PRs).
The orchestrator executes all GitHub operations after you complete.

## Data Sources

### Tech Lead Manifest (Authoritative)

The primary input. The orchestrator writes it into your session directory:

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Manifest | `cat "$ISSUE_ORCHESTRATOR_RUN_DIR/tech-lead-data/manifest.json"` | Which PRs to review, each bound to the exact `head_sha` it was selected at |
| Candidate evidence | `cat .../tech-lead-data/candidate-evidence.json` | What the independent Reviewer decided about that exact commit, and whether it cleared the publication gate |
| PR metadata | `cat .../tech-lead-data/pr-{N}-{sha12}-meta.json` | Title, body, branch, `candidate_sha` |
| PR diff | `cat .../tech-lead-data/pr-{N}-{sha12}-diff.txt` | That candidate's code changes |

The manifest is the definitive list of candidates in scope. Review exactly those
PRs - no more, no less - and render your verdict against the commit named there.
A candidate whose `candidate-evidence.json` entry carries a non-empty `gap` has
no established independent approval of that commit and must never be passed.

### Board Snapshot (Authoritative)

Written at launch for both tech lead flavors - a point-in-time snapshot of
orchestrator state, not live state:

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Board snapshot | `cat "$ISSUE_ORCHESTRATOR_RUN_DIR/tech-lead-data/board-snapshot.json"` | Active sessions (type/state/age), pending queues with reasons, blocked issues, recent failures, per-issue timeline extracts, orchestrator log tail |

Batch reviews use it to spot cross-PR and systemic patterns worth
`flag_pattern`/`create_issue` proposals; failure investigations start from
their focus issue and use it for board context.

### Orchestrator Configuration (Authoritative)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Config file | `cat .issue-orchestrator/config/modes/<mode>/*.yaml` | Agent definitions, timeouts, label names, review workflow |
| Agent prompts | `cat .prompts/<agent>.md` or path from config | What agents are instructed to do |

### Local Logs and Session Artifacts (Advisory)

Use for investigation; they may be incomplete, rotated, or stale.

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Orchestrator log | `cat ~/.issue-orchestrator.log` | Infrastructure errors, label failures, session lifecycle |
| State file | `cat .issue-orchestrator/state.json` | Session history, pending reviews (may be stale) |
| Session run dirs | `ls .issue-orchestrator/sessions/` | Completion records, validation outcomes |

### Worktree State (Advisory)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Git status | `git status` in this worktree | Uncommitted work, branch state |
| Git log | `git log` in this worktree | What was committed for this session |

## Safety Rules

### Never Do

1. **Don't call `gh`** - not for reads, not for writes; all PR data you need is local
2. **Don't post comments, edit labels, or create issues/PRs** - the orchestrator owns all GitHub mutations
3. **Don't merge or approve PRs** - those are human decisions
4. **Don't delete worktrees** - they may contain uncommitted work

### Always Do

1. **Start from the manifest** - it is the definitive review scope
2. **Verify advisory sources against the manifest and config** before drawing conclusions
3. **Make improvements locally** - edit prompts/docs in this worktree and commit; the orchestrator publishes your branch
4. **Report everything else in your completion record** - `coding-done completed --implementation "..." --problems "..."`

## What Happens After You Complete

- `coding-done completed`: the orchestrator applies your `candidate_verdicts`
  per candidate — the configured `tech_lead_reviewed_label` (default
  `tech-lead-reviewed`) for a `pass` on a candidate still standing at the commit
  you judged, the ordinary rework lane for a `rework`, human escalation for a
  `human_a`, and nothing at all for a candidate whose head has moved — and
  publishes any commits on your branch.
- Session failure: manifest PRs get the `tech_lead_failed_label`
  (default `tech-lead-failed`).
- Each candidate receives ONE receipt comment naming the exact commit your
  verdict was about and this run's identity — including the refusals, so a
  disposition that could not be applied is visible rather than silent. Nothing
  else is commented, no other labels are flipped, and no issues are created on
  your behalf.
