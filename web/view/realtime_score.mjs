// Front-only five-minute chart. state.t is sender/Fusion measurement time (Unix seconds),
// never browser receipt time. The window freezes at the last event when measurement stops.
import { canRenderState } from './measurement_phase.mjs';

export const REALTIME_WINDOW_SEC = 300;
const EXAMPLE_START = Date.parse('2026-01-01T00:00:00Z') / 1000;
// Illustrative states, NOT Front score thresholds or actual measurement results.
export const REALTIME_EXAMPLE = Object.freeze([
    [100, 'NORMAL'], [98, 'NORMAL'], [96, 'NORMAL'], [90, 'CAUTION'],
    [82, 'CAUTION'], [70, 'CAUTION'], [55, 'DANGER'], [60, 'DANGER'],
    [74, 'CAUTION'], [88, 'NORMAL'], [97, 'NORMAL'],
].map(([score, state], index) => Object.freeze({ t: EXAMPLE_START + index * 30, score, state })));

export function aggregateRealtimeScores(samples, end, intervalSec = 30) {
    if (![30, 60].includes(intervalSec)) throw new Error('invalid realtime interval');
    if (!Number.isFinite(end)) return [];
    const start = end - REALTIME_WINDOW_SEC;
    const buckets = new Map();
    for (const sample of samples) {
        if (!Number.isFinite(sample.t) || !Number.isFinite(sample.score) ||
            sample.t < start || sample.t > end) continue;
        const bucket = Math.floor(sample.t / intervalSec) * intervalSec;
        const values = buckets.get(bucket) || [];
        values.push(sample.score);
        buckets.set(bucket, values);
    }
    if (!buckets.size) return [];
    const points = [];
    for (let t = Math.floor(start / intervalSec) * intervalSec; t <= end; t += intervalSec) {
        const values = buckets.get(t);
        points.push({ t, score: values ? values.reduce((sum, value) => sum + value, 0) / values.length : null,
            count: values?.length || 0 });
    }
    return points;
}

export function createRealtimeScoreHistory() {
    let userId = null;
    let sessionId = null;
    let generation = null;
    let phase = 'OFF';
    let preparing = false;
    let measurementFloor = null;
    let end = null;
    let samples = new Map();
    let seen = new Set();
    let mode = 'actual';
    let intervalSec = 30;
    const clear = () => {
        samples = new Map(); seen = new Set(); end = null; measurementFloor = null;
    };
    return {
        setUser(nextUserId) {
            if (userId === nextUserId) return;
            userId = nextUserId;
            sessionId = null; generation = null; phase = 'OFF'; preparing = false;
            clear(); mode = 'actual';
        },
        syncLifecycle(model) {
            generation = model.connectionGeneration;
            const start = model.pendingAction === 'start';
            const newSession = !!model.sessionId && model.sessionId !== sessionId;
            const calibration = model.phase === 'CALIBRATING' && phase !== 'CALIBRATING';
            if ((start && !preparing) || newSession || calibration) {
                clear(); mode = 'actual';
            }
            if (start) sessionId = null;
            else if (model.sessionId) sessionId = model.sessionId;
            preparing = start;
            phase = model.phase;
            if (phase === 'MEASURING' && measurementFloor === null && Number.isFinite(model.phaseTime)) {
                measurementFloor = model.phaseTime;
            }
            // OFF/Stop preserves the buffer. User changes and a new preparation reset it.
        },
        record(event, model) {
            if (!userId || !model || !canRenderState(model) || model.sessionId !== sessionId ||
                model.connectionGeneration !== generation || event?.v !== 1 ||
                !['NORMAL', 'CAUTION', 'DANGER', 'ABSENT'].includes(event.state) ||
                !Number.isFinite(event.t) || event.t <= 0 ||
                (measurementFloor !== null && event.t < measurementFloor) ||
                (event.session_id && event.session_id !== sessionId) ||
                (end !== null && event.t < end - REALTIME_WINDOW_SEC) || seen.has(event.t)) return false;
            // No session_id in the current state contract: retain runtime generation/phase
            // guards and reject pre-MEASURING timestamps, without inventing an identity.
            if (event.state !== 'ABSENT' && (!Number.isInteger(event.score) ||
                event.score < 0 || event.score > 100)) return false;
            end = Math.max(end ?? event.t, event.t);
            seen.add(event.t);
            if (event.state !== 'ABSENT') samples.set(event.t, { t: event.t, score: event.score });
            // ABSENT advances time but contributes no numeric score (existing presentation policy).
            for (const t of seen) if (t < end - REALTIME_WINDOW_SEC) seen.delete(t);
            for (const t of samples.keys()) if (t < end - REALTIME_WINDOW_SEC) samples.delete(t);
            return true;
        },
        setMode(nextMode) {
            if (!['actual', 'example'].includes(nextMode)) throw new Error('invalid realtime mode');
            mode = nextMode;
        },
        setInterval(value) {
            if (![30, 60].includes(value)) throw new Error('invalid realtime interval');
            intervalSec = value;
        },
        view() {
            const example = mode === 'example';
            const source = example ? REALTIME_EXAMPLE : [...samples.values()];
            const last = example ? REALTIME_EXAMPLE.at(-1).t : end;
            return { mode, intervalSec, phase, sessionId, end: last,
                points: aggregateRealtimeScores(source, last, intervalSec),
                sampleCount: source.length };
        },
    };
}
