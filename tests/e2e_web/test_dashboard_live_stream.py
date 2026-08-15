"""Browser proof that the dashboard is a live liveness signal (issue #44).

The incident, replayed in Chromium: the engine's event stream goes quiet while
the socket stays open. Measured on this repo before the fix, the page reported
`EventSource.readyState === 1` for 25 seconds after the engine was killed,
never fired an `error`, never reconnected, and cleared its own warning banner
because `/api/info` still answered — a frozen board presenting itself as
healthy.

Producing that state needs care, because the two obvious ways to "break the
connection" both produce something else: killing the server sends a FIN, which
Chromium *does* report as an error, and Chromium's offline mode leaves an
already-established socket flowing. ``HalfOpenProxy`` produces the real thing —
bytes stop, the socket stays open, nothing is reported — so the only signal
left is the missing beacon.

Playwright is the right layer here only because the bug lives in a real
`EventSource`'s silence. The watchdog arithmetic, the reconnect backoff, and
the status wording are pinned far more cheaply in
``tests/js/live_event_stream.test.js``.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from issue_orchestrator.entrypoints import web_event_stream

from .conftest import login_via_form
from .half_open_proxy import HalfOpenProxy

# The client's deadline is 2.5 beacon intervals, and the interval is whatever
# the server puts on the wire. Running the server at 1s keeps this test's
# outage window in single-digit seconds instead of half a minute, while
# exercising exactly the production code path — the cadence travels on the
# beacon, so the watchdog retunes itself.
TEST_BEACON_INTERVAL_SECONDS = 1.0
DETECTION_TIMEOUT_MS = 20_000


@pytest.fixture
def fast_beacon():
    previous = web_event_stream.LIVENESS_INTERVAL_SECONDS
    web_event_stream.LIVENESS_INTERVAL_SECONDS = TEST_BEACON_INTERVAL_SECONDS
    try:
        yield TEST_BEACON_INTERVAL_SECONDS
    finally:
        web_event_stream.LIVENESS_INTERVAL_SECONDS = previous


@pytest.fixture
def proxied_dashboard(authed_web_server: dict[str, object]):
    """The dashboard, reachable through a proxy that can go half-open."""
    upstream = urlparse(str(authed_web_server["url"]))
    assert upstream.port
    proxy = HalfOpenProxy(upstream.port)
    try:
        port = proxy.start()
        yield proxy, f"http://127.0.0.1:{port}"
    finally:
        proxy.stop()


@pytest.mark.usefixtures("browser_context_args", "fast_beacon")
def test_dashboard_reports_a_lost_live_stream_and_resubscribes(
    page: Page,
    proxied_dashboard,
    cc_admin_token: str,
) -> None:
    proxy, base_url = proxied_dashboard

    sse_requests: list[str] = []
    page.on(
        "response",
        lambda response: (
            sse_requests.append(response.url)
            if "/api/events" in response.url
            else None
        ),
    )

    login_via_form(page, base_url, cc_admin_token)
    page.wait_for_function(
        "() => window.dashboardBundleLoaded === true", timeout=15_000
    )

    status = page.locator("#liveStreamStatus")

    # 1. A freshly authenticated dashboard establishes a live subscription and
    #    says so. The indicator is never blank, so "no warning" can never mean
    #    "silently dead" (requirement 3).
    expect(status).to_have_attribute("data-connection", "live", timeout=15_000)
    assert len(sse_requests) == 1, sse_requests

    # 2. The stream goes silent with the socket still nominally open. Nothing
    #    in the browser reports a fault, so detection can only come from the
    #    missing beacon — and the loss must be human-visible (requirement 5).
    frozen = proxy.freeze_matching("/api/events")
    assert frozen == 1, f"expected to freeze exactly the SSE connection, froze {frozen}"
    expect(status).to_have_attribute(
        "data-connection", "lost", timeout=DETECTION_TIMEOUT_MS
    )
    expect(status).to_contain_text("cached snapshot, not live")
    assert (
        page.evaluate("() => window.ioDashboardLiveStream.getStatus().engine")
        == "unknown"
    ), "a lost stream must not keep advertising the engine as advancing"

    # 3. Recovery is the page's own job: a new subscription over a new
    #    connection, with a freshly minted single-use token, since the
    #    previous one is spent (requirement 7).
    expect(status).to_have_attribute("data-connection", "live", timeout=60_000)
    assert len(sse_requests) >= 2, (
        "the dashboard did not open a new SSE subscription after the outage: "
        f"{sse_requests}"
    )


@pytest.mark.usefixtures("browser_context_args")
def test_live_status_is_reachable_to_assistive_tech(
    page: Page,
    authed_web_server: dict[str, object],
    cc_admin_token: str,
) -> None:
    """The indicator is a polite live region, and never colour-only.

    A status an operator cannot perceive is not an observability fix.
    """
    base_url = authed_web_server["url"]
    assert isinstance(base_url, str)

    login_via_form(page, base_url, cc_admin_token)
    page.wait_for_function(
        "() => window.dashboardBundleLoaded === true", timeout=15_000
    )

    status = page.locator("#liveStreamStatus")
    expect(status).to_have_attribute("role", "status")
    expect(status).to_have_attribute("aria-live", "polite")
    expect(status).to_be_visible()
    # The glyph is decorative; the state must survive in text alone.
    expect(status.locator(".live-stream-status__icon")).to_have_attribute(
        "aria-hidden", "true"
    )
    text = status.locator(".live-stream-status__text").inner_text().strip()
    assert text, "the live-stream status must never render as empty text"
