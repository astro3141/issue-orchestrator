// Owner of the dashboard's live event subscription (issue #44).
//
// The dashboard used to treat `EventSource.onerror` as its only disconnect
// signal. A half-open connection — engine killed and restarted, machine
// resumed from sleep, port rebound — never fires that error: readyState stays
// OPEN indefinitely, no reconnect is attempted, and the page keeps rendering a
// frozen read model. Measured in a real browser: 25s after the engine was
// killed the stream still reported readyState=1 and no error had fired, while
// the page's own "engine restarting" banner had already cleared itself because
// /api/info answered and the (dead) EventSource object was non-null.
//
// So liveness is proved positively instead of assumed until contradicted. The
// server emits a contracted `engine.liveness` beacon on a known cadence
// (`interval_seconds` travels on the frame, so this watchdog cannot drift out
// of tune with the server). Two independent facts come out of it:
//
//   * beacons arriving        -> the transport is alive
//   * beacon's tick_id moving -> the engine is alive
//
// Neither is inferred from process existence, HTTP reachability, or a non-null
// JS handle. When beacons stop, the stream is declared lost, closed, and
// reconnected with a *fresh* single-use token — and the operator is told, so a
// cached view is never silently presented as live.
(function (root) {
    'use strict';

    const LIVENESS_EVENT = 'engine.liveness';

    // Assumed cadence until the first beacon states the real one.
    const FALLBACK_INTERVAL_SECONDS = 10;
    // Beacons may be late (a slow tick, a busy loop) without the stream being
    // dead. Two and a half missed intervals is late enough to act on and short
    // enough that an operator is not staring at stale data for a minute.
    const MISSED_BEACON_TOLERANCE = 2.5;
    const WATCHDOG_POLL_MS = 1000;
    const MAX_BACKOFF_MS = 30000;

    function defaultTimers(target) {
        return {
            now: () => Date.now(),
            setTimeout: (fn, ms) => target.setTimeout(fn, ms),
            clearTimeout: (id) => target.clearTimeout(id),
            setInterval: (fn, ms) => target.setInterval(fn, ms),
            clearInterval: (id) => target.clearInterval(id),
            random: () => Math.random(),
        };
    }

    function backoffMs(attempt, random) {
        const capped = Math.min(attempt, 6);
        return Math.min(MAX_BACKOFF_MS, 1000 * (2 ** capped)) + Math.floor(random() * 300);
    }

    function formatAge(seconds) {
        if (seconds === null || seconds === undefined) return 'never';
        const whole = Math.max(0, Math.round(seconds));
        if (whole < 60) return `${whole}s ago`;
        const minutes = Math.floor(whole / 60);
        if (minutes < 60) return `${minutes}m ago`;
        return `${Math.floor(minutes / 60)}h ago`;
    }

    function retryClause(seconds) {
        if (seconds === null || seconds === undefined) return 'Reconnecting.';
        return `Reconnecting in ${Math.max(1, Math.round(seconds))}s.`;
    }

    function tickClause(tickId) {
        if (tickId === null || tickId === undefined) return '';
        return ` — engine tick ${tickId}`;
    }

    function phaseClause(phase) {
        return phase ? ` (phase: ${phase})` : '';
    }

    // One row per state the indicator can be in, keyed by the state name the
    // reading resolves to. A table rather than a chain of conditionals: the
    // states are a closed set and each has exactly one presentation, so a new
    // state is a missing row (loud) instead of a fall-through (silent).
    //
    // Every row carries a glyph as well as a tone class, so colour is never
    // the only signal (WCAG 1.4.1), and every row produces text — there is no
    // state that renders as blank, because "blank" is exactly how a dead
    // stream used to look.
    //
    // Each row also carries an `announcement`: what a screen reader is told
    // when the indicator *enters* this state. It is a plain string, not a
    // function of the view, and that is the point — the visible text ticks
    // (`3s ago`, `Reconnecting in 7s`) once a second on a healthy dashboard,
    // and re-announcing that would turn a status region into a metronome. The
    // announcement therefore says only what changed, and cannot go stale
    // between transitions because it carries no volatile detail to go stale.
    const LIVE_STREAM_PRESENTATION = {
        lost: {
            tone: 'lost',
            icon: '✕',
            announcement:
                'Live updates lost. This view is a cached snapshot, not live. '
                + 'Reconnecting.',
            text: (view) =>
                'Live updates lost — this view is a cached snapshot, not live. '
                + retryClause(view.reconnectInSeconds),
        },
        connecting: {
            tone: 'connecting',
            icon: '◌',
            announcement: 'Connecting to the live event stream.',
            text: () => 'Connecting to the live event stream…',
        },
        stalled: {
            tone: 'stalled',
            icon: '⚠',
            announcement: 'Engine is not completing ticks.',
            text: (view) =>
                'Engine is not completing ticks — last tick '
                + `${formatAge(view.secondsSinceTick)}${phaseClause(view.phase)}.`,
        },
        unknown: {
            tone: 'unknown',
            icon: '◌',
            announcement:
                'Live stream connected. The engine has not reported a tick yet.',
            text: () => 'Live stream connected — engine has not reported a tick yet.',
        },
        live: {
            tone: 'live',
            icon: '●',
            announcement: 'Live. The engine is completing ticks.',
            text: (view) =>
                `Live${tickClause(view.tickId)}, `
                + `last tick ${formatAge(view.secondsSinceTick)}.`,
        },
    };

    // Two independent facts collapse into one presentation key here, and only
    // here. A live transport carrying a stalled engine and a dead transport
    // are different things, and conflating them earlier is what let the page
    // describe a frozen board as healthy.
    function liveStreamStateKey(view) {
        if (view.connection !== 'live') return view.connection;
        return view.engine === 'advancing' ? 'live' : view.engine;
    }

    function describeLiveStreamStatus(view) {
        const state = liveStreamStateKey(view);
        const row = LIVE_STREAM_PRESENTATION[state] || LIVE_STREAM_PRESENTATION.unknown;
        return {
            state,
            tone: row.tone,
            icon: row.icon,
            announcement: row.announcement,
            text: row.text(view),
        };
    }

    // Reuse the node the server already rendered rather than replacing it.
    // Node identity matters for the announcement region in particular: a live
    // region that is torn down and rebuilt on every render is a *new* region
    // each time, and assistive technology either re-reads it or misses the
    // change entirely.
    function childByClass(element, className, doc, decorate) {
        if (typeof element.querySelector === 'function') {
            const found = element.querySelector(`.${className}`);
            if (found) return found;
        }
        const node = doc.createElement('span');
        node.className = className;
        if (decorate) decorate(node);
        element.appendChild(node);
        return node;
    }

    // Owner of the indicator's DOM. It exists because the indicator has state
    // the caller must not have to keep: which nodes are its own, and which
    // state was last *announced*. Rendering used to be a free function that
    // cleared and rebuilt the element on every publish — which, once the
    // region became permanent and `aria-live`, meant a screen reader user on a
    // perfectly healthy dashboard heard the status re-read every beacon.
    //
    // So the split here is deliberate: the visible text is refreshed on every
    // render (an operator watching the page wants the countdown to run), and
    // the polite region is written only when the state itself changes.
    function createLiveStreamIndicator(element, doc) {
        if (!element) {
            // Failing loudly beats a dashboard whose liveness indicator has
            // silently stopped rendering — an indicator that says nothing is
            // indistinguishable from the frozen board this module exists to
            // make impossible.
            throw new Error('live-stream indicator element is missing');
        }
        const document_ = doc || element.ownerDocument || null;
        const canBuildNodes = !!(document_ && typeof document_.createElement === 'function');
        let iconNode = null;
        let textNode = null;
        let announcementNode = null;
        let announcedState = null;

        function ensureNodes() {
            if (iconNode) return;
            iconNode = childByClass(
                element, 'live-stream-status__icon', document_,
                (node) => node.setAttribute('aria-hidden', 'true'),
            );
            textNode = childByClass(element, 'live-stream-status__text', document_);
            announcementNode = childByClass(
                element, 'live-stream-status__announcement', document_,
                (node) => {
                    node.className = 'live-stream-status__announcement visually-hidden';
                    node.setAttribute('role', 'status');
                    node.setAttribute('aria-live', 'polite');
                },
            );
        }

        function render(view) {
            const described = describeLiveStreamStatus(view);
            element.className = `live-stream-status live-stream-status--${described.tone}`;
            // Three hooks, because they answer three different questions and
            // collapsing them is how "the transport is fine" got mistaken for
            // "the engine is working". ``data-live-state`` is the presentation
            // tone; the other two are the underlying facts.
            element.setAttribute('data-live-state', described.tone);
            element.setAttribute('data-connection', view.connection);
            element.setAttribute('data-engine', view.engine);
            if (!canBuildNodes) {
                element.textContent = described.text;
                return described;
            }
            ensureNodes();
            iconNode.textContent = described.icon;
            textNode.textContent = described.text;
            if (described.state !== announcedState) {
                announcedState = described.state;
                announcementNode.textContent = described.announcement;
            }
            return described;
        }

        return { render };
    }

    // The one place a beacon payload becomes an engine reading. Anything the
    // frame does not state reads as "unknown" rather than inheriting the last
    // known value — a stale reading presented as current is the failure mode
    // this whole module exists to remove.
    function engineReadingFromBeacon(payload) {
        const frame = (payload && typeof payload === 'object') ? payload : {};
        return {
            engine: frame.state || 'unknown',
            tickId: frame.tick_id === undefined ? null : frame.tick_id,
            secondsSinceTick: frame.seconds_since_tick === undefined
                ? null
                : frame.seconds_since_tick,
            phase: frame.phase || '',
        };
    }

    function beaconInterval(payload, fallbackSeconds) {
        const declared = payload && payload.interval_seconds;
        const usable = typeof declared === 'number' && declared > 0;
        return usable ? declared : fallbackSeconds;
    }

    // The subscription itself. `openStream` is the only way a connection is
    // made, and it is expected to mint a fresh single-use SSE token every
    // time — reusing a spent one is what produced the incident's persistent
    // `invalid sse token` 401s.
    function createLiveEventStream(options) {
        const opts = options || {};
        const target = opts.target || root;
        const timers = Object.assign(defaultTimers(target), opts.timers || {});
        const openStream = opts.openStream;
        const wireEvents = opts.wireEvents || (() => {});
        const onOpen = opts.onOpen || (() => {});
        const onStatus = opts.onStatus || (() => {});

        let source = null;
        let started = false;
        let attempts = 0;
        let reconnectTimer = null;
        let watchdogTimer = null;
        let lastFrameAt = null;
        let connectStartedAt = null;
        let reconnectAt = null;
        // Every connect attempt is stamped, so an attempt the watchdog has
        // already given up on cannot resurrect itself when its promise
        // eventually settles and install a second source.
        let attemptToken = 0;
        let intervalSeconds = FALLBACK_INTERVAL_SECONDS;

        // The published view of the subscription: what the transport is
        // doing, and what the engine last said. Deliberately not called
        // "status" — it is a presentation view, and the two facts inside it
        // must stay separable.
        const view = {
            connection: 'connecting',
            engine: 'unknown',
            tickId: null,
            secondsSinceTick: null,
            phase: '',
            reconnectInSeconds: null,
        };

        function publish() {
            onStatus(Object.assign({}, view));
        }

        function setConnection(next) {
            const changed = view.connection !== next;
            view.connection = next;
            if (next !== 'live') {
                // An engine reading is only meaningful while the stream that
                // carries it is alive; keeping the last one would let a dead
                // stream keep advertising "advancing".
                Object.assign(view, engineReadingFromBeacon(null));
            }
            if (changed) publish();
        }

        function noteFrame() {
            lastFrameAt = timers.now();
            attempts = 0;
            reconnectAt = null;
            view.reconnectInSeconds = null;
            setConnection('live');
        }

        function applyLivenessFrame(payload) {
            noteFrame();
            intervalSeconds = beaconInterval(payload, intervalSeconds);
            Object.assign(view, engineReadingFromBeacon(payload));
            publish();
        }

        function closeSource() {
            if (source) {
                try { source.close(); } catch (_e) { /* already closed */ }
                source = null;
            }
        }

        function declareLost() {
            // Abandon any attempt still in flight before opening the next one,
            // or a hung `openStream` that resolves later would install its
            // source on top of the reconnect this call is about to schedule.
            attemptToken += 1;
            closeSource();
            lastFrameAt = null;
            connectStartedAt = null;
            setConnection('lost');
            scheduleReconnect();
        }

        function scheduleReconnect() {
            if (!started || reconnectTimer !== null) return;
            const waitMs = backoffMs(attempts, timers.random);
            attempts += 1;
            reconnectAt = timers.now() + waitMs;
            view.reconnectInSeconds = waitMs / 1000;
            publish();
            reconnectTimer = timers.setTimeout(() => {
                reconnectTimer = null;
                // The countdown is over the moment the attempt begins. Leaving
                // `reconnectAt` set would park the watchdog on the countdown
                // branch below, and a connect that then hangs would never be
                // timed by anything.
                reconnectAt = null;
                connect();
            }, waitMs);
        }

        // The watchdog is the whole point: it needs no cooperation from the
        // browser's error reporting, so it catches the half-open case that
        // `onerror` provably misses.
        //
        // It times whatever the subscription is currently waiting on — a frame
        // on an established stream, or the connect attempt that has not
        // produced its first frame yet. Those are the same failure: nothing is
        // reported. A connect can hang exactly as silently as a stream can
        // (a `fetch` for the SSE token has no default timeout; a socket can be
        // accepted and then blackholed, firing neither `open` nor `error`), so
        // it gets the same deadline rather than being trusted to settle.
        function checkWatchdog() {
            if (!started) return;
            if (reconnectAt !== null) {
                view.reconnectInSeconds = Math.max(0, (reconnectAt - timers.now()) / 1000);
                publish();
                return;
            }
            const waitingSince = lastFrameAt === null ? connectStartedAt : lastFrameAt;
            if (waitingSince === null) return;
            const silentSeconds = (timers.now() - waitingSince) / 1000;
            if (silentSeconds > intervalSeconds * MISSED_BEACON_TOLERANCE) {
                declareLost();
            }
        }

        async function connect() {
            if (!started) return;
            closeSource();
            attemptToken += 1;
            const token = attemptToken;
            connectStartedAt = timers.now();
            // The countdown ended with the wait, and the watchdog stops
            // publishing it here, so clear it rather than leaving the last
            // rendered "Reconnecting in 1s" frozen on screen for the length of
            // the attempt.
            view.reconnectInSeconds = null;
            setConnection(view.connection === 'lost' ? 'lost' : 'connecting');
            publish();
            try {
                if (typeof openStream !== 'function') {
                    throw new Error('openStream is not available');
                }
                const opened = await openStream();
                if (!started || token !== attemptToken) {
                    try { opened.close(); } catch (_e) { /* torn down mid-open */ }
                    return;
                }
                source = opened;
                source.addEventListener(LIVENESS_EVENT, (event) => {
                    let payload = null;
                    try {
                        payload = JSON.parse(event.data);
                    } catch (_e) {
                        payload = null;
                    }
                    applyLivenessFrame(payload);
                });
                source.addEventListener('open', () => {
                    noteFrame();
                    onOpen();
                });
                source.addEventListener('error', () => {
                    // Fast path only. Correctness does not depend on it.
                    declareLost();
                });
                wireEvents(source, noteFrame);
            } catch (_err) {
                // A superseded attempt has already been accounted for; letting
                // it declare loss again would restart the backoff it lost.
                if (!started || token !== attemptToken) return;
                declareLost();
            }
        }

        function start() {
            if (started) return;
            started = true;
            publish();
            if (watchdogTimer === null) {
                watchdogTimer = timers.setInterval(checkWatchdog, WATCHDOG_POLL_MS);
            }
            connect();
        }

        function stop() {
            started = false;
            attemptToken += 1;
            closeSource();
            // A stale deadline left behind here would still be in force on the
            // next start(): a non-null `reconnectAt` parks the watchdog on the
            // countdown branch, which is the branch that never checks silence.
            reconnectAt = null;
            connectStartedAt = null;
            lastFrameAt = null;
            if (reconnectTimer !== null) {
                timers.clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            if (watchdogTimer !== null) {
                timers.clearInterval(watchdogTimer);
                watchdogTimer = null;
            }
        }

        return {
            start,
            stop,
            getStatus: () => Object.assign({}, view),
            // Exposed for the watchdog's own tests and for callers that drive
            // a deterministic clock; production uses the interval above.
            checkWatchdog,
            noteFrame,
        };
    }

    const api = {
        LIVENESS_EVENT,
        LIVE_STREAM_PRESENTATION,
        MISSED_BEACON_TOLERANCE,
        backoffMs,
        createLiveEventStream,
        createLiveStreamIndicator,
        describeLiveStreamStatus,
        formatAge,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
    root.ioLiveEventStream = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
