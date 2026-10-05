import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Script, createContext, runInContext } from 'node:vm';
import { spawnSync } from 'node:child_process';
import { createMeasurementRuntime } from '../view/measurement_runtime.mjs';

const html = readFileSync(new URL('../view/index.html', import.meta.url), 'utf8');
const classic = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const moduleScript = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
const SID = '11111111-1111-4111-8111-111111111111';
const USER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const phase = (name, status = 'ACCEPTED') => ({ v: 1, t: 10,
    phase: name, active: name !== 'OFF', session_id: SID,
    ...(name === 'OFF' ? {} : { calibration: { status } }),
});

// Tiny DOM/Chart doubles only: exercise the real inline wiring without a browser framework.
function page() {
    const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
        style: {}, innerText: '', classList: { add() {}, remove() {}, toggle() {} },
        getContext: () => ({}), setAttribute() {}, addEventListener() {},
    }]));
    const charts = [];
    const context = createContext({
        document: { getElementById: id => {
            assert.ok(nodes.has(id), `UI references missing element ${id}`); return nodes.get(id);
        }, querySelectorAll: () => [], querySelector: () => ({ style: {} }) },
        window: { addEventListener() {} }, navigator: {},
        Chart: class { constructor(_ctx, config) { this.data = config.data; charts.push(this); } update() {} },
        Swal: { close() {}, fire() {} },
    });
    runInContext(classic, context);
    return { context, nodes, charts };
}

test('syntax: inline classic/module scripts and auth/runtime modules', () => {
    new Script(classic);
    const sources = [moduleScript, ...['auth.js', 'measurement_phase.mjs', 'measurement_runtime.mjs'].map(
        name => readFileSync(new URL(`../view/${name}`, import.meta.url), 'utf8'))];
    for (const source of sources) {
        const result = spawnSync(process.execPath, ['--input-type=module', '--check'], { input: source, encoding: 'utf8' });
        assert.equal(result.status, 0, result.stderr);
    }
});

test('UI calibration clears previous score/cache/chart; READY needs no click; new state restores display', () => {
    const { context, nodes, charts } = page();
    const model = { phase: 'MEASURING', sessionId: SID, calibration: null,
        connected: true, presentationRevision: 1, bufferedPhases: [] };
    context.window.measurementRuntime = { model, canRender: () => model.phase === 'MEASURING', hasSession: () => true };
    runInContext('renderMeasurementUI(); lastPostureData = { state: "NORMAL", score: 75 }; updateDashboardUI(lastPostureData);', context);
    assert.equal(nodes.get('currentScore').innerText, 75);
    assert.equal(charts[0].data.datasets[0].data.length, 1);
    model.phase = 'CALIBRATING'; model.calibration = { status: 'COLLECTING', progress: 1 };
    model.presentationRevision++;
    runInContext('renderMeasurementUI(); updateDashboardUI({state: "NORMAL", score: 99});', context);
    assert.equal(nodes.get('currentScore').innerText, '—');
    assert.equal(charts[0].data.datasets[0].data.length, 0);
    assert.equal(runInContext('lastPostureData', context), null);
    assert.equal(nodes.get('topStatusMessage').innerText, '측정 준비 중');
    assert.match(nodes.get('measurement-note').innerText, /평소 작업/);
    assert.equal(nodes.get('btn-stop').disabled, false);
    model.phase = 'READY'; runInContext('renderMeasurementUI()', context);
    assert.equal(nodes.get('topStatusMessage').innerText, '측정 준비 완료');
    assert.equal(nodes.get('currentScore').innerText, '—');
    model.phase = 'MEASURING'; runInContext('renderMeasurementUI()', context);
    assert.equal(nodes.get('topStatusMessage').innerText, '측정 중 · 데이터 수신 대기');
    assert.equal(nodes.get('currentScore').innerText, '—');
    runInContext('lastPostureData = { state: "NORMAL", score: 80 }; renderMeasurementUI(); updateDashboardUI(lastPostureData);', context);
    assert.equal(nodes.get('currentScore').innerText, 80);
    assert.equal(nodes.get('measure-status-text').innerText, '측정 중');
    assert.equal(charts[0].data.datasets[0].data.length, 1);
    runInContext('changeLanguage("en")', context);
    assert.equal(charts[0].data.datasets[0].data.length, 1); // No fabricated duplicate chart samples.
});

test('UI module wiring restores authenticated socket, start/phase/state/stop, and failure/retry controls', async () => {
    const { context, nodes, charts } = page();
    let callback, socket;
    const calls = [], events = new Map();
    Object.assign(context, {
        location: { hash: '', hostname: '127.0.0.1' },
        localStorage: { setItem() {}, removeItem() {} },
        createMeasurementRuntime,
        supabase: { auth: {
            getSession: async () => ({ data: { session: null } }),
            onAuthStateChange: handler => { callback = handler; },
        } },
        authenticatedFetch: async url => {
            const starting = url.endsWith('/start'); calls.push(starting ? 'start' : 'stop');
            return new Response(JSON.stringify({ status: 'success', measurement: {
                user_id: USER, session_id: SID, status: starting ? 'ACTIVE' : 'STOPPED',
            } }));
        },
        io: (_url, options) => {
            socket = { auth: options.auth, connected: false, active: true,
                on: (name, handler) => events.set(name, [...(events.get(name) || []), handler]),
                off: (name, handler) => events.set(name, events.get(name).filter(fn => fn !== handler)),
                connect() {}, disconnect() {},
            }; return socket;
        },
    });
    runInContext(moduleScript.replace(/^\s*import .*;$/gm, ''), context);
    await Promise.resolve();
    assert.equal(socket, undefined);
    callback('SIGNED_IN', { user: { id: USER }, access_token: 'test-token' });
    const fire = (name, event) => { for (const handler of events.get(name) || []) handler(event); };
    socket.connected = true; fire('connect');
    const starting = context.startMeasurement();
    fire('measurement_phase', phase('CALIBRATING', 'COLLECTING'));
    fire('state', { v: 1, state: 'NORMAL', score: 98 });
    assert.equal(nodes.get('currentScore').innerText, '—');
    await starting;
    assert.equal(nodes.get('btn-stop').disabled, false);
    fire('measurement_phase', phase('READY'));
    fire('measurement_phase', phase('MEASURING'));
    fire('state', { v: 1, state: 'NORMAL', score: 85, metrics: {} });
    assert.equal(nodes.get('currentScore').innerText, 85);
    assert.equal(charts[0].data.datasets[0].data.length, 1);
    await context.stopMeasurement();
    assert.equal(nodes.get('currentScore').innerText, '—');
    assert.deepEqual(calls, ['start', 'stop']);
    // Fresh generation synchronizing to a failed Backend session: retry UI, never normal score.
    callback('SIGNED_OUT', null);
    callback('SIGNED_IN', { user: { id: USER }, access_token: 'test-token' });
    socket.connected = true; fire('connect');
    fire('measurement_phase', phase('CALIBRATING', 'FAILED'));
    assert.equal(nodes.get('btn-retry').hidden, false);
    assert.equal(nodes.get('btn-retry').disabled, false);
    assert.match(nodes.get('topStatusMessage').innerText, /완료하지 못/);
    assert.equal(nodes.get('currentScore').innerText, '—');
});
