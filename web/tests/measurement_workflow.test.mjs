import test from 'node:test';
import assert from 'node:assert/strict';
import { initialLifecycle, reduceLifecycle, canRenderState, feedbackPresentation } from '../view/measurement_phase.mjs';
import { createAuthenticatedFetch, createMeasurementRuntime, WorkflowError } from '../view/measurement_runtime.mjs';

const USER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const OTHER_USER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const SID = '11111111-1111-4111-8111-111111111111';
const NEXT = '22222222-2222-4222-8222-222222222222';
const OLD = '33333333-3333-4333-8333-333333333333';
const session = (user = USER, token = 'test-token') => ({ user: { id: user }, access_token: token });
const phase = (name, sid = SID, t = 10, status = name === 'CALIBRATING' ? 'COLLECTING' : 'ACCEPTED') => ({
    v: 1, t, phase: name, active: name !== 'OFF', session_id: sid,
    ...(name === 'OFF' ? {} : { calibration: {
        status, progress: status === 'COLLECTING' ? 0.5 : 1,
        valid_samples: { vision: 10, chair_ir: 10 }, required_samples: { vision: 10, chair_ir: 10 },
        readiness: { duration: true, vision: true, chair_ir: true },
        ...(status === 'FAILED' ? { error: 'calibration_timeout' } : {}),
    } }),
});
const apply = (state, type, extra = {}) => reduceLifecycle(state, {
    type, generation: state.connectionGeneration, ...extra,
});
const active = () => apply(apply(initialLifecycle(), 'CONNECTED'), 'PHASE', { event: phase('MEASURING') });
const feedback = (sid = SID) => ({ v: 1, t: 11, session_id: sid, level: 'BREAK',
    transition: true, reason: 'LOW_SCORE', recommended_break_sec: 300 });
const response = (action, sid = SID, user = USER) => new Response(JSON.stringify({
    status: 'success', measurement: { user_id: user, session_id: sid,
        status: action === 'start' ? 'ACTIVE' : 'STOPPED' },
}), { status: action === 'start' ? 201 : 200 });
const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
};

class FakeSocket {
    events = new Map();
    connects = 0;
    disconnects = 0;
    connected = false;
    constructor(options) { this.auth = options.auth; this.options = options; }
    on(name, handler) { this.events.set(name, [...(this.events.get(name) || []), handler]); }
    off(name, handler) { this.events.set(name, (this.events.get(name) || []).filter(fn => fn !== handler)); }
    fire(name, event) {
        if (name === 'connect') this.connected = true;
        if (name === 'disconnect') this.connected = false;
        for (const handler of [...(this.events.get(name) || [])]) handler(event);
    }
    connect() {
        assert.ok(this.events.has('measurement_phase'));
        assert.ok(this.events.has('state'));
        this.connects++;
    }
    disconnect() { this.disconnects++; if (this.connected) this.fire('disconnect'); }
}

function fixture(rest = async url => response(url.endsWith('/start') ? 'start' : 'stop')) {
    const sockets = [], calls = [], states = [], feedbacks = [], revisions = [];
    const auth = { getSession: async () => ({ data: { session: session() } }) };
    const runtime = createMeasurementRuntime({
        auth, backendUrl: 'http://backend.test',
        authenticatedFetch: async (...args) => { calls.push(args); return rest(...args); },
        socketFactory: (_url, options) => {
            assert.equal(options.autoConnect, false);
            const socket = new FakeSocket(options); sockets.push(socket); return socket;
        },
        onState: event => states.push(event), onFeedback: event => feedbacks.push(event),
        onChange: model => revisions.push(model.presentationRevision),
    });
    runtime.authChanged('SIGNED_IN', session());
    sockets[0].fire('connect');
    return { runtime, auth, sockets, calls, states, feedbacks, revisions, socket: sockets[0] };
}

test('OFF -> CALIBRATING -> READY -> MEASURING -> OFF; equal READY/MEASURING timestamp', () => {
    let model = apply(initialLifecycle(), 'CONNECTED');
    for (const name of ['CALIBRATING', 'READY', 'MEASURING', 'OFF']) {
        model = apply(model, 'PHASE', { event: phase(name) });
        assert.equal(model.phase, name);
        assert.equal(canRenderState(model), name === 'MEASURING');
    }
    assert.equal(model.sessionId, null);
});

test('phase before response buffers by session; late REST ACK cannot regress MEASURING', () => {
    let model = apply(apply(initialLifecycle(), 'CONNECTED'), 'START');
    for (const event of [phase('MEASURING', OLD), phase('CALIBRATING'), phase('READY'), phase('MEASURING')]) {
        model = apply(model, 'PHASE', { event });
    }
    assert.equal(canRenderState(model), false);
    model = apply(model, 'START_ACK', { request: model.requestGeneration, sessionId: SID });
    assert.equal(model.phase, 'MEASURING');
    assert.equal(model.sessionId, SID);
    assert.equal(canRenderState(model), true);
    assert.equal(model.bufferedPhases.length, 0);
});

test('REST start ACK without phase only waits for synchronization', () => {
    let model = apply(apply(initialLifecycle(), 'CONNECTED'), 'START');
    model = apply(model, 'START_ACK', { request: model.requestGeneration, sessionId: SID });
    assert.equal(model.pendingAction, 'sync');
    assert.equal(canRenderState(model), false);
    model = apply(model, 'PHASE', { event: phase('CALIBRATING') });
    assert.equal(model.pendingAction, null);
    assert.equal(model.phase, 'CALIBRATING');
});

test('stale session, regressive phase/time and invalid phase cannot overwrite current lifecycle', () => {
    const model = active();
    for (const event of [phase('OFF', OLD), phase('MEASURING', NEXT), phase('READY'),
        phase('MEASURING', SID, 9), phase('OFF', SID, 9), { ...phase('READY'), phase: 'constructor' },
        { ...phase('READY'), t: NaN }, { ...phase('READY'), session_id: 'bad' }]) {
        assert.equal(apply(model, 'PHASE', { event }), model);
    }
});

test('closed-session phases cannot reopen after OFF or overwrite a new session', () => {
    let model = apply(active(), 'PHASE', { event: phase('OFF') });
    assert.equal(apply(model, 'PHASE', { event: phase('MEASURING') }), model);
    model = apply(model, 'PHASE', { event: phase('CALIBRATING', NEXT) });
    assert.equal(model.sessionId, NEXT);
    assert.equal(apply(model, 'PHASE', { event: phase('OFF') }), model);
});

test('new calibration increments presentation revision; CALIBRATING and READY at 100% block state', () => {
    let model = active();
    const revision = model.presentationRevision;
    model = apply(model, 'START');
    assert.ok(model.presentationRevision > revision);
    assert.equal(model.calibration, null);
    assert.equal(canRenderState(model), false);
    model = apply(model, 'PHASE', { event: phase('CALIBRATING', NEXT, 20) });
    model = apply(model, 'START_ACK', { request: model.requestGeneration, sessionId: NEXT });
    assert.equal(canRenderState(model), false);
    model = apply(model, 'PHASE', { event: phase('READY', NEXT, 20, 'READY') });
    assert.equal(canRenderState(model), false);
    model = apply(model, 'PHASE', { event: phase('MEASURING', NEXT, 20) });
    assert.equal(canRenderState(model), true);
});

test('obsolete connection/request generations are ignored', () => {
    const model = active();
    assert.equal(reduceLifecycle(model, { type: 'PHASE', generation: -1, event: phase('OFF') }), model);
    assert.equal(apply(model, 'STOP_ACK', { request: -1, sessionId: SID }), model);
    assert.equal(canRenderState(model, -1), false);
});

test('feedback is phase/session/transition gated; reason maps to non-medical text', () => {
    const model = active();
    assert.ok(feedbackPresentation(model, feedback()).persistent);
    assert.equal(feedbackPresentation(model, feedback(OLD)), null);
    assert.equal(feedbackPresentation(model, { ...feedback(), transition: false }), null);
    assert.equal(feedbackPresentation(model, { ...feedback(), level: 'NORMAL' }), null);
    for (const name of ['OFF', 'CALIBRATING', 'READY']) {
        assert.equal(feedbackPresentation({ ...model, phase: name }, feedback()), null);
    }
    assert.equal(feedbackPresentation(apply(model, 'STOP'), feedback()), null);
    assert.equal(feedbackPresentation(model, feedback(), -1), null);
    assert.match(feedbackPresentation(model, feedback()).text, /작업 부하/);
});

test('FAILED calibration cannot silently switch to accepted/MEASURING in same session', () => {
    const model = apply(apply(initialLifecycle(), 'CONNECTED'), 'PHASE', {
        event: phase('CALIBRATING', SID, 20, 'FAILED'),
    });
    assert.equal(apply(model, 'PHASE', { event: phase('MEASURING', SID, 21) }), model);
});

test('pre-ACK FAILED is terminal until Stop; pre-ACK OFF cannot be revived by start ACK', () => {
    for (const terminal of ['FAILED', 'OFF']) {
        let model = apply(apply(initialLifecycle(), 'CONNECTED'), 'START');
        const event = terminal === 'OFF' ? phase('OFF') : phase('CALIBRATING', SID, 10, 'FAILED');
        model = apply(model, 'PHASE', { event });
        model = apply(model, 'PHASE', { event: phase('MEASURING', SID, 11) });
        model = apply(model, 'START_ACK', { request: model.requestGeneration, sessionId: SID });
        assert.equal(canRenderState(model), false);
        assert.equal(model.phase, terminal === 'OFF' ? 'OFF' : 'CALIBRATING');
    }
});

test('restore uses auth, handlers precede connect, never automatically starts', async () => {
    const f = fixture();
    await f.runtime.restore();
    assert.equal(f.sockets.length, 1);
    assert.equal(f.socket.connects, 1);
    assert.equal(f.socket.auth.token, 'test-token');
    assert.equal(f.calls.length, 0);
    assert.equal(f.runtime.model.phase, 'OFF');
});

test('no session produces no anonymous socket', async () => {
    let connects = 0;
    const runtime = createMeasurementRuntime({ auth: { getSession: async () => ({ data: { session: null } }) },
        authenticatedFetch: async () => { throw Error('must not request'); },
        socketFactory: () => { connects++; }, backendUrl: '' });
    await runtime.restore();
    assert.equal(connects, 0);
    assert.equal(await runtime.start(), false);
});

test('initial restore cannot overwrite newer Auth callback/logout', async () => {
    const f = fixture(), pending = deferred();
    f.auth.getSession = () => pending.promise;
    const restoring = f.runtime.restore();
    f.runtime.authChanged('SIGNED_OUT', null);
    pending.resolve({ data: { session: session() } });
    await restoring;
    assert.equal(f.runtime.hasSession(), false);
    assert.equal(f.sockets.length, 1);
});

test('restore failure distinguishes auth/network without revealing provider exceptions', async () => {
    for (const [failure, kind] of [[new TypeError('secret-provider-detail'), 'network'],
        [new Error('secret-provider-detail'), 'auth']]) {
        const f = fixture();
        f.auth.getSession = async () => { throw failure; };
        await f.runtime.restore();
        assert.equal(f.runtime.model.error.kind, kind);
        assert.ok(!f.runtime.model.error.message.includes('secret'));
    }
});

test('start consumes pre-response phase and gates state until MEASURING', async () => {
    const pending = deferred(), f = fixture(() => pending.promise);
    const starting = f.runtime.start();
    f.socket.fire('measurement_phase', phase('CALIBRATING'));
    f.socket.fire('state', { v: 1, state: 'NORMAL', score: 99 });
    pending.resolve(response('start'));
    assert.equal(await starting, true);
    assert.equal(f.runtime.model.phase, 'CALIBRATING');
    f.socket.fire('measurement_phase', phase('READY', SID, 20));
    f.socket.fire('state', { v: 1, state: 'NORMAL', score: 99 });
    assert.equal(f.states.length, 0);
    f.socket.fire('measurement_phase', phase('MEASURING', SID, 20));
    f.socket.fire('state', { v: 1, state: 'NORMAL', score: 99 });
    assert.equal(f.states.length, 1);
});

test('Stop during CALIBRATING closes session and ignores late phases/state', async () => {
    const f = fixture();
    f.socket.fire('measurement_phase', phase('CALIBRATING'));
    const stopping = f.runtime.stop();
    f.socket.fire('measurement_phase', phase('MEASURING', SID, 11));
    f.socket.fire('state', { v: 1, state: 'NORMAL' });
    assert.equal(f.states.length, 0);
    assert.equal(await stopping, true);
    f.socket.fire('measurement_phase', phase('MEASURING', SID, 12));
    assert.equal(f.runtime.model.phase, 'OFF');
    assert.equal(f.calls[0][0], 'http://backend.test/api/measurement/stop');
});

test('double Start submits once; initial handshake phase synchronizes before connect event', async () => {
    const f = fixture(), pending = deferred();
    f.socket.fire('disconnect');
    f.socket.fire('measurement_phase', phase('MEASURING'));
    assert.equal(f.runtime.canRender(), false);
    f.socket.fire('connect');
    assert.equal(f.runtime.canRender(), true);
    await f.runtime.stop();
    const g = fixture(() => pending.promise);
    const starting = g.runtime.start();
    assert.equal(await g.runtime.start(), false);
    assert.equal(g.calls.length, 1);
    pending.resolve(response('start'));
    await starting;
});

test('failed Stop can be retried successfully without allowing intervening stale state', async () => {
    let attempts = 0;
    const f = fixture(async () => {
        if (++attempts === 1) throw new WorkflowError('backend', 'safe failure');
        return response('stop');
    });
    f.socket.fire('measurement_phase', phase('MEASURING'));
    assert.equal(await f.runtime.stop(), false);
    f.socket.fire('measurement_phase', phase('MEASURING', SID, 20));
    f.socket.fire('state', { v: 1, state: 'NORMAL' });
    assert.equal(f.states.length, 0);
    assert.equal(await f.runtime.stop(), true);
    assert.equal(f.runtime.model.phase, 'OFF');
    assert.equal(f.runtime.model.error, null);
});

test('Stop while start pending serializes POSTs; late start response cannot reactivate', async () => {
    const pending = deferred();
    const f = fixture(url => url.endsWith('/start') ? pending.promise : response('stop'));
    const starting = f.runtime.start();
    await Promise.resolve();
    const stopping = f.runtime.stop();
    assert.equal(f.runtime.stop(), stopping);
    f.socket.fire('measurement_phase', phase('MEASURING'));
    f.socket.fire('state', { v: 1, state: 'NORMAL' });
    assert.equal(f.calls.length, 1);
    pending.resolve(response('start'));
    await starting;
    assert.equal(await stopping, true);
    assert.deepEqual(f.calls.map(([url]) => url.split('/').at(-1)), ['start', 'stop']);
    assert.equal(f.runtime.model.phase, 'OFF');
    assert.equal(f.runtime.canRender(), false);
    assert.equal(f.states.length, 0);
});

test('FAILED retry successfully stops before new start, clearing old calibration', async () => {
    const f = fixture(url => response(url.endsWith('/start') ? 'start' : 'stop',
        url.endsWith('/start') ? NEXT : SID));
    f.socket.fire('measurement_phase', phase('CALIBRATING', SID, 20, 'FAILED'));
    assert.equal(await f.runtime.retry(), true);
    assert.deepEqual(f.calls.map(([url]) => url.split('/').at(-1)), ['stop', 'start']);
    assert.equal(f.runtime.model.sessionId, NEXT);
    assert.equal(f.runtime.model.calibration, null);
    assert.equal(f.runtime.canRender(), false);
});

test('failed Stop blocks retry Start, leaving a safe retryable error', async () => {
    const f = fixture(async () => { throw new WorkflowError('network', 'safe network message'); });
    f.socket.fire('measurement_phase', phase('CALIBRATING', SID, 20, 'FAILED'));
    assert.equal(await f.runtime.retry(), false);
    assert.equal(f.calls.length, 1);
    assert.equal(f.runtime.model.error.kind, 'network');
    assert.equal(f.runtime.canRender(), false);
    assert.equal(await f.runtime.start(), false);
});

test('same-user token refresh updates future handshake with no disconnect or lifecycle loss', () => {
    const f = fixture();
    f.socket.fire('measurement_phase', phase('MEASURING'));
    f.runtime.authChanged('TOKEN_REFRESHED', session(USER, 'refreshed-test-token'));
    assert.equal(f.socket.auth.token, 'refreshed-test-token');
    assert.equal(f.socket.disconnects, 0);
    assert.equal(f.socket.connects, 1);
    assert.equal(f.runtime.canRender(), true);
});

test('refreshed token retries denied handshake, but not an ongoing auto-reconnection', () => {
    const f = fixture();
    f.socket.connected = false; f.socket.active = false;
    f.socket.fire('connect_error');
    assert.equal(f.runtime.model.error.kind, 'connection');
    f.runtime.authChanged('TOKEN_REFRESHED', session(USER, 'fresh-test-token'));
    assert.equal(f.socket.connects, 2);
    assert.equal(f.socket.disconnects, 0);
    f.socket.active = true;
    f.runtime.authChanged('TOKEN_REFRESHED', session(USER, 'next-test-token'));
    assert.equal(f.socket.connects, 2);
});

test('logout/user change invalidates old captured lifecycle/state/feedback callbacks', () => {
    const f = fixture();
    const oldPhase = f.socket.events.get('measurement_phase')[0];
    const oldState = f.socket.events.get('state')[0];
    const oldFeedback = f.socket.events.get('feedback')[0];
    f.runtime.authChanged('SIGNED_IN', session(OTHER_USER));
    oldPhase(phase('MEASURING')); oldState({ v: 1, state: 'NORMAL' }); oldFeedback(feedback());
    assert.equal(f.runtime.model.phase, 'OFF');
    assert.equal(f.states.length, 0);
    assert.equal(f.feedbacks.length, 0);
    assert.equal(f.socket.disconnects, 1);
    f.runtime.authChanged('SIGNED_OUT', null);
    assert.equal(f.runtime.hasSession(), false);
    assert.equal(f.sockets.length, 2);
});

test('late REST result after user change cannot bind old session', async () => {
    const pending = deferred(), f = fixture(() => pending.promise);
    const starting = f.runtime.start();
    await Promise.resolve();
    f.runtime.authChanged('SIGNED_IN', session(OTHER_USER));
    pending.resolve(response('start'));
    assert.equal(await starting, false);
    assert.equal(f.runtime.model.sessionId, null);
});

test('reconnect drops obsolete generation; owner snapshot synchronizes, last-owner cleanup stays OFF', () => {
    const f = fixture();
    f.socket.fire('measurement_phase', phase('MEASURING'));
    const oldPhase = f.socket.events.get('measurement_phase')[0];
    f.socket.fire('disconnect');
    oldPhase(phase('MEASURING'));
    assert.equal(f.runtime.model.phase, 'OFF');
    f.socket.fire('connect');
    assert.equal(f.runtime.canRender(), false);
    f.socket.fire('measurement_phase', phase('MEASURING', SID, 50));
    assert.equal(f.runtime.canRender(), true);
    f.socket.fire('disconnect');
    f.socket.fire('connect');
    assert.equal(f.runtime.model.phase, 'OFF');
    assert.equal(f.calls.length, 0);
});

test('runtime feedback only accepts matching MEASURING transitions', () => {
    const f = fixture();
    f.socket.fire('measurement_phase', phase('CALIBRATING'));
    f.socket.fire('feedback', feedback());
    f.socket.fire('measurement_phase', phase('MEASURING'));
    f.socket.fire('feedback', feedback(OLD));
    f.socket.fire('feedback', feedback());
    assert.equal(f.feedbacks.length, 1);
});

test('NORMAL dismissal retains feedback session/generation/transition validation', () => {
    const model = active();
    const recovery = { v: 1, t: 12, session_id: SID, level: 'NORMAL', transition: true };
    assert.deepEqual(feedbackPresentation(model, recovery), { dismiss: true });
    assert.deepEqual(feedbackPresentation(model, { ...recovery, reason: 'ABSENT' }), { dismiss: true });
    for (const event of [{ ...recovery, session_id: OLD }, { ...recovery, transition: false },
        { ...recovery, t: NaN }, { ...recovery, reason: 'LOW_SCORE' }]) {
        assert.equal(feedbackPresentation(model, event), null);
    }
    assert.equal(feedbackPresentation(model, recovery, -1), null);
    assert.equal(feedbackPresentation(apply(model, 'STOP'), recovery), null);
});

test('feedback timestamp guard resets per session and rejects obsolete connection callbacks', async () => {
    const f = fixture();
    f.socket.fire('measurement_phase', phase('MEASURING'));
    f.socket.fire('feedback', { ...feedback(), t: 100 });
    await f.runtime.stop();
    f.socket.fire('measurement_phase', phase('CALIBRATING', NEXT, 20));
    f.socket.fire('measurement_phase', phase('MEASURING', NEXT, 21));
    f.socket.fire('feedback', { ...feedback(NEXT), t: 10 });
    assert.equal(f.feedbacks.length, 2);
    f.socket.fire('feedback', { v: 1, t: 200, session_id: SID, level: 'NORMAL', transition: true });
    assert.equal(f.feedbacks.length, 2);
    const oldFeedback = f.socket.events.get('feedback')[0];
    f.socket.fire('disconnect');
    f.socket.fire('connect');
    f.socket.fire('measurement_phase', phase('MEASURING', NEXT, 50));
    f.socket.fire('feedback', { ...feedback(NEXT), t: 60 });
    oldFeedback({ v: 1, t: 61, session_id: NEXT, level: 'NORMAL', transition: true });
    oldFeedback({ ...feedback(NEXT), t: 62 });
    assert.equal(f.feedbacks.length, 3);
});

test('REST obtains current token, preserves options and never sends client user identity', async () => {
    let currentSession = session(), seen = [];
    const helper = createAuthenticatedFetch({ getSession: async () => ({ data: { session: currentSession } }) },
        async (_url, options) => { seen.push(options); return new Response('{}'); });
    await helper('/start', { method: 'POST', headers: { 'X-Test': 'test' } }, USER);
    currentSession = session(USER, 'new-test-token');
    await helper('/stop', { method: 'POST' }, USER);
    assert.equal(seen[0].headers.get('Authorization'), 'Bearer test-token');
    assert.equal(seen[1].headers.get('Authorization'), 'Bearer new-test-token');
    assert.equal(seen[0].headers.get('X-Test'), 'test');
    assert.equal(seen[0].body, undefined);
});

test('401 refreshes once and retries once using refreshed Bearer', async () => {
    let refreshes = 0, calls = 0;
    const helper = createAuthenticatedFetch({ getSession: async () => ({ data: { session: session() } }),
        refreshSession: async () => { refreshes++; return { data: { session: session(USER, 'new-test-token') } }; } },
        async (_url, options) => {
            calls++;
            if (calls === 1) return new Response('', { status: 401 });
            assert.equal(options.headers.get('Authorization'), 'Bearer new-test-token');
            return new Response('{}');
        });
    assert.equal((await helper('/start')).status, 200);
    assert.equal(refreshes, 1); assert.equal(calls, 2);
});

test('second 401 is safe auth failure, with no infinite retry', async () => {
    let calls = 0;
    const helper = createAuthenticatedFetch({ getSession: async () => ({ data: { session: session() } }),
        refreshSession: async () => ({ data: { session: session() } }) },
        async () => { calls++; return new Response('do-not-print-secret', { status: 401 }); });
    await assert.rejects(helper('/start'), error => error.kind === 'auth' && !error.message.includes('secret'));
    assert.equal(calls, 2);
});

test('refresh cannot retry under a different user', async () => {
    let calls = 0;
    const helper = createAuthenticatedFetch({ getSession: async () => ({ data: { session: session() } }),
        refreshSession: async () => ({ data: { session: session(OTHER_USER) } }) },
        async () => { calls++; return new Response('', { status: 401 }); });
    await assert.rejects(helper('/start', {}, USER), error => error.kind === 'auth');
    assert.equal(calls, 1);
});

test('missing session, network, backend and measurement conflict are distinct safe failures', async () => {
    const auth = { getSession: async () => ({ data: { session: session() } }) };
    const cases = [
        ['auth', { getSession: async () => ({ data: { session: null } }) }, async () => { throw Error('unreachable'); }],
        ['network', auth, async () => { throw Error('do-not-print-secret'); }],
        ['backend', auth, async () => new Response('do-not-print-secret', { status: 503 })],
        ['conflict', auth, async () => new Response(JSON.stringify({ error: { code: 'measurement_in_use' } }), { status: 409 })],
    ];
    for (const [kind, provider, fetcher] of cases) {
        await assert.rejects(createAuthenticatedFetch(provider, fetcher)('/start'), error =>
            error.kind === kind && !error.message.includes('secret'));
    }
});
