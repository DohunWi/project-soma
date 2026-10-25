import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import * as history from '../view/measurement_history.mjs';
import * as reportUi from '../view/session_report.mjs';
import { createAuthenticatedFetch, createMeasurementRuntime } from '../view/measurement_runtime.mjs';

const USER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const OTHER_USER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const SID = '11111111-1111-4111-8111-111111111111';
const OLDER = '22222222-2222-4222-8222-222222222222';
const sessions = [
    { session_id: SID, first_measured_at: '2026-10-14T02:00:00Z',
        last_measured_at: '2026-10-14T03:00:00Z', snapshot_count: 31 },
    { session_id: OLDER, first_measured_at: '2026-10-13T01:00:00Z',
        last_measured_at: '2026-10-13T02:00:00Z', snapshot_count: 12 },
];
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const listing = (items = sessions, userId = USER) => ({ v: 1, status: 'success', user_id: userId, sessions: items });
const detail = (url, score = 87, userId = USER) => ({
    v: 1, status: 'success',
    stream: { user_id: userId, session_id: url.pathname.split('/').at(-1) },
    period: { start: url.searchParams.get('start'), end: url.searchParams.get('end') },
    states: [{ measured_at: url.searchParams.get('end'), state: 'CAUTION', score }],
});
const report = (sessionId = SID, average = 88, userId = USER) => ({
    v: 1, status: 'success', stream: { user_id: userId, session_id: sessionId },
    period: { start: '2026-10-14T02:00:00Z', end: '2026-10-14T03:00:00Z',
        basis: 'snapshot_bounds', lifecycle_exact: false },
    summary: { snapshot_span_sec: 3600, observed_coverage_sec: 3540,
        seated_observed_sec: 3480, absent_observed_sec: 60, unknown_gap_sec: 60,
        average_score: average, minimum_score: 71,
        state_seconds: { NORMAL: 1800, CAUTION: 1200, DANGER: 480, ABSENT: 60 },
        major_reasons: [{ reason: 'static_hold', seconds: 1200 }],
        break_feedback: { supported: false, occurred: null, message: '현재 저장 데이터로 확인 불가' } },
    quality: { snapshot_count: 2, valid_interval_count: 1, long_gap_count: 0,
        nonpositive_interval_count: 0, gap_limit_sec: 60, single_snapshot: false,
        score_model_versions: ['soma_load_v1'], legacy_unversioned: false,
        trigger_counts: { state_change: 1, periodic: 1 } },
    timeseries: [
        { measured_at: '2026-10-14T02:00:00Z', state: 'NORMAL', score: 100, gap_before: false },
        { measured_at: '2026-10-14T03:00:00Z', state: 'CAUTION', score: 71, gap_before: true },
    ],
});
const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
};
const html = readFileSync(new URL('../view/index.html', import.meta.url), 'utf8');
const classic = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const moduleScript = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];

// Exercise the actual page handlers, Auth adapter, charts and measurement Stop;
// only HTTP, Socket.IO, and the browser DOM are replaced with hardware-free doubles.
async function page() {
    const node = () => ({
        style: {}, innerText: '', textContent: '', hidden: false, disabled: false,
        value: '', children: [], handlers: new Map(),
        classList: { add() {}, remove() {}, toggle() {} },
        getContext: () => ({}), setAttribute() {}, play: async () => {},
        addEventListener(name, handler) { this.handlers.set(name, handler); },
        appendChild(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
    });
    const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, node()]));
    const selectors = new Map();
    const charts = [];
    const calls = [];
    let authSession = null, authCallback, socket;
    let refreshes = 0;
    let route = async url => {
        if (url.pathname.endsWith('/stop')) return json({ status: 'success', measurement: {
            user_id: USER, session_id: SID, status: 'STOPPED',
        } });
        if (url.pathname.endsWith('/sessions')) return json(listing());
        if (url.pathname.endsWith('/report')) return json(report(url.pathname.split('/').at(-2)));
        return json(detail(url, url.pathname.endsWith(OLDER) ? 76 : 87));
    };
    const auth = {
        getSession: async () => ({ data: { session: authSession } }),
        refreshSession: async () => {
            refreshes++;
            return { data: { session: authSession } };
        },
        onAuthStateChange(callback) { authCallback = callback; },
    };
    const context = createContext({
        document: { getElementById: id => {
            assert.ok(nodes.has(id), `missing UI element ${id}`);
            return nodes.get(id);
        }, querySelectorAll: () => [], querySelector: selector => {
            if (!selectors.has(selector)) selectors.set(selector, node());
            return selectors.get(selector);
        }, createElement: node },
        window: { addEventListener() {} }, navigator: {},
        Chart: class { constructor(_ctx, config) { this.data = config.data; charts.push(this); } update() {} },
        Swal: { close() {}, fire() {} },
        location: { hash: '', hostname: '127.0.0.1' },
        localStorage: { setItem() {}, removeItem() {} },
        supabase: { auth }, createMeasurementRuntime, ...history, ...reportUi,
        authenticatedFetch: createAuthenticatedFetch(auth, async (rawUrl, options) => {
            const url = new URL(rawUrl);
            calls.push({ url, method: options.method, authorization: options.headers.get('Authorization') });
            return route(url, options);
        }),
        io: (_url, options) => {
            socket = { auth: options.auth, connected: false, active: true, events: new Map(),
                on(name, handler) { this.events.set(name, [...(this.events.get(name) || []), handler]); },
                off(name, handler) { this.events.set(name, (this.events.get(name) || []).filter(fn => fn !== handler)); },
                fire(name, event) { for (const fn of this.events.get(name) || []) fn(event); },
                connect() { this.connected = true; this.fire('connect'); },
                disconnect() { this.connected = false; this.fire('disconnect'); },
            };
            return socket;
        },
    });
    runInContext(classic, context);
    runInContext(moduleScript.replace(/^\s*import .*;$/gm, ''), context);
    await Promise.resolve();
    await Promise.resolve();
    function signIn(userId = USER) {
        authSession = { user: { id: userId }, access_token: 'test-only-token' };
        authCallback('SIGNED_IN', authSession);
    }
    signIn();
    nodes.get('history-range').value = '30';
    return {
        context, nodes, charts, calls, signIn,
        setRoute(next) { route = next; },
        signOut() { authSession = null; authCallback('SIGNED_OUT', null); },
        get socket() { return socket; },
        get refreshes() { return refreshes; },
        click: id => nodes.get(id).handlers.get('click')(),
        change: id => nodes.get(id).handlers.get('change')(),
        status: () => nodes.get('history-status').innerText,
        scores: () => [...charts[2].data.datasets[0].data],
        reportScores: () => [...charts[3].data.datasets[0].data],
    };
}

test('saved session report renders summary fields and breaks the full-session chart gap', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(p.nodes.get('session-report-card').hidden, false);
    assert.equal(p.nodes.get('report-average-score').innerText, '88.0');
    assert.equal(p.nodes.get('report-minimum-score').innerText, 71);
    assert.match(p.nodes.get('report-seated-time').innerText, /58분/);
    assert.match(p.nodes.get('report-state-times').innerText, /ABSENT/);
    assert.match(p.nodes.get('report-reasons').innerText, /정적 유지/);
    assert.equal(p.nodes.get('report-break').innerText, '현재 저장 데이터로 확인 불가');
    assert.deepEqual(p.reportScores(), [100, null, 71]);
    assert.ok(p.calls.some(call => call.url.pathname === `/api/state/history/sessions/${SID}/report`));
});

test('ABSENT-only session renders no seated minimum score', async () => {
    const p = await page();
    const absentOnly = report();
    absentOnly.summary.average_score = null;
    absentOnly.summary.minimum_score = null;
    absentOnly.summary.seated_observed_sec = 0;
    absentOnly.summary.absent_observed_sec = 3600;
    absentOnly.summary.state_seconds = { NORMAL: 0, CAUTION: 0, DANGER: 0, ABSENT: 3600 };
    absentOnly.timeseries = absentOnly.timeseries.map(point => ({ ...point, state: 'ABSENT' }));
    p.setRoute(async url => {
        if (url.pathname.endsWith('/sessions')) return json(listing());
        if (url.pathname.endsWith('/report')) return json(absentOnly);
        return json(detail(url));
    });

    await p.click('history-sessions-button');
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(p.nodes.get('report-average-score').innerText, '산출 불가');
    assert.equal(p.nodes.get('report-minimum-score').innerText,
        '산출 불가 (착석 snapshot 없음)');
});

test('late report cannot overwrite a newer session selection', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    const pending = deferred();
    p.setRoute(async url => {
        if (url.pathname.endsWith('/sessions')) return json(listing());
        if (url.pathname === `/api/state/history/sessions/${SID}/report`) return pending.promise;
        if (url.pathname === `/api/state/history/sessions/${OLDER}/report`) return json(report(OLDER, 72));
        return json(detail(url, url.pathname.endsWith(OLDER) ? 76 : 87));
    });
    const oldLoading = p.change('history-session-select');
    p.nodes.get('history-session-select').value = OLDER;
    await p.change('history-session-select');
    await Promise.resolve();
    assert.equal(p.nodes.get('report-average-score').innerText, '72.0');
    pending.resolve(json(report(SID, 99)));
    await oldLoading;
    await Promise.resolve();
    assert.equal(p.nodes.get('report-average-score').innerText, '72.0');
});

test('report auth failure is isolated from saved History and retries through shared auth', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    p.setRoute(async url => url.pathname.endsWith('/report') ?
        json({ error: { code: 'invalid_token' } }, 401) : json(detail(url, 84)));
    await p.change('history-session-select');
    await Promise.resolve();

    assert.equal(p.refreshes, 1);
    assert.match(p.nodes.get('session-report-status').innerText, /인증이 만료/);
    assert.deepEqual(p.scores(), [84]);
});

test('user switch invalidates an in-flight report response', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    const pending = deferred();
    p.setRoute(async url => url.pathname.endsWith('/report') ? pending.promise : json(detail(url)));
    const loading = p.change('history-session-select');
    p.signIn(OTHER_USER);
    pending.resolve(json(report(SID, 99)));
    await loading;
    await Promise.resolve();

    assert.equal(p.nodes.get('session-report-card').hidden, true);
    assert.notEqual(p.nodes.get('report-average-score').innerText, '99.0');
});

test('recorded URL uses selected UUID and last measured time, with no client user identity', () => {
    const url = new URL(history.recordedHistoryRequestUrl('http://backend.test', sessions[1], 60));
    assert.equal(url.pathname, `/api/state/history/sessions/${OLDER}`);
    assert.equal(url.searchParams.get('end'), sessions[1].last_measured_at.replace('Z', '.000Z'));
    assert.equal(Date.parse(url.searchParams.get('end')) - Date.parse(url.searchParams.get('start')), 3_600_000);
    assert.deepEqual([...url.searchParams.keys()].sort(), ['end', 'start']);
    assert.throws(() => history.recordedHistoryRequestUrl('http://backend.test', { ...sessions[0], session_id: '../other' }, 30));
    assert.throws(() => history.recordedHistoryRequestUrl('http://backend.test', sessions[0], 61));
    assert.throws(() => history.recordedSessionsFromPayload(listing(sessions, OTHER_USER), USER));
});

test('page lists saved sessions, selects older session, and keeps current history available', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    const select = p.nodes.get('history-session-select');
    assert.equal(select.disabled, false);
    assert.deepEqual(select.children.map(option => option.value), [SID, OLDER]);
    assert.ok(select.children[0].textContent.includes('31개'));
    assert.deepEqual(p.scores(), [87]);
    assert.match(p.nodes.get('history-transitions').children[0].textContent, /CAUTION/);
    select.value = OLDER;
    await p.change('history-session-select');
    assert.deepEqual(p.scores(), [76]);
    assert.equal(p.calls.at(-1).url.pathname, `/api/state/history/sessions/${OLDER}`);
    assert.ok(p.calls.every(call => call.authorization === 'Bearer test-only-token' && call.method === 'GET'));

    p.setRoute(async url => json({ ...detail(url, 94), stream: { user_id: USER, session_id: SID } }));
    await p.click('history-actual-button');
    assert.equal(p.calls.at(-1).url.pathname, '/api/state/history');
    assert.deepEqual(p.scores(), [94]);
    assert.equal(p.nodes.get('history-session-controls').hidden, true);
});

test('measurement Stop then ACTIVE 409 does not block archived history or start a measurement', async () => {
    const p = await page();
    p.socket.fire('measurement_phase', { v: 1, t: 10, phase: 'MEASURING', active: true,
        session_id: SID, calibration: { status: 'ACCEPTED' } });
    assert.equal(await p.context.stopMeasurement(), true);
    assert.equal(p.context.window.measurementRuntime.model.phase, 'OFF');
    p.setRoute(async url => url.pathname === '/api/state/history' ?
        json({ error: { code: 'no_active_session' } }, 409) :
        url.pathname.endsWith('/sessions') ? json(listing()) : json(detail(url)));
    await p.click('history-actual-button');
    assert.match(p.status(), /저장된 세션/);
    await p.click('history-sessions-button');
    assert.deepEqual(p.scores(), [87]);
    assert.equal(p.context.window.measurementRuntime.model.phase, 'OFF');
    assert.deepEqual(p.calls.filter(call => call.method === 'POST').map(call => call.url.pathname), ['/api/measurement/stop']);
});

test('saved range change and refresh preserve selection and use latest list timestamp', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    p.nodes.get('history-session-select').value = OLDER;
    await p.change('history-session-select');
    p.nodes.get('history-range').value = '60';
    await p.change('history-range');
    const lastUrl = p.calls.at(-1).url;
    assert.equal(Date.parse(lastUrl.searchParams.get('end')) - Date.parse(lastUrl.searchParams.get('start')), 3_600_000);
    const updated = { ...sessions[1], last_measured_at: '2026-10-13T02:05:00Z' };
    p.setRoute(async url => url.pathname.endsWith('/sessions') ? json(listing([sessions[0], updated])) : json(detail(url)));
    await p.click('history-refresh-button');
    assert.equal(p.nodes.get('history-session-select').value, OLDER);
    assert.equal(p.calls.at(-1).url.searchParams.get('end'), '2026-10-13T02:05:00.000Z');
});

test('empty saved list and empty detail clear previous charts with actionable messages', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    p.setRoute(async () => json(listing([])));
    await p.click('history-refresh-button');
    assert.deepEqual(p.scores(), []);
    assert.equal(p.nodes.get('history-session-select').disabled, true);
    assert.match(p.status(), /저장된 측정 기록이 없습니다/);
    p.setRoute(async url => url.pathname.endsWith('/sessions') ? json(listing()) : json({ ...detail(url), states: [] }));
    await p.click('history-refresh-button');
    assert.deepEqual(p.scores(), []);
    assert.match(p.status(), /이 구간에는 저장된 snapshot이 없습니다/);
});

for (const status of [404, 503]) test(`saved detail HTTP ${status} clears old data and allows list retry`, async () => {
    const p = await page();
    await p.click('history-sessions-button');
    p.setRoute(async url => url.pathname.endsWith('/sessions') ? json(listing()) :
        json({ error: { code: status === 404 ? 'session_not_found' : 'history_unavailable', message: 'private-db-diagnostic' } }, status));
    await p.change('history-session-select');
    assert.deepEqual(p.scores(), []);
    assert.match(p.status(), /Backend\/DB/);
    assert.doesNotMatch(p.status(), /private-db-diagnostic/);
    p.setRoute(async url => url.pathname.endsWith('/sessions') ? json(listing()) : json(detail(url, 91)));
    await p.click('history-refresh-button');
    assert.deepEqual(p.scores(), [91]);
});

test('list DB/network errors clear old selection; unauthenticated users send no request', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    p.setRoute(async () => json({ error: { code: 'history_unavailable' } }, 503));
    await p.click('history-refresh-button');
    assert.deepEqual(p.scores(), []);
    assert.equal(p.nodes.get('history-session-select').disabled, true);
    assert.match(p.status(), /Backend\/DB/);
    p.setRoute(async () => { throw new TypeError('network unavailable'); });
    await p.click('history-refresh-button');
    assert.match(p.status(), /연결할 수 없습니다/);
    p.signOut();
    const count = p.calls.length;
    await p.click('history-sessions-button');
    assert.equal(p.calls.length, count);
    assert.match(p.status(), /로그인/);
});

test('expired auth retries once through shared auth helper and leaves no old chart on failure', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    p.setRoute(async () => json({ error: { code: 'invalid_token' } }, 401));
    await p.click('history-refresh-button');
    assert.equal(p.refreshes, 1);
    assert.deepEqual(p.scores(), []);
    assert.match(p.status(), /인증이 만료/);
});

test('user switch discards pending list and detail; mismatched response ownership is rejected', async () => {
    const p = await page();
    const pendingList = deferred();
    p.setRoute(async () => pendingList.promise);
    const loading = p.click('history-sessions-button');
    p.signIn(OTHER_USER);
    pendingList.resolve(json(listing()));
    await loading;
    assert.deepEqual(p.scores(), []);
    assert.equal(p.nodes.get('history-session-select').disabled, true);

    p.signIn(USER);
    p.setRoute(async url => url.pathname.endsWith('/sessions') ? json(listing()) : json(detail(url)));
    await p.click('history-sessions-button');
    const pendingDetail = deferred();
    const selectedUrl = history.recordedHistoryRequestUrl('http://backend.test', sessions[0], 30);
    p.setRoute(async () => pendingDetail.promise);
    const loadingDetail = p.change('history-session-select');
    p.signOut();
    pendingDetail.resolve(json(detail(new URL(selectedUrl))));
    await loadingDetail;
    assert.deepEqual(p.scores(), []);
    assert.equal(p.nodes.get('history-session-select').disabled, true);

    p.signIn(OTHER_USER);
    p.setRoute(async () => json(listing()));
    await p.click('history-sessions-button');
    assert.deepEqual(p.scores(), []);
    assert.match(p.status(), /응답/);
});

test('late ACTIVE failure cannot clear successfully loaded saved history', async () => {
    const p = await page();
    const pending = deferred();
    p.setRoute(async url => url.pathname === '/api/state/history' ? pending.promise :
        url.pathname.endsWith('/sessions') ? json(listing()) : json(detail(url, 89)));
    const activeLoading = p.click('history-actual-button');
    await p.click('history-sessions-button');
    pending.resolve(json({ error: { code: 'no_active_session' } }, 409));
    await activeLoading;
    assert.deepEqual(p.scores(), [89]);
    assert.match(p.nodes.get('history-mode-badge').innerText, /저장된 세션/);
});

test('late detail cannot overwrite a newer selection or example history', async () => {
    const p = await page();
    await p.click('history-sessions-button');
    const pending = deferred();
    p.setRoute(async url => url.pathname.endsWith(SID) ? pending.promise : json(detail(url, 73)));
    const loading = p.change('history-session-select');
    p.nodes.get('history-session-select').value = OLDER;
    await p.change('history-session-select');
    assert.deepEqual(p.scores(), [73]);
    pending.resolve(json(detail(new URL(history.recordedHistoryRequestUrl('http://backend.test', sessions[0], 30)), 99)));
    await loading;
    assert.deepEqual(p.scores(), [73]);

    const second = deferred();
    p.setRoute(async () => second.promise);
    const another = p.change('history-session-select');
    await p.click('history-example-button');
    const exampleScores = p.scores();
    second.resolve(json(detail(new URL(history.recordedHistoryRequestUrl('http://backend.test', sessions[1], 30)), 20)));
    await another;
    assert.deepEqual(p.scores(), exampleScores);
    assert.match(p.nodes.get('history-mode-badge').innerText, /예시/);
});

test('archived detail rejects a response for a different session or user', async () => {
    const p = await page();
    for (const stream of [{ user_id: OTHER_USER, session_id: SID }, { user_id: USER, session_id: OLDER }]) {
        p.setRoute(async url => url.pathname.endsWith('/sessions') ? json(listing()) : json({ ...detail(url), stream }));
        await p.click('history-sessions-button');
        assert.deepEqual(p.scores(), []);
        assert.match(p.status(), /응답/);
    }
});
