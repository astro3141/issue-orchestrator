// Reconcile is a sweep, and a sweep that left engines running is not a
// clean reconcile (#326).
//
// The route used to inherit the 120 s per-engine shutdown budget, stop
// nothing without force authorization, and still hand the operator
// "Reconciled 0 stale lock(s), stopped 0 orphaned, stopped 0 unresponsive"
// as a success toast. The response now names the engines it left running,
// so the surface that renders it has to stop claiming success.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadControlCenter(response) {
    const toasts = [];
    const values = new Map();
    const localStorage = {
        getItem: key => (values.has(key) ? values.get(key) : null),
        setItem: (key, value) => values.set(key, value),
    };
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/control_center.js'),
        'utf8',
    ).split("document.addEventListener('DOMContentLoaded'")[0];
    const context = {
        URL,
        console,
        localStorage,
        confirm: () => false,
        window: {
            addEventListener() {},
            matchMedia: () => ({ addEventListener() {}, matches: false }),
            setTimeout() {},
        },
        document: { addEventListener() {}, getElementById: () => null },
        fetch: async () => ({
            ok: true,
            status: 200,
            json: async () => response,
        }),
        setInterval() {},
        setTimeout() {},
        clearTimeout() {},
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    Object.assign(context, {
        showToast: (message, severity) => toasts.push([severity, message]),
        renderRepos() {},
        loadRepos: async () => {},
        loadActivityView() {},
        switchView() {},
    });
    return { context, toasts };
}

// The route derives these sentences from `StopOutcome`; this surface only
// renders whichever one it was handed. Restating the reason here is the
// defect the tests below pin closed.
const TIMED_OUT_DETAIL =
    '1 repository engine(s) left running. The graceful timeout expired while '
    + 'they were still alive. No force escalation was authorized, so no signal '
    + 'was sent. Stop again with force to terminate.';

const FORCE_FAILED_DETAIL =
    '1 repository engine(s) left running. Force escalation was authorized and '
    + 'a kill signal was sent, but they survived it. Stopping again with force '
    + 'is unlikely to help; inspect the processes directly.';

const SUMMARY =
    'Reconciled 0 stale lock(s), stopped 0 orphaned, stopped 0 unresponsive';

function sweep(overrides) {
    return {
        status: 'ok',
        reconciled_stale_locks: [],
        orphaned_detected: [],
        stopped_orphaned: [],
        unresponsive_detected: [],
        stopped_unresponsive: [],
        still_running: [],
        still_running_detail: null,
        ...overrides,
    };
}

test('a reconcile that left engines running is not reported as success', async () => {
    const { context, toasts } = loadControlCenter(sweep({
        still_running: [{
            repo_root: '/repo',
            outcome: 'timed_out',
            instance_id: null,
            pid: null,
            port: 19080,
        }],
        still_running_detail: TIMED_OUT_DETAIL,
    }));

    await context.cleanRecoveryState();

    assert.equal(toasts.length, 1);
    const [severity, message] = toasts[0];
    assert.notEqual(severity, 'success', 'a sweep that stopped nothing claimed success');
    assert.equal(severity, 'warning');
    assert.equal(message, `${SUMMARY}; ${TIMED_OUT_DETAIL}`);
});

test('a failed reconcile escalation is not called an unauthorized one', async () => {
    // `force: true` reaches `stop_by_port`, so a SIGKILL that lost reaches
    // this toast. The surface used to hard-code "because no force escalation
    // was authorized" for it — false on a machine where the kill already ran.
    const { context, toasts } = loadControlCenter(sweep({
        still_running: [{
            repo_root: '/repo',
            outcome: 'force_failed',
            instance_id: null,
            pid: 4242,
            port: 19080,
        }],
        still_running_detail: FORCE_FAILED_DETAIL,
    }));

    await context.cleanRecoveryState();

    assert.equal(toasts.length, 1);
    const [severity, message] = toasts[0];
    assert.equal(severity, 'warning');
    // Exact, so any reason this surface adds of its own fails here.
    assert.equal(message, `${SUMMARY}; ${FORCE_FAILED_DETAIL}`);
    assert.equal(
        /no force escalation was authorized/i.test(message),
        false,
        'a failed escalation was reported as an unauthorized one',
    );
});

test('a reconcile that left nothing running still reports success', async () => {
    const { context, toasts } = loadControlCenter(sweep({
        reconciled_stale_locks: ['/repo'],
    }));

    await context.cleanRecoveryState();

    assert.deepEqual(toasts, [[
        'success',
        'Reconciled 1 stale lock(s), stopped 0 orphaned, stopped 0 unresponsive',
    ]]);
});
