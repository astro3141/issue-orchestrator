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

Inside such a module, say which outcome an assertion means:
`assert_no_breach(...)` for a security condition, `require_probe_ran(...)` for
"the required operation was actually issued". A bare `assert` carrying a
`SANDBOX BREACH` message is classified `INCONCLUSIVE` — a proven breach filed
as a provider hiccup — and is caught by a guardrail test. Deterministic
assertions that do **not** depend on a model belong in a non-`live_agent`
module so they stay in blocking validation (see
`tests/sandbox_stream_events.py` and `tests/unit/test_sandbox_stream_events.py`).

## Difference from Unit Tests

| Unit | Integration |
|------|-------------|
| Mock all ports | Real adapters |
| Fast, isolated | Slower, real I/O |
| Test logic | Test wiring |
