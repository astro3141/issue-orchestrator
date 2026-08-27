# Control Center Lifecycle Model

## Purpose

Define one stable mental model for the Control Center UI and repository runtime lifecycle.

## Scope Model

1. **Control Center**: the local UI shell and dashboard process.
2. **Repository Engine**: per-repository runtime that orchestrates work.
3. **Jobs**: issue-level execution units managed by a repository engine.

The Control Center is a client surface. Repository engines are the long-lived runtime entities.

## Core Behavior

1. Closing Control Center does **not** stop repository engines.
2. Engine lifecycle is controlled at engine scope: `Start engine`, `Pause engine`, `Resume engine`, `Stop engine`.
3. Global controls are only app-level or bulk actions.
4. Left navigation remains a stable set of view selectors and does not add/remove entries based on runtime state.

## Stop Disposition

A non-force stop is a request plus an observation, never a signal.

1. **Failure to confirm a graceful shutdown request is not authority to signal
   the engine.** Whether the request succeeds or comes back unconfirmed, the
   target is observed over the same graceful budget.
2. If the budget expires while the engine is still alive and no force
   escalation is authorized, the stop reports a non-success outcome and leaves
   the engine running — no `SIGTERM`, `SIGKILL`, process-group kill or port
   kill.
3. Explicit force, and an explicitly-authorized force-on-timeout policy, keep
   their existing authority; force-on-timeout escalates only after the budget
   is spent.
4. This rule is identical for the tracked-lock stop and the port-only stop:
   both run through the one disposition owner,
   `infra/shutdown_timing.py::InterruptibleStopController`, and both accept the
   same `force` and `force_if_graceful_fails` authority from the caller. An
   escalation the endpoint accepted is never silently dropped by whichever
   branch happens to identify the target.
5. A stop response states what was observed, and states *why*. The disposition
   owner returns `EngineStopDisposition` — outcome, stopped count, and the
   engines still running — and the response is derived from it rather than from
   a second observation made by the caller. A stop that left the engine running
   is reported as such, is never presented as a clean stop or as "already
   stopped", and distinguishes "no escalation was authorized, so nothing was
   signalled" from "the authorized escalation ran and the engine survived it".
   Process and lock evidence outrank presentation.
6. Reconcile is a sweep across every registered repository, so it carries its
   own bounded graceful budget rather than the per-engine shutdown default, it
   never escalates on its own authority, and its result reports the engines it
   left running so the surface cannot render a clean success for a sweep that
   stopped nothing.

## UI Placement Rules

1. Global header contains app-level actions and aggregate status only.
2. Per-engine controls live on engine cards and engine detail view.
3. Per-engine config selection lives on engine surfaces (overview card and/or detail), not global header.
4. Destructive action labels must be explicit:
   - `Stop engine` for single engine scope.
   - `Stop all engines` for bulk scope.
   - Avoid ambiguous `Shutdown` in engine contexts.

## State Vocabulary

Use this exact set everywhere in Control Center:

- `Running`
- `Paused`
- `Not running`

Use this exact set for engine actions:

- `Start engine`
- `Start paused`
- `Pause engine`
- `Resume engine`
- `Stop engine`

## Recovery and Mishap Handling

1. Browser/window close is treated as UI detach.
2. On reopen, detect still-running engines and present reconnect/recover actions.
3. Surface stale/orphaned runtime states clearly and provide deterministic cleanup actions.

## Future Compatibility

Design is forward-compatible with multi-node testing by extending runtime identity to `(node_id, engine_id)` while keeping the UI mental model unchanged.
