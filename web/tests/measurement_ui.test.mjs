import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Script, createContext, runInContext } from 'node:vm';
import { spawnSync } from 'node:child_process';
import { createMeasurementRuntime } from '../view/measurement_runtime.mjs';
import { createHistoryBrowser } from '../view/measurement_history.mjs';

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
function page({ mediaDevices } = {}) {
    const eventHandlers = new Map();
    const makeNode = () => ({
        style: {}, innerText: '', textContent: '', hidden: false, disabled: false, value: '',
        children: [], classList: { add() {}, remove() {}, toggle() {} },
        getContext: () => ({}), setAttribute() {},
        addEventListener(name, handler) { eventHandlers.set(name, handler); },
        appendChild(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        play: async () => {},
    });
    const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, makeNode()]));
    const windowEvents = new Map();
    const charts = [];
    const alerts = [];
    let alertCloses = 0;
    const context = createContext({
        document: { getElementById: id => {
            assert.ok(nodes.has(id), `UI references missing element ${id}`); return nodes.get(id);
        }, querySelectorAll: () => [], querySelector: () => makeNode(), createElement: () => makeNode() },
        window: { addEventListener: (name, handler) => windowEvents.set(name, handler) },
        navigator: { ...(mediaDevices ? { mediaDevices } : {}) },
        Chart: class { constructor(_ctx, config) { this.data = config.data; charts.push(this); } update() {} },
        Swal: { close() { alertCloses++; }, fire(options) { alerts.push(options); } },
    });
    runInContext(classic, context);
    return { context, nodes, charts, windowEvents, eventHandlers, alerts,
        alertCloses: () => alertCloses };
}

test('syntax: inline classic/module scripts and auth/runtime modules', () => {
    new Script(classic);
    const sources = [moduleScript, ...[
        'auth.js', 'auth_redirect.mjs', 'measurement_phase.mjs',
        'measurement_runtime.mjs', 'measurement_history.mjs',
    ].map(
        name => readFileSync(new URL(`../view/${name}`, import.meta.url), 'utf8'))];
    for (const source of sources) {
        const result = spawnSync(process.execPath, ['--input-type=module', '--check'], { input: source, encoding: 'utf8' });
        assert.equal(result.status, 0, result.stderr);
    }
});

test('ABSENT hides score and instantaneous metrics, adds no chart point, then recovers', () => {
    const { context, nodes, charts } = page();
    runInContext('isMeasuring = true', context);
    runInContext(`updateDashboardUI({ state: "ABSENT", score: 73,
        metrics: { balance: "LEFT", blink_rate: 3, face_distance_cm: 20, static_hold_sec: 99 } })`, context);
    assert.equal(nodes.get('currentScore').innerText, '—');
    assert.equal(nodes.get('val-balance').innerText, '—');
    assert.equal(nodes.get('val-blink').innerText, '—');
    assert.equal(charts[0].data.datasets[0].data.length, 0);
    assert.equal(nodes.get('topStatusLabel').innerText, '자리 비움');
    runInContext(`updateDashboardUI({ state: "NORMAL", score: 98,
        metrics: { balance: "CENTER", blink_rate: 12, face_distance_cm: 60, static_hold_sec: 2 } })`, context);
    assert.equal(nodes.get('currentScore').innerText, 98);
    assert.equal(nodes.get('val-balance').innerText, '균형 (정상)');
    assert.equal(charts[0].data.datasets[0].data.length, 1);
});

test('state description uses Backend reasons without guessing Phase C classifications', () => {
    const { context, nodes } = page();
    runInContext('isMeasuring = true', context);
    runInContext('updateDashboardUI({ state: "DANGER", score: 90, reasons: ["imbalance"] })', context);
    assert.match(nodes.get('posture-sub').innerText, /좌우 하중 차이/);
    assert.doesNotMatch(nodes.get('posture-sub').innerText, /FACE_ONLY|BODY_FORWARD|장시간 정적/);
});

test('camera preview starts only on demand, stops on change/pagehide, and is blocked during measurement', async () => {
    let opens = 0, stops = 0;
    const listeners = new Map();
    const stream = { getTracks: () => [{ stop: () => { stops++; } }],
        getVideoTracks: () => [{ getSettings: () => ({ deviceId: 'cam-1' }), label: 'Camera 1' }] };
    const mediaDevices = {
        enumerateDevices: async () => [{ kind: 'videoinput', deviceId: 'cam-1', label: 'Camera 1' }],
        getUserMedia: async () => { opens++; return stream; },
        addEventListener: (name, handler) => listeners.set(name, handler),
    };
    const { context, nodes, windowEvents } = page({ mediaDevices });
    assert.equal(opens, 0);
    await context.startCameraTest();
    assert.equal(opens, 1);
    assert.equal(nodes.get('webcamPreview').hidden, false);
    context.changeCamera('cam-2');
    assert.equal(stops, 1);
    await context.startCameraTest();
    context.switchView('monitoring', { classList: { add() {} } });
    assert.equal(stops, 2);
    await context.startCameraTest();
    windowEvents.get('pagehide')();
    assert.equal(stops, 3);
    context.window.measurementRuntime = { model: { phase: 'MEASURING', sessionId: SID, pendingAction: null } };
    await context.startCameraTest();
    assert.equal(opens, 3);
    assert.match(nodes.get('camera-test-status').innerText, /측정 중/);
});

test('Front-only feedback demo supports NORMAL/CAUTION/BREAK, marks tests, and blocks during measurement', () => {
    const { context, nodes, alerts, alertCloses } = page();
    for (const level of ['NORMAL', 'CAUTION', 'BREAK']) {
        assert.equal(context.showTestFeedback(level), true);
    }
    assert.equal(alerts.length, 3);
    assert.ok(alerts.every(alert => alert.title.startsWith('[테스트]')));
    assert.ok(alerts.every(alert => alert.text.includes('실제 측정 결과가 아닙니다')));
    assert.equal(alerts[0].timer, 5000);
    assert.equal(alerts[1].timer, 5000);
    assert.equal(alerts[2].timer, undefined);
    assert.equal(alerts[2].showCloseButton, true);
    context.closeTestFeedback();
    assert.equal(alertCloses(), 1);

    context.window.measurementRuntime = {
        model: { phase: 'MEASURING', sessionId: SID, pendingAction: null },
    };
    assert.equal(context.showTestFeedback('CAUTION'), false);
    assert.equal(alerts.length, 3);
    assert.match(nodes.get('feedback-test-status').innerText, /측정.*사용할 수 없습니다/);
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

test('Chair-only calibration shows Vision 0/0 and nonzero Chair progress without enabling score', () => {
    const { context, nodes } = page();
    const model = { phase: 'CALIBRATING', sessionId: SID, connected: true,
        presentationRevision: 1, pendingAction: null, calibration: {
            status: 'COLLECTING', progress: .8,
            valid_samples: { vision: 0, chair_ir: 4 }, required_samples: { vision: 0, chair_ir: 5 },
        } };
    context.window.measurementRuntime = { model, canRender: () => false, hasSession: () => true };
    runInContext('renderMeasurementUI()', context);
    assert.match(nodes.get('measurement-note').innerText, /Chair-only.*80%.*Vision 0\/0.*Chair IR 4\/5/);
    assert.match(nodes.get('measurement-note').innerText, /사용 안 함/);
    assert.equal(nodes.get('currentScore').innerText, '—');
    assert.equal(nodes.get('btn-stop').disabled, false);
});

test('UI module wiring restores authenticated socket, start/phase/state/stop, and failure/retry controls', async () => {
    const { context, nodes, charts } = page();
    let callback, socket;
    const calls = [], events = new Map();
    Object.assign(context, {
        location: { hash: '', hostname: '127.0.0.1' },
        localStorage: { setItem() {}, removeItem() {} },
        createMeasurementRuntime, createHistoryBrowser,
        supabase: { auth: {
            getSession: async () => ({ data: { session: null } }),
            onAuthStateChange: handler => { callback = handler; },
        } },
        authenticatedFetch: async url => {
            if (url.includes('/api/state/history')) {
                return new Response(JSON.stringify(url.endsWith('/sessions') ?
                    { v: 1, status: 'success', user_id: USER, sessions: [{ session_id: SID,
                        first_measured_at: '2026-10-14T00:29:30Z', last_measured_at: '2026-10-14T00:30:00Z', snapshot_count: 2 }] } :
                    { v: 1, status: 'success', stream: { user_id: USER, session_id: SID },
                        period: { start: '2026-10-14T00:00:00Z', end: '2026-10-14T00:30:00Z' }, states: [
                            { measured_at: '2026-10-14T00:29:30Z', state: 'NORMAL', score: 90 },
                            { measured_at: '2026-10-14T00:30:00Z', state: 'CAUTION', score: 85 },
                        ] }));
            }
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
        aggregateHistory: () => ({ points: [], durationMs: { NORMAL: 0, CAUTION: 0, DANGER: 0, ABSENT: 0 }, transitions: [], sampleCount: 0 }),
        exampleHistoryPayload: () => ({ period: {}, states: [] }),
        historyRequestUrl: () => 'http://backend.test/api/state/history',
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
    for (let n = 0; n < 12; n++) await Promise.resolve();
    assert.equal(nodes.get('history-session').value, SID);
    assert.equal(nodes.get('history-session').disabled, false);
    assert.match(nodes.get('history-status').innerText, /마지막 snapshot/);
    assert.deepEqual(Array.from(context.window.timelineChartObj.data.datasets[0].data), [90, 85]);
    assert.deepEqual(Array.from(context.window.pieChartObj.data.datasets[0].data), [30, 0, 0, 0]);
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
