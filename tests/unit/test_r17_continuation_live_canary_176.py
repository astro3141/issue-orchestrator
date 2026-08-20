"""Disposable canary fixture for controller issue #176/#175.

This module is not product functionality and must not be merged into product
`main`. It exists to make the first canonical full publication gate fail
exactly once for one candidate SHA, and to pass unchanged on the same SHA after
restart, by consuming a marker file the controller creates once.

The node id
`tests/unit/test_r17_continuation_live_canary_176.py::test_r17_continuation_once`
deliberately carries none of the selfhost quick-gate selector terms
(`validation`, `profile`, `config`, `prepush`, `planted`, `cli_tools`, `dirty`,
`worktree`, `review_verdict`, `review_exchange_records`, `review_artifacts`,
`Verdict`, `codex_home`, `publication`, `publish`, `contract`, `gate`,
`execution_identity`, `ExecutionIdentity`, `attempt`), which is what pytest's
case-insensitive `-k` matches against the item's keywords (`tests`, `unit`, the
module basename, the test name). The configured quick gate therefore deselects
it and never arms the sentinel; only the full publish suite can, which is what
puts the failure inside the orchestrator's publication seam.
"""

from pathlib import Path


def test_r17_continuation_once() -> None:
    marker = Path("/tmp/io-r17-continuation-canary-176-once")
    if marker.exists():
        marker.unlink()
        raise AssertionError("R17_POST_173_CANARY_FIRST_PUBLISH_SENTINEL")
