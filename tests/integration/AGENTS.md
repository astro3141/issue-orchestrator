# Integration Tests

Tests that verify wiring between components with real (not mocked) adapters.

## What These Test

- Component wiring via `entrypoints/bootstrap.py`
- Real adapter implementations (not mocks)
- Plugin registration and hook dispatch
- Claude CLI execution (if available)

## Running

```bash
pytest tests/integration/ -v
```

## Key Files

- `test_wiring.py` - Verify DI and adapter wiring
- `test_live_hooks.py` - Test hook installation/execution
- `test_claude_execution.py` - Test Claude CLI integration (requires claude)

## The `live_agent` marker decides which gate owns a module

A module that spawns a real provider CLI must declare
`pytest.mark.live_agent` at **module scope** (`pytestmark`). That single marker
is the whole mechanism:

- blocking targets deselect it (`-m "... and not live_agent"`), so a model
  declining to issue a tool call can never fail an unrelated candidate;
- `make test-live-assurance` collects it and files a
  `PASS` / `SECURITY_FAIL` / `INCONCLUSIVE` record for the exact artifact.

There is no filename list to update; adding a live-agent module is one edit.
A per-test marker is wrong — the rest of the file would stay in the blocking
lane, and `tests/unit/test_makefile_validation_phases.py` fails on it.

**The marker deselects; it does not prevent collection.** Blocking validation
still *imports* every module in this directory, once per xdist worker. So a
readiness probe that contacts a provider — `is_claude_authenticated()`, which
runs a real `claude -p` — must happen at call time, in an autouse fixture,
never at module scope. At module scope it becomes a live provider call inside
the publish gate, for tests that gate is about to throw away — and a
`skipif` condition is module scope, because a decorator expression is evaluated
on import. `test_live_agent_chain.py` shows the shape, the probe registry lives
in `tests/fixtures/live_agent_cli.py`, and a guardrail proves the rule by AST.

The rule is per **provider**, and so is the registry: `live_codex` deselects
after collection exactly like `live_agent`, so
`is_codex_authenticated()` belongs in the same module and the same
`LIVE_PROVIDER_PROBES` tuple. A probe written as a private helper in the test
that needs it is structurally invisible to the guardrail, which is how a
module-scope `codex login status` sat inside the publish gate while the rule
forbidding it read green (#227). Collection also runs *before*
`tests/codex_home.py`'s autouse isolation fixtures, so an import-time codex
spawn hits the operator's real `~/.codex`.

That fixture reports an unusable provider with `require_probe_ran(...)`, not by
skipping. An unauthenticated CLI leaves the boundary exactly as unexercised as
a model that declined to issue the tool call, so the lane should reach
`INCONCLUSIVE` by the same route; and the root `AGENTS.md` is explicit that a
missing prerequisite must fail loudly enough to tell someone to install it.
The orchestrator's own branch-diff guard refuses newly added skip constructs in
test paths, so this is the shape that lands, not merely the one that reads
better.

Inside such a module, say which outcome an assertion means:
`assert_no_breach(...)` for a security condition, `require_probe_ran(...)` for
"the required operation was actually issued". A bare `assert` carrying a
`SANDBOX BREACH` message is classified `INCONCLUSIVE` — a proven breach filed
as a provider hiccup — and is caught by a guardrail test.

Say which one it is in the message, too. `require_probe_ran` also covers a
*positive control* that failed — the operation was issued and our own
allow-list refused it. Both leave the boundary unproven, so both are
`INCONCLUSIVE`, but only "never issued" is answered by the re-run
`trusted-runtime-promote` suggests. The message survives verbatim into the
record's `detail` and is all an operator has once the probes are gone.

## Every test in a `live_agent` module must reach a provider

The marker is module scope, so it takes the whole file. A test that spawns no
provider and depends on no model leaves blocking validation along with the
probes it was sitting next to, and the assurance lane files a record rather
than failing a candidate — so it then runs in **no** gate at all. That is what
happened to `TestShellEscaping` and the `agent-done` cases; they now live in
`tests/integration/test_agent_invocation_surface.py`, which carries no marker.

`tests/live_agent_reach.py` states the rule structurally: a test reaches a
provider when its body names a provider CLI, a registered production seam that
builds one (`CodexProvider`, `ClaudeCodeAdapter`, …), a registered
live-provider probe, or `assert_no_breach` / `require_probe_ran`.
`tests/unit/test_makefile_validation_phases.py` fails on any live-agent test
that does not. Reach means the provider **CLI**, not the model: `codex
--version` needs no model but does need the operator's install, and a CLI
upgrade must not be able to fail an unrelated candidate.

The check fails closed. If a test genuinely reaches a provider by a route
nothing registers, add the route in `tests/live_agent_reach.py` — do not loosen
the check. Deterministic assertions that do **not** depend on a provider belong
in a non-`live_agent` module so they stay in blocking validation (see
`tests/integration/test_agent_invocation_surface.py`, and
`tests/sandbox_stream_events.py` + `tests/unit/test_sandbox_stream_events.py`).

## Difference from Unit Tests

| Unit | Integration |
|------|-------------|
| Mock all ports | Real adapters |
| Fast, isolated | Slower, real I/O |
| Test logic | Test wiring |
