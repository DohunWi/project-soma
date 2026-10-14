import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { aggregateHistory, createHistoryBrowser, recordedHistoryRequestUrl, historyFailure } from '../view/measurement_history.mjs';
import { createAuthenticatedFetch } from '../view/measurement_runtime.mjs';

const USER = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const SID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const OLD = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const recorded = (session_id = SID) => ({ session_id, first_measured_at: '2026-10-01T00:00:00Z',
    last_measured_at: '2026-10-01T01:00:00Z', snapshot_count: 2 });
const list = sessions => ({ v: 1, status: 'success', user_id: USER, sessions });
const history = (session_id = SID) => ({ v: 1, status: 'success', stream: { user_id: USER, session_id },
    period: { start: '2026-10-01T00:30:00Z', end: '2026-10-01T01:00:00Z' },
    states: [{ measured_at: '2026-10-01T00:59:30Z', state: 'NORMAL', score: 90 },
        { measured_at: '2026-10-01T01:00:00Z', state: 'CAUTION', score: 85 }] });
const response = payload => new Response(JSON.stringify(payload));
const settle = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };
function fixture(fetch = async url => response(url.endsWith('/sessions') ? list([recorded(), recorded(OLD)]) : history(new URL(url).pathname.split('/').at(-1)))) {
    const calls = [], results = [], statuses = [];
    const browser = createHistoryBrowser({ backendUrl: 'http://backend.test',
        authenticatedFetch: (...args) => { calls.push(args); return fetch(...args); },
        onChange: model => statuses.push(model.status), onData: data => results.push(data) });
    return { browser, calls, results, statuses };
}

test('archived 30/60 minutes anchor to last snapshot, not current wall clock', () => {
    for (const minutes of [30, 60]) {
        const url = new URL(recordedHistoryRequestUrl('http://backend.test', recorded(), minutes));
        assert.equal(url.pathname, `/api/state/history/sessions/${SID}`);
        assert.equal(url.searchParams.get('end'), '2026-10-01T01:00:00.000Z');
        assert.equal(Date.parse(url.searchParams.get('end')) - Date.parse(url.searchParams.get('start')), minutes * 60_000);
        assert.equal(url.searchParams.has('user_id'), false);
    }
    assert.throws(() => recordedHistoryRequestUrl('http://backend.test', { session_id: '../other' }, 30));
});

test('OFF restores newest recorded session and explicit selection preserves stored score/ratios', async () => {
    const f = fixture(); f.browser.setContext(USER); await settle();
    assert.equal(f.browser.model.selectedSessionId, SID);
    assert.equal(f.browser.model.status, 'ready');
    assert.equal(f.results.at(-1).sampleCount, 2);
    assert.equal(f.results.at(-1).durationMs.NORMAL, 30_000);
    assert.equal(f.results.at(-1).points.at(-1).score, 85);
    await f.browser.select(OLD);
    assert.equal(f.browser.model.selectedSessionId, OLD);
    assert.match(f.calls.at(-1)[0], new RegExp(OLD));
    assert.equal(f.calls.at(-1)[2], USER);
    await f.browser.setMinutes(60);
    assert.equal(new URL(f.calls.at(-1)[0]).searchParams.get('start'), '2026-10-01T00:00:00.000Z');
});

test('ACTIVE uses unchanged ACTIVE API; Stop automatically reloads recorded sessions', async () => {
    const f = fixture(async url => response(url.endsWith('/sessions') ? list([recorded()]) : history()));
    f.browser.setContext(USER, SID); await settle();
    assert.equal(new URL(f.calls[0][0]).pathname, '/api/state/history');
    assert.equal(f.calls.length, 1);
    f.browser.setContext(USER, null); await settle();
    assert.equal(new URL(f.calls[1][0]).pathname, '/api/state/history/sessions');
    assert.equal(f.browser.model.selectedSessionId, SID);
});

test('no records, no ACTIVE, auth, DB, and unknown session have distinct safe statuses', async () => {
    const f = fixture(async () => response(list([])));
    f.browser.setContext(USER); await settle();
    assert.equal(f.browser.model.status, 'empty_sessions');
    assert.equal(f.results.length, 0);
    for (const [error, expected] of [
        [{ code: 'no_active_session' }, 'no_active_session'], [{ kind: 'auth' }, 'auth'],
        [{ code: 'history_unavailable' }, 'unavailable'], [{ code: 'session_not_found' }, 'missing_session'],
        [{ kind: 'network' }, 'network'],
    ]) {
        assert.equal(historyFailure(error).status, expected);
        assert.doesNotMatch(historyFailure({ ...error, message: 'secret' }).message, /secret/);
    }
    const fail = fixture(async () => { throw { code: 'history_unavailable' }; });
    fail.browser.setContext(USER); await settle();
    assert.equal(fail.browser.model.status, 'unavailable');
});

test('new user/logout/example/session switches discard late responses and clear selection', async () => {
    let resolve;
    const f = fixture(() => new Promise(done => { resolve = done; }));
    f.browser.setContext(USER); await settle();
    f.browser.setContext(null);
    resolve(response(list([recorded()]))); await settle();
    assert.equal(f.browser.model.status, 'signed_out');
    assert.equal(f.browser.model.selectedSessionId, null);
    assert.equal(f.results.length, 0);
    const g = fixture(() => new Promise(done => { resolve = done; }));
    g.browser.setContext(USER); await settle();
    g.browser.showExample(); resolve(response(list([recorded()]))); await settle();
    assert.equal(g.browser.model.mode, 'example');
    assert.equal(g.results.length, 1);
});

test('mismatched user/session response cannot populate archive UI', async () => {
    for (const stream of [{ user_id: OTHER, session_id: SID }, { user_id: USER, session_id: OLD }]) {
        const f = fixture(async url => response(url.endsWith('/sessions') ? list([recorded()]) : { ...history(), stream }));
        f.browser.setContext(USER); await settle();
        assert.equal(f.browser.model.status, 'error');
        assert.equal(f.results.length, 0);
    }
});

test('REST helper preserves safe history error code/status without changing Bearer auth', async () => {
    let token;
    const helper = createAuthenticatedFetch({ getSession: async () => ({ data: { session: {
        user: { id: USER }, access_token: 'test-token',
    } } }) }, async (_url, options) => {
        token = options.headers.get('Authorization');
        return new Response(JSON.stringify({ error: { code: 'history_unavailable', message: 'secret driver' } }), { status: 503 });
    });
    await assert.rejects(helper('http://backend.test/api/state/history/sessions'), error => {
        assert.equal(error.code, 'history_unavailable'); assert.equal(error.status, 503);
        assert.doesNotMatch(error.message, /secret/); return true;
    });
    assert.equal(token, 'Bearer test-token');
});

test('old baseline cannot invent NORMAL time across a missing-data gap', () => {
    const result = aggregateHistory({ ...history(),
        baseline: { measured_at: '2026-10-01T00:00:00Z', score: 100, state: 'NORMAL' },
        states: [{ measured_at: '2026-10-01T00:30:30Z', score: 85, state: 'CAUTION' }],
    });
    assert.equal(result.durationMs.NORMAL, 0);
});

test('source and deployment copies stay identical; session selector is wired into real page', () => {
    for (const file of ['index.html', 'measurement_history.mjs', 'measurement_runtime.mjs']) {
        assert.equal(readFileSync(new URL(`../view/${file}`, import.meta.url), 'utf8'),
            readFileSync(new URL(`../${file}`, import.meta.url), 'utf8'));
    }
    const html = readFileSync(new URL('../view/index.html', import.meta.url), 'utf8');
    assert.match(html, /id="history-session"/);
    assert.match(html, /historyBrowser\.setContext\(historyUserId, model\.sessionId\)/);
    assert.match(html, /historyBrowser\.select\(event\.target\.value\)/);
});

test('cross-layer E2E: Flask Stop/restart output passes real Front history browser', async () => {
    const root = fileURLToPath(new URL('../../', import.meta.url));
    const venvPython = process.platform === 'win32' ? `${root}.venv/Scripts/python.exe` : `${root}.venv/bin/python`;
    const python = process.env.SOMA_TEST_PYTHON || (existsSync(venvPython) ? venvPython : 'python');
    const run = spawnSync(python, ['-B', '-c',
        'import json; from server.tests.test_recorded_history import stopped_session_fixture; a,b=stopped_session_fixture(); print(json.dumps({"sessions":a,"history":b}))'],
    { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
    assert.equal(run.status, 0, run.stderr);
    const payloads = JSON.parse(run.stdout.trim());
    const f = fixture(async url => response(url.endsWith('/sessions') ? payloads.sessions : payloads.history));
    f.browser.setContext(USER); await settle();
    assert.equal(f.browser.model.status, 'ready');
    assert.equal(f.results.at(-1).sampleCount, payloads.history.states.length);
    assert.equal(f.browser.model.selectedSessionId, payloads.sessions.sessions[0].session_id);
});
