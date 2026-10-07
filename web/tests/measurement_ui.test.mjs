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
    const toast = { visible: false, current: null, shown: [], closes: 0 };
    const context = createContext({
        document: { getElementById: id => {
            assert.ok(nodes.has(id), `UI references missing element ${id}`); return nodes.get(id);
        }, querySelectorAll: () => [], querySelector: () => ({ style: {} }) },
        window: { addEventListener() {} }, navigator: {},
        Chart: class { constructor(_ctx, config) { this.data = config.data; charts.push(this); } update() {} },
        Swal: {
            close() {
                toast.closes++;
                const previous = toast.current;
                toast.visible = false; toast.current = null;
                previous?.didClose?.();
            },
            fire(options) {
                const previous = toast.current;
                toast.current = options; toast.visible = true; toast.shown.push(options);
                previous?.didClose?.();
            },
        },
    });
    runInContext(classic, context);
    return { context, nodes, charts, toast };
}

async function wiredPage() {
    const result = page();
    const { context } = result;
    let authCallback, socket;
    const events = new Map();
    Object.assign(context, {
        location: { hash: '', hostname: '127.0.0.1' },
        localStorage: { setItem() {}, removeItem() {} }, createMeasurementRuntime,
        supabase: { auth: {
            getSession: async () => ({ data: { session: null } }),
            onAuthStateChange: callback => { authCallback = callback; },
        } },
        authenticatedFetch: async url => new Response(JSON.stringify({
            status: 'success', measurement: { user_id: USER, session_id: SID,
                status: url.endsWith('/start') ? 'ACTIVE' : 'STOPPED' },
        })),
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
    authCallback('SIGNED_IN', { user: { id: USER }, access_token: 'test-token' });
    const fire = (name, event) => { for (const handler of [...(events.get(name) || [])]) handler(event); };
    socket.connected = true; fire('connect');
    fire('measurement_phase', phase('MEASURING'));
    return { ...result, fire, events };
}

const breakEvent = (t = 11, sid = SID) => ({ v: 1, t, session_id: sid,
    level: 'BREAK', reason: 'LOW_SCORE', transition: true, recommended_break_sec: 300 });
const recoveredEvent = (t = 12, sid = SID) => ({ v: 1, t, session_id: sid,
    level: 'NORMAL', transition: true });

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

test('ABSENT masks score/balance, skips chart, and reentry restores real Backend values', async () => {
    const { fire, nodes, charts, context } = await wiredPage();
    fire('state', { v: 1, state: 'NORMAL', score: 75, metrics: { balance: 'LEFT' } });
    for (const score of [75, 100]) {
        const event = { v: 1, state: 'ABSENT', score, metrics: { balance: 'CENTER' } };
        fire('state', event);
        assert.equal(event.score, score); // Presentation does not alter Backend data.
        assert.equal(nodes.get('currentScore').innerText, '—');
        assert.equal(nodes.get('val-balance').innerText, '—');
        assert.equal(nodes.get('topStatusLabel').innerText, '자리 비움');
        assert.match(nodes.get('posture-sub').innerText, /실시간 표시/);
        assert.doesNotMatch(nodes.get('posture-sub').innerText, /계산.*정지/);
        assert.equal(charts[0].data.datasets[0].data.length, 1);
    }
    runInContext('changeLanguage("en")', context);
    assert.equal(nodes.get('currentScore').innerText, '—');
    assert.equal(nodes.get('val-balance').innerText, '—');
    assert.doesNotMatch(nodes.get('posture-sub').innerText, /paused/i);
    runInContext('changeLanguage("ko")', context);
    fire('state', { v: 1, state: 'NORMAL', score: 82, metrics: { balance: 'RIGHT' } });
    assert.equal(nodes.get('currentScore').innerText, 82);
    assert.equal(nodes.get('val-balance').innerText, '오른쪽 쏠림');
    assert.equal(charts[0].data.datasets[0].data.length, 2);
});

test('CAUTION/DANGER copy never invents static cause or reclassifies a high Backend score', async () => {
    const { fire, nodes, context } = await wiredPage();
    for (const state of ['CAUTION', 'DANGER']) {
        fire('state', { v: 1, state, score: 100, reasons: ['low_blink'], metrics: { static_hold_sec: 0 } });
        assert.equal(nodes.get('currentScore').innerText, 100);
        assert.equal(nodes.get('topStatusLabel').innerText, state === 'DANGER' ? '위험' : '주의');
        assert.doesNotMatch(nodes.get('topDetailFeedback').innerText, /정적|장시간|점수.*떨어/);
        runInContext('changeLanguage("en")', context);
        assert.doesNotMatch(nodes.get('topDetailFeedback').innerText, /static|too long|dropping/i);
        runInContext('changeLanguage("ko")', context);
    }
});

test('current-session recovery dismisses BREAK, but stale/old-session events cannot touch it', async () => {
    const { fire, toast } = await wiredPage();
    fire('feedback', breakEvent(20));
    assert.equal(toast.visible, true);
    assert.equal(toast.current.timer, undefined);
    const closes = toast.closes;
    const old = '22222222-2222-4222-8222-222222222222';
    fire('feedback', recoveredEvent(21, old));
    fire('feedback', recoveredEvent(19));
    fire('feedback', recoveredEvent(20));
    fire('feedback', { ...recoveredEvent(21), transition: false });
    fire('feedback', { ...breakEvent(19), level: 'WARNING', reason: 'FUSION_DANGER' });
    fire('measurement_phase', { ...phase('OFF'), session_id: old, t: 30 });
    assert.equal(toast.closes, closes);
    assert.equal(toast.shown.length, 1);
    assert.equal(toast.visible, true);
    fire('feedback', recoveredEvent(21));
    assert.equal(toast.visible, false);
    assert.equal(toast.closes, closes + 1);
});

test('valid non-BREAK warning replaces BREAK with a finite toast; late dismissal cannot close it', async () => {
    const { fire, toast } = await wiredPage();
    fire('feedback', breakEvent(20));
    fire('feedback', { v: 1, t: 21, session_id: SID, level: 'NOTICE',
        transition: true, reason: 'FUSION_CAUTION' });
    assert.equal(toast.current.timer, 5000);
    const closes = toast.closes;
    fire('feedback', recoveredEvent(19));
    assert.equal(toast.closes, closes);
    assert.equal(toast.shown.length, 2);
});

test('Stop and valid OFF close BREAK; a new calibration never inherits it', async () => {
    const next = '22222222-2222-4222-8222-222222222222';
    for (const action of ['stop', 'off']) {
        const { fire, toast, context, nodes } = await wiredPage();
        fire('feedback', breakEvent());
        if (action === 'stop') await context.stopMeasurement();
        else fire('measurement_phase', { ...phase('OFF'), t: 12 });
        assert.equal(toast.visible, false);
        fire('measurement_phase', { ...phase('CALIBRATING', 'COLLECTING'), session_id: next, t: 13 });
        fire('feedback', breakEvent(14));
        assert.equal(toast.visible, false);
        assert.equal(nodes.get('currentScore').innerText, '—');
    }
});

test('current NORMAL recovery does not close unrelated dialogs after BREAK was dismissed', async () => {
    const { fire, context, toast } = await wiredPage();
    fire('feedback', breakEvent());
    context.Swal.close(); // User closes the recommendation.
    context.Swal.fire({ title: 'Unrelated dialog' });
    const closes = toast.closes;
    fire('feedback', recoveredEvent());
    assert.equal(toast.closes, closes);
    assert.equal(toast.current.title, 'Unrelated dialog');
});

test('demo hides legacy daily charts/test popup and never queries fatigue_logs', async () => {
    assert.match(html, /id="daily-report-unavailable"/);
    for (const cssClass of ['feedback-card', 'card half-chart', 'card half-chart-wide']) {
        assert.ok(html.includes(`class="${cssClass}" hidden style="display:none"`));
    }
    assert.match(html, /<button onclick="testPopup\(\)" hidden style="display:none"/);
    const { context } = await wiredPage();
    let queried = false;
    context.supabase.from = () => { queried = true; throw Error('legacy query must not run'); };
    await context.window.fetchDailyReport();
    assert.equal(queried, false);
});
