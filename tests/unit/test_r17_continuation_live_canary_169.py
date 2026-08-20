"""Disposable canary fixture for controller issue #169.

This module is not product functionality and must not be merged into product
`main`. It exists to make the first canonical full publication gate fail
exactly once for one candidate SHA, and to pass unchanged on the same SHA after
restart, by consuming a marker file the controller creates once.

The node id deliberately carries none of the selfhost quick-gate selector
terms, so the configured quick gate never consumes the marker.
"""

from pathlib import Path


def test_r17_continuation_once() -> None:
    marker = Path("/tmp/io-r17-continuation-canary-169-once")
    if marker.exists():
        marker.unlink()
        raise AssertionError("R17_POST_167_CANARY_FIRST_PUBLISH_SENTINEL")
