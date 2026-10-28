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
function page({ mediaDevices } = {}) {
    const eventHandlers = new Map();
    const makeNode = () => ({
        style: {}, innerText: '', textContent: '', hidden: false, disabled: false, value: '',
        children: [], attributes: {}, classList: { add() {}, remove() {}, toggle() {} },
        getContext: () => ({}),
        setAttribute(name, value) { this.attributes[name] = value; },
        focus() { this.focused = true; },
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
    const toast = { visible: false, current: null, shown: alerts, closes: 0 };
    const context = createContext({
        document: { getElementById: id => {
            assert.ok(nodes.has(id), `UI references missing element ${id}`); return nodes.get(id);
        }, querySelectorAll: () => [], querySelector: () => makeNode(), createElement: () => makeNode() },
        window: { addEventListener: (name, handler) => windowEvents.set(name, handler) },
        navigator: { ...(mediaDevices ? { mediaDevices } : {}) },
        Chart: class { constructor(_ctx, config) { this.data = config.data; charts.push(this); } update() {} },
        Swal: {
            close() {
                alertCloses++; toast.closes++;
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
    return { context, nodes, charts, windowEvents, eventHandlers, alerts, toast,
        alertCloses: () => alertCloses };
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

test('DEMO/NORMAL manual tabs switch presentation only and keep both panels available', () => {
    const { context, nodes } = page();
    assert.equal(nodes.get('manual-panel-demo').hidden, false);
    assert.equal(nodes.get('manual-panel-real').hidden, false); // The tiny DOM does not parse boolean attrs.

    context.switchManual('real');
    assert.equal(nodes.get('manual-panel-demo').hidden, true);
    assert.equal(nodes.get('manual-panel-real').hidden, false);
    assert.equal(nodes.get('manual-tab-demo').attributes['aria-selected'], 'false');
    assert.equal(nodes.get('manual-tab-real').attributes['aria-selected'], 'true');

    context.switchManual('demo');
    assert.equal(nodes.get('manual-panel-demo').hidden, false);
    assert.equal(nodes.get('manual-panel-real').hidden, true);
    assert.equal(nodes.get('manual-tab-demo').attributes['aria-selected'], 'true');
    assert.equal(nodes.get('manual-tab-real').attributes['aria-selected'], 'false');
});

test('technical term dialog opens accessibly and closes by button, Escape, or backdrop without touching measurement', () => {
    assert.match(html, /<button[^>]+class="technical-term-button"[\s\S]*?>cumulative-v2<\/button>/);
    assert.match(html, /<button[^>]+class="technical-term-button"[\s\S]*?>score-v1<\/button>/);
    const { context, nodes } = page();
    const model = { phase: 'MEASURING', sessionId: SID, pendingAction: null };
    context.window.measurementRuntime = { model };
    const overlay = nodes.get('technical-term-overlay');
    overlay.hidden = true;
    const staticTrigger = nodes.get('manual-tab-real');

    assert.equal(context.openTechnicalTerm('cumulative-v2', staticTrigger), true);
    assert.equal(overlay.hidden, false);
    assert.equal(nodes.get('technical-term-title').innerText, 'cumulative-v2');
    assert.equal(nodes.get('technical-term-cumulative-v2').hidden, false);
    assert.equal(nodes.get('technical-term-score-v1').hidden, true);
    assert.equal(nodes.get('technical-term-close').focused, true);
    assert.equal(model.phase, 'MEASURING');

    let prevented = false;
    context.handleTechnicalTermKeydown({ key: 'Escape', preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(overlay.hidden, true);
    assert.equal(staticTrigger.focused, true);
    assert.equal(model.phase, 'MEASURING');

    const blinkTrigger = nodes.get('feedback-test-normal');
    context.openTechnicalTerm('score-v1', blinkTrigger);
    assert.equal(nodes.get('technical-term-score-v1').hidden, false);
    context.handleTechnicalTermBackdrop({ target: overlay, currentTarget: overlay });
    assert.equal(overlay.hidden, true);
    assert.equal(blinkTrigger.focused, true);

    context.openTechnicalTerm('score-v1', blinkTrigger);
    assert.equal(context.closeTechnicalTerm(), true);
    assert.equal(overlay.hidden, true);
    assert.equal(model.phase, 'MEASURING');
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

test('hardware-isolated feedback demo supports every level, OFF, and blocks during measurement', async () => {
    const { context, nodes, alerts, alertCloses } = page();
    const commands = [];
    const model = { phase: 'OFF', sessionId: null, pendingAction: null };
    context.window.measurementRuntime = { model, hasSession: () => true };
    context.window.requestFeedbackDemo = async level => { commands.push(level); return true; };
    for (const level of ['NORMAL', 'CAUTION', 'DANGER', 'BREAK']) {
        assert.equal(await context.showTestFeedback(level), true);
    }
    assert.equal(alerts.length, 4);
    assert.ok(alerts.every(alert => alert.title.startsWith('[테스트]')));
    assert.ok(alerts.every(alert => alert.text.includes('실제 측정 결과가 아닙니다')));
    assert.equal(alerts[0].timer, 5000);
    assert.equal(alerts[1].timer, 5000);
    assert.equal(alerts[2].timer, 5000);
    assert.equal(alerts[3].timer, undefined);
    assert.equal(alerts[3].showCloseButton, true);
    await context.closeTestFeedback();
    assert.deepEqual(commands,
        ['NORMAL', 'OFF', 'CAUTION', 'OFF', 'DANGER', 'OFF', 'BREAK', 'OFF']);
    assert.ok(alertCloses() >= 1);

    model.phase = 'MEASURING'; model.sessionId = SID;
    assert.equal(await context.showTestFeedback('CAUTION'), false);
    assert.equal(alerts.length, 4);
    assert.match(nodes.get('feedback-test-status').innerText, /측정.*사용할 수 없습니다/);
});

test('late didClose from replaced popup cannot turn off the current Nano demo', async () => {
    const { context, alerts } = page();
    const commands = [];
    context.window.measurementRuntime = {
        model: { phase: 'OFF', sessionId: null, pendingAction: null },
        hasSession: () => true,
    };
    context.window.requestFeedbackDemo = async level => { commands.push(level); return true; };

    assert.equal(await context.showTestFeedback('NORMAL'), true);
    const oldDidClose = alerts[0].didClose;
    assert.equal(await context.showTestFeedback('CAUTION'), true);
    assert.deepEqual(commands, ['NORMAL', 'OFF', 'CAUTION']);

    oldDidClose();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(commands, ['NORMAL', 'OFF', 'CAUTION']);

    alerts[1].didClose();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(commands, ['NORMAL', 'OFF', 'CAUTION', 'OFF']);
});

test('rapid transitions serialize and retain only the latest requested popup state', async () => {
    const { context, alerts } = page();
    const commands = [];
    let releaseOff;
    context.window.measurementRuntime = {
        model: { phase: 'OFF', sessionId: null, pendingAction: null },
        hasSession: () => true,
    };
    context.window.requestFeedbackDemo = async level => {
        commands.push(level);
        if (level === 'OFF') await new Promise(resolve => { releaseOff = resolve; });
        return true;
    };

    assert.equal(await context.showTestFeedback('NORMAL'), true);
    const normalDidClose = alerts[0].didClose;
    const caution = context.showTestFeedback('CAUTION');
    await new Promise(resolve => setImmediate(resolve));
    const danger = context.showTestFeedback('DANGER');
    const breaking = context.showTestFeedback('BREAK');
    assert.deepEqual(commands, ['NORMAL', 'OFF']);
    releaseOff();

    assert.equal(await caution, false);
    assert.equal(await danger, false);
    assert.equal(await breaking, true);
    assert.deepEqual(commands, ['NORMAL', 'OFF', 'BREAK']);
    assert.equal(alerts.at(-1).title, '[테스트] 휴식 권고 알림');

    normalDidClose();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(commands, ['NORMAL', 'OFF', 'BREAK']);
});

test('measurement start owns demo cleanup and late popup close cannot send another OFF', async () => {
    const { context, alerts } = page();
    const actions = [];
    context.window.measurementRuntime = {
        model: { phase: 'OFF', sessionId: null, pendingAction: null },
        hasSession: () => true,
        start: async () => { actions.push('START'); return true; },
    };
    context.window.requestFeedbackDemo = async level => { actions.push(level); return true; };

    assert.equal(await context.showTestFeedback('DANGER'), true);
    const staleDidClose = alerts[0].didClose;
    assert.equal(await context.startMeasurement(), true);
    assert.deepEqual(actions, ['DANGER', 'OFF', 'START']);

    staleDidClose();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(actions, ['DANGER', 'OFF', 'START']);
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

test('current history/report and feedback demo replace legacy fatigue_logs UI', async () => {
    assert.match(html, /id="history-session-controls"/);
    assert.match(html, /id="session-report-card"/);
    assert.match(html, /id="feedback-test-panel"/);
    assert.doesNotMatch(html, /fatigue_logs/);
    const { context } = await wiredPage();
    assert.equal(context.window.fetchDailyReport, undefined);
});

test('runtime recovery cannot close a manual demo toast it does not own', async () => {
    const { context, toast } = page();
    context.window.measurementRuntime = {
        model: { phase: 'OFF', sessionId: null, pendingAction: null },
        hasSession: () => true,
    };
    context.window.requestFeedbackDemo = async () => true;
    assert.equal(await context.showTestFeedback('BREAK'), true);
    const current = toast.current;
    const closes = toast.closes;
    context.presentFeedbackToast({ clear: true }, { owner: 'runtime' });
    assert.equal(toast.closes, closes);
    assert.equal(toast.current, current);
    await context.closeTestFeedback();
});

test('blocked manual demo cannot replace an owned runtime BREAK toast', async () => {
    const { context, fire, toast } = await wiredPage();
    fire('feedback', breakEvent(20));
    const current = toast.current;
    assert.equal(await context.showTestFeedback('CAUTION'), false);
    assert.equal(toast.current, current);
    fire('feedback', recoveredEvent(21));
    assert.equal(toast.visible, false);
});
