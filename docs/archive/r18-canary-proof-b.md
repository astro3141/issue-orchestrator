# R18 canary Proof-B — fixture evidence record

**Disposable.** Written 2026-08-24 for issue #250 as the single inert change
carried by the R18 canary Proof-B lifecycle. It records that a run happened; it
is not guidance, not a runbook, and nothing reads it.

## What this file is for

Issue #249 promotes an R18 provisional. Proof B asks a narrower question than
"does the provisional work": can the exact provisional artifact carry one
ordinary issue through Actor → validation → review → PR → cleanup without
touching preserved Pilot-3 state? Answering that needs a change to carry, and
the change must be incapable of influencing the answer. Hence a Markdown file in
`docs/archive/`: no orchestrator behavior, policy, authority, configuration,
test, script, provider setup, or runtime state depends on it existing.

## Lifecycle facts recorded

- Baseline the candidate started from: `0240d4defdd6025a1f9a8448c37960e6b84ed7d5`
- Ordinary lane, one Actor launch, one issue (#250) processed
- Publication validation and `reviewer_ok` bind to the exact candidate SHA
- Fixture PR head equals the reviewed candidate

The pass/fail verdict itself is not restated here. #249 is the promotion
controller and the single place that record lives; a second copy would be the
kind of drift `docs/selfhosting/RUNBOOK.md` already describes.

## Terminal handling

The pull request that carries this file is canary evidence and **must remain
unmerged**. If #249 declares the current provisional terminal, this fixture is
spent — it is not reused for another promotion attempt, and this file is not a
template for one.
