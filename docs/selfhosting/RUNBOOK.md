# Self-hosting runbook — moved

The living runbook for operating Issue-Orchestrator against this fork is
**issue #18**, at
<https://github.com/astro3141/issue-orchestrator/issues/18>. Read it there.

This file used to carry its own copy of those operating notes. The copy drifted
— it went on naming a runtime and a config path that had both moved on — because
the same facts were written down in two places and only one of them was kept
current. Operating notes a human reads live in the issue; the repository keeps
what an agent must read from a checkout, or what is executed. So nothing about
the current runtime, pins, paths or procedure is restated here: #18 is the one
place to change when any of it changes.

What remains under `docs/selfhosting/` is executed, not narrated:

- `io-status.sh` — status of a self-hosting run
- `canary-verify.sh` — canary verification for a given issue

`SELF_HOSTING_READY.md` is a dated milestone record of what was established on
2026-08-12, not operating instructions.

<!-- PROOF-ONLY #317: disposable EXHAUSTED poison fixture for the #307 replay; evidence only, do not merge -->
