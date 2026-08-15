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

test('applyLiveStreamStatus writes a glyph node, a text node and a state hook', () => {
    const created = [];
    const element = {
        className: '',
        attributes: {},
        children: [],
        textContent: '',
        setAttribute(name, value) { this.attributes[name] = value; },
        appendChild(child) { this.children.push(child); },
    };
    const doc = {
        createElement: () => {
            const node = { className: '', textContent: '', attributes: {},
                setAttribute(n, v) { this.attributes[n] = v; } };
            created.push(node);
            return node;
        },
    };

    liveStream.applyLiveStreamStatus(element, {
        connection: 'lost', engine: 'unknown', reconnectInSeconds: 5,
    }, doc);

    assert.equal(element.attributes['data-live-state'], 'lost');
    assert.equal(element.attributes['data-connection'], 'lost');
    assert.equal(element.attributes['data-engine'], 'unknown');
    assert.match(element.className, /live-stream-status--lost/);
    assert.equal(element.children.length, 2);
    assert.equal(element.children[0].attributes['aria-hidden'], 'true');
    assert.match(element.children[1].textContent, /cached snapshot/);
});

test('formatAge stays readable across seconds, minutes and hours', () => {
    assert.equal(liveStream.formatAge(null), 'never');
    assert.equal(liveStream.formatAge(3), '3s ago');
    assert.equal(liveStream.formatAge(125), '2m ago');
    assert.equal(liveStream.formatAge(7300), '2h ago');
});
