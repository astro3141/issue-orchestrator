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

test('a reconcile that left engines running is not reported as success', async () => {
    const { context, toasts } = loadControlCenter({
        status: 'ok',
        reconciled_stale_locks: [],
        orphaned_detected: [],
        stopped_orphaned: [],
        unresponsive_detected: [],
        stopped_unresponsive: [],
        still_running: [
            { repo_root: '/repo', instance_id: null, pid: null, port: 19080 },
        ],
    });

    await context.cleanRecoveryState();

    assert.equal(toasts.length, 1);
    const [severity, message] = toasts[0];
    assert.notEqual(severity, 'success', 'a sweep that stopped nothing claimed success');
    assert.equal(severity, 'warning');
    assert.match(message, /1 engine\(s\) left running/);
    assert.match(message, /no force escalation was authorized/);
});

test('a reconcile that left nothing running still reports success', async () => {
    const { context, toasts } = loadControlCenter({
        status: 'ok',
        reconciled_stale_locks: ['/repo'],
        orphaned_detected: [],
        stopped_orphaned: [],
        unresponsive_detected: [],
        stopped_unresponsive: [],
        still_running: [],
    });

    await context.cleanRecoveryState();

    assert.deepEqual(toasts, [[
        'success',
        'Reconciled 1 stale lock(s), stopped 0 orphaned, stopped 0 unresponsive',
    ]]);
});
