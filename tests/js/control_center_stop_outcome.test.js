// The operator must be told what actually happened to the engine (#326).
//
// #324 stopped an engine non-gracefully and the Control Center still said
// it stopped. The route now answers 409 `engine_still_running` when the
// engine was left running, so the surface that renders that answer has to
// keep it truthful: an engine that is still up cannot produce the
// "engine stopping" success toast, and cannot be read as "already stopped".
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const STILL_RUNNING_DETAIL =
    'The repository engine did not stop within the graceful timeout and is '
    + 'still running. No force escalation was authorized, so it was left '
    + 'running and no signal was sent. Stop it again with force to terminate it.';

// The other reachable still-running case: the escalation the operator did
// authorize ran, and the engine survived it (#326).
const FORCE_FAILED_DETAIL =
    'The repository engine did not stop. Force escalation was authorized and '
    + 'a kill signal was sent, but the engine is still running. Stopping it '
    + 'again with force is unlikely to help; inspect the process directly.';

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
        window: {
            addEventListener() {},
            matchMedia: () => ({ addEventListener() {}, matches: false }),
            setTimeout() {},
        },
        document: { addEventListener() {}, getElementById: () => null },
        fetch: async () => response,
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

function jsonResponse(status, payload) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => payload,
    };
}

test('an engine left running is reported as an error, never as stopping', async () => {
    const { context, toasts } = loadControlCenter(jsonResponse(409, {
        error: 'engine_still_running',
        detail: STILL_RUNNING_DETAIL,
        repo_root: '/repo',
        stopped_count: 0,
        still_running: [{ instance_id: null, pid: 4242, port: 19080 }],
    }));

    await context.stopRepo('/repo', { force: false });

    assert.deepEqual(toasts, [['error', STILL_RUNNING_DETAIL]]);
});

test('a confirmed stop still reports success', async () => {
    const { context, toasts } = loadControlCenter(jsonResponse(200, {
        status: 'stopped',
        repo_root: '/repo',
        stopped_count: 1,
    }));

    await context.stopRepo('/repo', { force: false });

    assert.equal(toasts.length, 1);
    assert.equal(toasts[0][0], 'success');
});

test('nothing running is still reported as already stopped', async () => {
    const { context, toasts } = loadControlCenter(jsonResponse(200, {
        status: 'not_running',
        repo_root: '/repo',
    }));

    await context.stopRepo('/repo', { force: false });

    assert.deepEqual(toasts, [['info', 'Repository engine was already stopped']]);
});

test('a failed force escalation is surfaced as its own reason', async () => {
    const { context, toasts } = loadControlCenter(jsonResponse(409, {
        error: 'engine_still_running',
        detail: FORCE_FAILED_DETAIL,
        repo_root: '/repo',
        stopped_count: 0,
        still_running: [{ instance_id: null, pid: 4242, port: 19080 }],
    }));

    await context.stopRepo('/repo', { force: true });

    assert.deepEqual(toasts, [['error', FORCE_FAILED_DETAIL]]);
});
