import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
    REALTIME_EXAMPLE, aggregateRealtimeScores, createRealtimeScoreHistory,
} from '../view/realtime_score.mjs';
import { EXAMPLE_HISTORY } from '../view/measurement_history.mjs';

const SID = '11111111-1111-4111-8111-111111111111';
const NEXT = '22222222-2222-4222-8222-222222222222';
const BASE = Date.parse('2026-01-01T00:00:00Z') / 1000;
const state = (t, score = 90, name = 'NORMAL') => ({ v: 1, t, score, state: name });
const values = history => history.view().points.filter(point => point.count).map(point => point.score);
function fixture() {
    const history = createRealtimeScoreHistory();
    history.setUser('user-a');
    const model = { connected: true, phase: 'MEASURING', phaseTime: BASE,
        sessionId: SID, connectionGeneration: 1, pendingAction: null, stopRequested: false };
    history.syncLifecycle(model);
    return { history, model };
}

test('event measurement time, irregular samples, duplicates, and out-of-order delivery use arithmetic means', () => {
    const { history, model } = fixture();
    assert.equal(history.record(state(BASE + 1, 100), model), true);
    history.record(state(BASE + 9, 80), model);
    assert.equal(history.record(state(BASE + 9, 10), model), false);
    history.record(state(BASE + 31, 60), model);
    history.record(state(BASE + 20, 90), model); // Late delivery in the same window.
    assert.deepEqual(values(history), [90, 60]);
    assert.equal(history.view().end, BASE + 31); // Never Date.now()/receipt time.
    history.setInterval(60);
    assert.deepEqual(values(history), [82.5]);
    assert.equal(history.view().sampleCount, 4);
});

test('half-open interval boundaries and missing buckets retain null, including a real zero score', () => {
    const { history, model } = fixture();
    history.record(state(BASE + 29, 100), model);
    history.record(state(BASE + 30, 0), model);
    history.record(state(BASE + 91, 80), model);
    const points = history.view().points.filter(point => point.t >= BASE);
    assert.deepEqual(points.map(point => point.score), [100, 0, null, 80]);
    assert.deepEqual(points.map(point => point.count), [1, 1, 0, 1]);
});

test('sliding five minutes excludes older samples and includes the exact start boundary', () => {
    const { history, model } = fixture();
    history.record(state(BASE, 100), model);
    history.record(state(BASE + 1, 90), model);
    history.record(state(BASE + 301, 70), model);
    assert.deepEqual(values(history), [90, 70]);
    assert.equal(history.view().sampleCount, 2);
    assert.equal(history.record(state(BASE, 10), model), false);
});

test('CALIBRATING/READY/OFF and stop-in-flight cannot create scores; invalid scores/timestamps are ignored', () => {
    const { history, model } = fixture();
    for (const phase of ['CALIBRATING', 'READY', 'OFF']) {
        const blocked = { ...model, phase };
        history.syncLifecycle(blocked);
        assert.equal(history.record(state(BASE + 1), blocked), false);
    }
    history.syncLifecycle(model);
    for (const event of [state(BASE + 1, NaN), state(BASE + 1, null), state(BASE + 1, 101),
        state(BASE + 1, -1), state(BASE + 1, 1.5), state(NaN), state(0),
        { v: 1, state: 'NORMAL', score: 80 }, { ...state(BASE + 1), v: 2 }]) {
        assert.equal(history.record(event, model), false);
    }
    assert.equal(history.record(state(BASE + 1), { ...model, stopRequested: true }), false);
    assert.deepEqual(history.view().points, []);
});

test('Stop retains final graph/interval; new Start clears and switches away from example', () => {
    const { history, model } = fixture();
    history.record(state(BASE + 1, 85), model);
    history.setInterval(60);
    const before = history.view().points;
    history.syncLifecycle({ ...model, phase: 'OFF', sessionId: null });
    assert.deepEqual(history.view().points, before);
    assert.equal(history.record(state(BASE + 2), { ...model, phase: 'OFF', sessionId: null }), false);
    history.setMode('example');
    history.syncLifecycle({ ...model, phase: 'OFF', sessionId: null, pendingAction: 'start' });
    assert.equal(history.view().mode, 'actual');
    assert.equal(history.view().intervalSec, 60);
    assert.deepEqual(history.view().points, []);
    history.syncLifecycle({ ...model, phase: 'CALIBRATING', sessionId: NEXT, phaseTime: BASE + 50 });
    assert.deepEqual(history.view().points, []);
});

test('new session/user/generation isolation and pre-MEASURING timestamp protection', () => {
    const { history, model } = fixture();
    history.record(state(BASE + 1), model);
    const next = { ...model, sessionId: NEXT, phaseTime: BASE + 50, connectionGeneration: 2 };
    history.syncLifecycle(next);
    assert.equal(history.record(state(BASE + 60), model), false);
    assert.equal(history.record(state(BASE + 1), next), false);
    assert.equal(history.record({ ...state(BASE + 60), session_id: SID }, next), false);
    assert.equal(history.record(state(BASE + 60, 88), next), true);
    assert.deepEqual(values(history), [88]);
    history.setUser('user-b');
    assert.deepEqual(history.view().points, []);
    assert.equal(history.record(state(BASE + 61), next), false);
    history.setUser(null);
    history.syncLifecycle(next);
    assert.equal(history.record(state(BASE + 62), next), false);
});

test('ABSENT adds no score but advances window; return resumes real scores', () => {
    const { history, model } = fixture();
    history.record(state(BASE, 90), model);
    history.record(state(BASE + 60, 100, 'ABSENT'), model);
    assert.deepEqual(values(history), [90]);
    history.record(state(BASE + 301, 100, 'ABSENT'), model);
    assert.deepEqual(history.view().points, []);
    history.record(state(BASE + 302, 83), model);
    assert.deepEqual(values(history), [83]);
});

test('example has 11 fixed 30-second points; 1-minute mode averages the same fixture', () => {
    assert.equal(REALTIME_EXAMPLE.length, 11);
    assert.equal(REALTIME_EXAMPLE.at(-1).t - REALTIME_EXAMPLE[0].t, 300);
    assert.ok(Object.isFrozen(REALTIME_EXAMPLE) && REALTIME_EXAMPLE.every(Object.isFrozen));
    assert.deepEqual(new Set(REALTIME_EXAMPLE.map(point => point.state)), new Set(['NORMAL', 'CAUTION', 'DANGER']));
    const history = createRealtimeScoreHistory();
    history.setMode('example');
    assert.deepEqual(values(history), REALTIME_EXAMPLE.map(point => point.score));
    history.setInterval(60);
    assert.deepEqual(values(history), [99, 93, 76, 57.5, 81, 97]);
    history.setMode('actual');
    assert.deepEqual(history.view().points, []);
});

test('example/actual toggles never mix real buffers or alter lower four-hour history fixture', () => {
    const { history, model } = fixture();
    const original = JSON.stringify(EXAMPLE_HISTORY);
    history.record(state(BASE + 1, 45), model);
    history.setMode('example');
    history.record(state(BASE + 2, 75), model); // Actual events are still buffered separately.
    assert.equal(history.view().sampleCount, 11);
    history.setInterval(60);
    history.setMode('actual');
    assert.deepEqual(values(history), [60]);
    assert.equal(history.view().sampleCount, 2);
    history.setMode('example');
    assert.deepEqual(values(history), [99, 93, 76, 57.5, 81, 97]);
    assert.equal(JSON.stringify(EXAMPLE_HISTORY), original);
});

test('fresh page has empty actual graph; interval validation and deployment copy consistency', () => {
    const history = createRealtimeScoreHistory();
    assert.equal(history.view().mode, 'actual');
    assert.equal(history.view().intervalSec, 30);
    assert.deepEqual(history.view().points, []);
    assert.throws(() => history.setInterval(10));
    assert.throws(() => history.setMode('other'));
    assert.throws(() => aggregateRealtimeScores([], BASE, 10));
    assert.equal(readFileSync(new URL('../view/realtime_score.mjs', import.meta.url), 'utf8'),
        readFileSync(new URL('../realtime_score.mjs', import.meta.url), 'utf8'));
    const source = readFileSync(new URL('../view/realtime_score.mjs', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /\bfetch\(|\.emit\(|supabase|localStorage/);
});
