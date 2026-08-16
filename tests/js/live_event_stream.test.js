// The dashboard's live subscription must detect a dead stream on its own (#44).
//
// The regression these guard: the page used to rely on `EventSource.onerror`
// to notice a disconnect. A browser-level measurement in this repo showed
// readyState stuck at OPEN with no error fired 25 seconds after the engine
// was killed — so every test below drives the failure *without* emitting an
// error event. If the watchdog stops working, they fail; if only the error
// fast-path works, they still fail.
const test = require('node:test');
const assert = require('node:assert');

const liveStream = require('../../src/issue_orchestrator/static/js/dashboard/live_event_stream.js');

function fakeEventSource() {
    const listeners = new Map();
    return {
        closed: false,
        addEventListener(name, fn) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(fn);
        },
        close() { this.closed = true; },
        emit(name, data) {
            (listeners.get(name) || []).forEach((fn) => fn({ data }));
        },
        has(name) { return listeners.has(name); },
    };
}

// A clock the test advances by hand: no sleeps, no real timers.
function fakeClock() {
    let now = 0;
    let nextId = 1;
    const timeouts = new Map();
    const intervals = new Map();
    return {
        timers: {
            now: () => now,
            setTimeout: (fn, ms) => {
                const id = nextId++;
                timeouts.set(id, { fn, at: now + ms });
                return id;
            },
            clearTimeout: (id) => timeouts.delete(id),
            setInterval: (fn, ms) => {
                const id = nextId++;
                intervals.set(id, { fn, every: ms, next: now + ms });
                return id;
            },
            clearInterval: (id) => intervals.delete(id),
            random: () => 0,
        },
        intervalCount: () => intervals.size,
        timeoutCount: () => timeouts.size,
        advance(ms) {
            const target = now + ms;
            let guard = 0;
            for (;;) {
                if (guard++ > 10000) throw new Error('clock advance did not settle');
                let soonest = null;
                for (const [id, t] of timeouts) {
                    if (t.at <= target && (soonest === null || t.at < soonest.at)) {
                        soonest = { kind: 'timeout', id, at: t.at };
                    }
                }
                for (const [id, i] of intervals) {
                    if (i.next <= target && (soonest === null || i.next < soonest.at)) {
                        soonest = { kind: 'interval', id, at: i.next };
                    }
                }
                if (soonest === null) break;
                now = soonest.at;
                if (soonest.kind === 'timeout') {
                    const entry = timeouts.get(soonest.id);
                    timeouts.delete(soonest.id);
                    entry.fn();
                } else {
                    const entry = intervals.get(soonest.id);
                    entry.next = now + entry.every;
                    entry.fn();
                }
            }
            now = target;
        },
    };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function beacon(overrides = {}) {
    return JSON.stringify(Object.assign({
        state: 'advancing',
        tick_id: 485,
        seconds_since_tick: 2.0,
        phase: '',
        interval_seconds: 10,
        stall_threshold_seconds: 120,
        schema: 1,
    }, overrides));
}

async function startedStream(options = {}) {
    const clock = fakeClock();
    const sources = [];
    const statuses = [];
    const opens = [];
    const stream = liveStream.createLiveEventStream(Object.assign({
        target: globalThis,
        timers: clock.timers,
        openStream: async () => {
            const source = fakeEventSource();
            sources.push(source);
            return source;
        },
        wireEvents: () => {},
        onOpen: () => opens.push(clock.timers.now()),
        onStatus: (status) => statuses.push(status),
    }, options));
    stream.start();
    await flush();
    return { clock, sources, statuses, opens, stream };
}

// ---------------------------------------------------------------------------
// The watchdog — the part that does not depend on the browser reporting a fault
// ---------------------------------------------------------------------------

test('a stream that goes silent is declared lost without any error event', async () => {
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon());
    assert.equal(stream.getStatus().connection, 'live');

    // 30s of silence on a 10s cadence (the deadline is 2.5 intervals). No
    // 'error' is ever emitted — this is exactly the half-open socket the
    // browser never reports.
    clock.advance(30000);

    assert.equal(stream.getStatus().connection, 'lost');
    assert.equal(sources[0].closed, true, 'the dead source must be closed');
});

test('a stream still receiving beacons is not declared lost', async () => {
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');
    for (let i = 0; i < 10; i += 1) {
        sources[0].emit('engine.liveness', beacon());
        clock.advance(9000);
    }

    assert.equal(stream.getStatus().connection, 'live');
});

test('the deadline follows the server-declared cadence, not a client constant', async () => {
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon({ interval_seconds: 60 }));

    // 25s would have tripped the default 10s cadence; on a 60s cadence it
    // must not, or a server retune would produce false disconnects.
    clock.advance(25000);
    assert.equal(stream.getStatus().connection, 'live');

    clock.advance(160000);
    assert.equal(stream.getStatus().connection, 'lost');
});

test('any event frame feeds the watchdog, not only the beacon', async () => {
    const wired = [];
    const { clock, sources, stream } = await startedStream({
        wireEvents: (source, noteFrame) => {
            source.addEventListener('session.started', () => noteFrame());
            wired.push(source);
        },
    });
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon());

    for (let i = 0; i < 5; i += 1) {
        clock.advance(20000);
        sources[0].emit('session.started', '{}');
    }

    assert.equal(wired.length, 1);
    assert.equal(stream.getStatus().connection, 'live');
});

// ---------------------------------------------------------------------------
// Reconnection — always through openStream, which mints a fresh token
// ---------------------------------------------------------------------------

test('a connect attempt that never produces a frame is retried', async () => {
    // The same silence, moved from an established stream to the connect
    // attempt: the socket is accepted and then says nothing — no 'open', no
    // 'error'. Before the connect deadline existed, the page parked on
    // "Reconnecting in 1s" forever because nothing was timing the attempt.
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon());

    clock.advance(30000);
    assert.equal(stream.getStatus().connection, 'lost');

    clock.advance(2000);
    await flush();
    assert.equal(sources.length, 2, 'reconnect must open a new stream');

    // sources[1] is deliberately left silent — it never emits 'open' and
    // never emits 'error'.
    clock.advance(30000);
    await flush();

    assert.ok(sources.length >= 3,
        `a silent connect attempt must be retried, got ${sources.length} attempts`);
    assert.equal(sources[1].closed, true, 'the silent attempt must be closed');
});

test('a hung openStream does not park the page on a countdown forever', async () => {
    // `fetch('/api/sse-token')` has no default timeout, so the token request
    // itself can hang on exactly the network condition this module exists for.
    // Nothing else can notice that: `openStream` simply never settles.
    let calls = 0;
    const clock = fakeClock();
    const stream = liveStream.createLiveEventStream({
        target: globalThis,
        timers: clock.timers,
        openStream: () => {
            calls += 1;
            return new Promise(() => {});
        },
    });
    stream.start();
    await flush();
    assert.equal(calls, 1);

    clock.advance(30000);
    await flush();

    assert.ok(calls >= 2, `a hung connect must be abandoned and retried, got ${calls}`);
    assert.equal(stream.getStatus().connection, 'lost');
});

test('the first connect is under the same deadline as every later one', async () => {
    // The initial attempt had a second hole: `lastFrameAt` was null, so the
    // watchdog returned early and the page sat at "Connecting…" indefinitely.
    let calls = 0;
    const clock = fakeClock();
    const stream = liveStream.createLiveEventStream({
        target: globalThis,
        timers: clock.timers,
        openStream: async () => {
            calls += 1;
            return fakeEventSource();  // never emits anything
        },
    });
    stream.start();
    await flush();
    assert.equal(stream.getStatus().connection, 'connecting');

    clock.advance(30000);
    await flush();

    assert.ok(calls >= 2, `the first attempt must be timed too, got ${calls}`);
});

test('an abandoned attempt cannot install its source when it finally settles', async () => {
    // The race the attempt token closes: the watchdog gives up at 25s, a new
    // attempt is already running, and *then* the old promise resolves. Without
    // the token that stale source becomes `source` and gets wired up, leaving
    // two live subscriptions and a spent token replaying.
    const resolvers = [];
    const clock = fakeClock();
    const stream = liveStream.createLiveEventStream({
        target: globalThis,
        timers: clock.timers,
        openStream: () => new Promise((resolve) => resolvers.push(resolve)),
    });
    stream.start();
    await flush();
    assert.equal(resolvers.length, 1);

    clock.advance(30000);
    await flush();
    assert.equal(resolvers.length, 2, 'the hung attempt must have been replaced');

    const stale = fakeEventSource();
    resolvers[0](stale);
    await flush();

    assert.equal(stale.closed, true, 'the abandoned source must be closed');
    assert.equal(stale.has('engine.liveness'), false,
        'an abandoned attempt must not be wired up as the live subscription');
});

test('a lost stream reconnects through openStream and returns to live', async () => {
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon());

    clock.advance(30000);
    assert.equal(stream.getStatus().connection, 'lost');

    clock.advance(2000);
    await flush();
    assert.equal(sources.length, 2, 'reconnect must open a new stream');

    sources[1].emit('open');
    sources[1].emit('engine.liveness', beacon());
    assert.equal(stream.getStatus().connection, 'live');
});

test('a failing openStream keeps retrying with backoff instead of giving up', async () => {
    let attempts = 0;
    const clock = fakeClock();
    const stream = liveStream.createLiveEventStream({
        target: globalThis,
        timers: clock.timers,
        openStream: async () => {
            attempts += 1;
            throw new Error('sse-token request failed (401)');
        },
    });
    stream.start();
    await flush();
    assert.equal(attempts, 1);
    assert.equal(stream.getStatus().connection, 'lost');

    for (let i = 0; i < 4; i += 1) {
        clock.advance(60000);
        await flush();
    }

    assert.ok(attempts >= 4, `expected repeated retries, got ${attempts}`);
});

test('an error event closes the source immediately (no spent-token replay)', async () => {
    const { sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('error');

    assert.equal(sources[0].closed, true);
    assert.equal(stream.getStatus().connection, 'lost');
});

test('stop() closes the stream and cancels its timers', async () => {
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');

    stream.stop();

    assert.equal(sources[0].closed, true);
    assert.equal(clock.intervalCount(), 0);
    assert.equal(clock.timeoutCount(), 0);
});

// ---------------------------------------------------------------------------
// Engine liveness is a separate answer from transport liveness
// ---------------------------------------------------------------------------

test('a stalled engine is reported while the stream itself stays live', async () => {
    const { sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon({
        state: 'stalled', seconds_since_tick: 600, phase: 'planning',
    }));

    const status = stream.getStatus();
    assert.equal(status.connection, 'live');
    assert.equal(status.engine, 'stalled');
    assert.equal(status.phase, 'planning');
});

test('losing the stream drops the last engine reading rather than keeping it', async () => {
    const { clock, sources, stream } = await startedStream();
    sources[0].emit('open');
    sources[0].emit('engine.liveness', beacon({ state: 'advancing' }));

    clock.advance(30000);

    assert.equal(stream.getStatus().engine, 'unknown',
        'a dead stream must not keep advertising the engine as advancing');
});

// ---------------------------------------------------------------------------
// Human-visible status
// ---------------------------------------------------------------------------

test('every state renders text plus a distinct glyph, never colour alone', () => {
    const states = [
        { connection: 'connecting', engine: 'unknown' },
        { connection: 'live', engine: 'advancing', tickId: 485, secondsSinceTick: 3 },
        { connection: 'live', engine: 'stalled', secondsSinceTick: 600, phase: 'planning' },
        { connection: 'live', engine: 'unknown' },
        { connection: 'lost', engine: 'unknown', reconnectInSeconds: 8 },
    ];

    const described = states.map((s) => liveStream.describeLiveStreamStatus(s));

    described.forEach((d) => {
        assert.ok(d.text && d.text.length > 0, 'every state must have text');
        assert.ok(d.icon && d.icon.length > 0, 'every state must have a glyph');
        assert.ok(d.tone && d.tone.length > 0);
    });
    assert.equal(new Set(described.map((d) => d.tone)).size, 5,
        'each state needs its own tone class');
});

test('the lost state names the consequence, not just the disconnection', () => {
    const described = liveStream.describeLiveStreamStatus({
        connection: 'lost', engine: 'unknown', reconnectInSeconds: 8,
    });

    assert.match(described.text, /cached snapshot, not live/);
    assert.match(described.text, /Reconnecting in 8s/);
});

test('the live state names the engine tick, so liveness is engine-backed', () => {
    const described = liveStream.describeLiveStreamStatus({
        connection: 'live', engine: 'advancing', tickId: 485, secondsSinceTick: 3,
    });

    assert.match(described.text, /engine tick 485/);
    assert.match(described.text, /3s ago/);
});

test('a stalled engine is reported with its age and phase', () => {
    const described = liveStream.describeLiveStreamStatus({
        connection: 'live', engine: 'stalled', secondsSinceTick: 600, phase: 'planning',
    });

    assert.match(described.text, /not completing ticks/);
    assert.match(described.text, /10m ago/);
    assert.match(described.text, /phase: planning/);
});

// A node that remembers every write to `textContent`, because the thing under
// test is not only *what* the indicator says but *how often it says it*: an
// `aria-live` region that is rewritten on every beacon announces itself to a
// screen reader every ten seconds, forever, on a perfectly healthy dashboard.
function stubNode(className) {
    return {
        className,
        attributes: {},
        children: [],
        writes: [],
        _text: '',
        get textContent() { return this._text; },
        set textContent(value) { this._text = value; this.writes.push(value); },
        setAttribute(name, value) { this.attributes[name] = value; },
        appendChild(child) { this.children.push(child); },
        querySelector(selector) {
            const wanted = selector.replace(/^\./, '');
            return this.children.find(
                (child) => String(child.className).split(/\s+/).indexOf(wanted) !== -1,
            ) || null;
        },
    };
}

function stubIndicator() {
    const element = stubNode('live-stream-status');
    const doc = { createElement: () => stubNode('') };
    const indicator = liveStream.createLiveStreamIndicator(element, doc);
    return {
        element,
        indicator,
        icon: () => element.querySelector('.live-stream-status__icon'),
        text: () => element.querySelector('.live-stream-status__text'),
        announcement: () => element.querySelector('.live-stream-status__announcement'),
    };
}

test('the indicator writes a glyph node, a text node and the state hooks', () => {
    const { element, indicator, icon, text } = stubIndicator();

    indicator.render({ connection: 'lost', engine: 'unknown', reconnectInSeconds: 5 });

    assert.equal(element.attributes['data-live-state'], 'lost');
    assert.equal(element.attributes['data-connection'], 'lost');
    assert.equal(element.attributes['data-engine'], 'unknown');
    assert.match(element.className, /live-stream-status--lost/);
    assert.equal(icon().attributes['aria-hidden'], 'true');
    assert.match(text().textContent, /cached snapshot/);
});

test('a healthy stream announces once, however many beacons arrive', () => {
    const { indicator, text, announcement } = stubIndicator();

    for (let i = 0; i < 10; i += 1) {
        indicator.render({
            connection: 'live', engine: 'advancing', tickId: 480 + i, secondsSinceTick: i,
        });
    }

    assert.equal(announcement().writes.length, 1,
        'ten beacons in one state must produce exactly one announcement');
    assert.equal(text().writes.length, 10,
        'the visible text must still track the detail on every beacon');
    assert.match(text().textContent, /engine tick 489/);
});

test('the reconnect countdown is visible but never announced second by second', () => {
    const { indicator, text, announcement } = stubIndicator();
    indicator.render({ connection: 'live', engine: 'advancing', tickId: 12 });

    for (let seconds = 8; seconds > 0; seconds -= 1) {
        indicator.render({ connection: 'lost', engine: 'unknown', reconnectInSeconds: seconds });
    }

    assert.equal(announcement().writes.length, 2,
        'the whole backoff window is one announcement, not one per second');
    assert.match(announcement().textContent, /cached snapshot, not live/);
    assert.match(text().textContent, /Reconnecting in 1s/);
});

test('every state transition is announced exactly once', () => {
    const { indicator, announcement } = stubIndicator();

    indicator.render({ connection: 'connecting', engine: 'unknown' });
    indicator.render({ connection: 'live', engine: 'advancing', tickId: 1, secondsSinceTick: 1 });
    indicator.render({ connection: 'live', engine: 'advancing', tickId: 2, secondsSinceTick: 1 });
    indicator.render({ connection: 'live', engine: 'stalled', secondsSinceTick: 600 });
    indicator.render({ connection: 'lost', engine: 'unknown', reconnectInSeconds: 2 });

    assert.equal(announcement().writes.length, 4,
        'four state changes, four announcements — the repeat beacon adds none');
});

test('no announcement embeds detail that can go stale between transitions', () => {
    // The invariant that makes "announce only on change" safe: an announcement
    // is a fixed sentence per state, so it cannot still be on screen claiming
    // "3s ago" a minute later.
    Object.keys(liveStream.LIVE_STREAM_PRESENTATION).forEach((state) => {
        const row = liveStream.LIVE_STREAM_PRESENTATION[state];
        assert.equal(typeof row.announcement, 'string',
            `${state} must announce a fixed sentence, not a rendered view`);
        assert.ok(row.announcement.length > 0, `${state} must announce something`);
        assert.ok(!/\d/.test(row.announcement),
            `${state} announcement must not embed a changing number`);
    });
});

test('the polite region keeps its identity and its ARIA across renders', () => {
    const { indicator, element, announcement } = stubIndicator();
    indicator.render({ connection: 'connecting', engine: 'unknown' });
    const first = announcement();

    indicator.render({ connection: 'live', engine: 'advancing', tickId: 3 });
    indicator.render({ connection: 'lost', engine: 'unknown', reconnectInSeconds: 4 });

    assert.equal(announcement(), first,
        'a live region rebuilt on every render is a new region each time');
    assert.equal(first.attributes.role, 'status');
    assert.equal(first.attributes['aria-live'], 'polite');
    assert.match(first.className, /visually-hidden/);
    assert.equal(element.children.length, 3);
});

test('the indicator adopts the server-rendered nodes instead of replacing them', () => {
    // The template ships the region already in the DOM, empty, because a live
    // region has to exist *before* its content changes to be announced at all.
    const element = stubNode('live-stream-status');
    const serverIcon = stubNode('live-stream-status__icon');
    const serverText = stubNode('live-stream-status__text');
    const serverRegion = stubNode('live-stream-status__announcement visually-hidden');
    element.appendChild(serverIcon);
    element.appendChild(serverText);
    element.appendChild(serverRegion);

    const indicator = liveStream.createLiveStreamIndicator(element, {
        createElement: () => { throw new Error('must reuse the server-rendered nodes'); },
    });
    indicator.render({ connection: 'live', engine: 'advancing', tickId: 7, secondsSinceTick: 1 });

    assert.equal(element.children.length, 3);
    assert.match(serverText.textContent, /engine tick 7/);
    assert.equal(serverRegion.writes.length, 1);
});

test('an indicator with no element fails loudly rather than rendering nowhere', () => {
    assert.throws(() => liveStream.createLiveStreamIndicator(null, {}), /missing/);
});

test('formatAge stays readable across seconds, minutes and hours', () => {
    assert.equal(liveStream.formatAge(null), 'never');
    assert.equal(liveStream.formatAge(3), '3s ago');
    assert.equal(liveStream.formatAge(125), '2m ago');
    assert.equal(liveStream.formatAge(7300), '2h ago');
});
