"""A TCP proxy that can leave a live connection half-open (issue #44).

Reproducing the incident needs the one network condition the browser does not
report: bytes stop arriving on an established socket that is never closed. A
killed server sends a FIN, which Chromium turns into an ``error`` event;
Chromium's own offline mode leaves an already-established socket untouched, so
the stream keeps flowing. Neither reproduces the failure.

This proxy does. ``freeze_matching("/api/events")`` stops relaying the connections carrying
that request while holding their sockets open, so the browser sees an SSE
stream that has silently gone quiet and ``readyState`` stays ``OPEN``. Every
other connection — and every connection opened afterwards — is relayed
normally, so the page's own recovery path (mint a fresh token, open a new
stream) still works, exactly as it must after a real engine restart.
"""

from __future__ import annotations

import socket
import threading

#: Cap on how much recent browser->server traffic each link remembers for
#: matching. Requests are small; this only exists so a long-lived connection
#: cannot grow the buffer without bound.
_MAX_RECORDED_REQUEST_BYTES = 32768


class _Link:
    """One browser<->server connection pair."""

    def __init__(self, downstream: socket.socket, upstream: socket.socket) -> None:
        self.downstream = downstream
        self.upstream = upstream
        self.frozen = False
        # Everything the browser has sent on this connection (bounded), so a
        # caller can freeze the one carrying a given request rather than every
        # socket to the origin. Chromium pools connections, so the SSE request
        # is often not the first on its socket — matching only the first bytes
        # would find nothing. Freezing all sockets would also strand the
        # pooled connections the page needs to recover on, which is not what a
        # dead event stream does.
        self.requests = b""


class HalfOpenProxy:
    """Relay TCP to ``upstream_port``, with a freeze that mimics a dead path."""

    def __init__(self, upstream_port: int, host: str = "127.0.0.1") -> None:
        self._host = host
        self._upstream_port = upstream_port
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._links: list[_Link] = []
        self._lock = threading.Lock()
        self._closed = False
        self.port: int | None = None

    def start(self) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._host, 0))
        listener.listen(64)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._spawn(self._accept_loop)
        return self.port

    def freeze_matching(self, request_substring: str) -> int:
        """Freeze open connections whose request contains ``request_substring``.

        Returns how many were frozen, so a caller can assert it actually hit
        the connection it meant to and is not testing a no-op.
        """
        needle = request_substring.encode("ascii")
        frozen = 0
        with self._lock:
            for link in self._links:
                if needle in link.requests:
                    link.frozen = True
                    frozen += 1
        return frozen

    def stop(self) -> None:
        self._closed = True
        with self._lock:
            links = list(self._links)
        for link in links:
            for sock in (link.downstream, link.upstream):
                try:
                    sock.close()
                except OSError:
                    pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=5)

    # -- internals ---------------------------------------------------------

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._closed:
            try:
                downstream, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(
                    (self._host, self._upstream_port), timeout=5
                )
            except OSError:
                downstream.close()
                continue
            downstream.settimeout(0.2)
            upstream.settimeout(0.2)
            link = _Link(downstream, upstream)
            with self._lock:
                self._links.append(link)
            self._spawn(self._relay, link, downstream, upstream, True)
            self._spawn(self._relay, link, upstream, downstream, False)

    def _relay(
        self,
        link: _Link,
        src: socket.socket,
        dst: socket.socket,
        from_browser: bool,
    ) -> None:
        while not self._closed:
            if link.frozen:
                # Hold the sockets open and deliver nothing. This is the whole
                # point: no FIN, no RST, no error for the browser to report.
                threading.Event().wait(0.1)
                continue
            try:
                chunk = src.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            if from_browser:
                # Keep the tail, not the head: an SSE request is the last one
                # its pooled connection ever carries, so the head fills up
                # with the page's CSS/JS long before the interesting request.
                link.requests = (link.requests + chunk)[
                    -_MAX_RECORDED_REQUEST_BYTES:
                ]
            try:
                dst.sendall(chunk)
            except OSError:
                break
        if not link.frozen:
            for sock in (src, dst):
                try:
                    sock.close()
                except OSError:
                    pass
