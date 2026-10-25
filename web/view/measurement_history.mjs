export const HISTORY_GAP_LIMIT_MS = 60_000;

export const EXAMPLE_HISTORY = Object.freeze([
    ["2026-01-01T09:00:00Z", 100, "NORMAL"],
    ["2026-01-01T09:20:00Z", 98, "NORMAL"],
    ["2026-01-01T09:40:00Z", 94, "CAUTION"],
    ["2026-01-01T10:00:00Z", 89, "CAUTION"],
    ["2026-01-01T10:20:00Z", 83, "DANGER"],
    ["2026-01-01T10:40:00Z", 78, "DANGER"],
    ["2026-01-01T11:00:00Z", 84, "CAUTION"],
    ["2026-01-01T11:20:00Z", 91, "CAUTION"],
    ["2026-01-01T11:40:00Z", 97, "NORMAL"],
    ["2026-01-01T12:00:00Z", 95, "NORMAL"],
    ["2026-01-01T12:20:00Z", 90, "CAUTION"],
    ["2026-01-01T12:40:00Z", 86, "CAUTION"],
    ["2026-01-01T13:00:00Z", 93, "NORMAL"],
].map(([measured_at, score, state]) => Object.freeze({ measured_at, score, state })));

const VALID_STATES = new Set(["NORMAL", "CAUTION", "DANGER", "ABSENT"]);
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function validSnapshot(value) {
    const timestamp = Date.parse(value?.measured_at);
    return Number.isFinite(timestamp) && VALID_STATES.has(value?.state) &&
        Number.isFinite(value?.score) && value.score >= 0 && value.score <= 100;
}

export function historyRequestUrl(backendUrl, minutes, now = new Date()) {
    if (![30, 60].includes(minutes)) throw new Error("history range must be 30 or 60 minutes");
    const end = new Date(now);
    const start = new Date(end.getTime() - minutes * 60_000);
    const url = new URL("/api/state/history", backendUrl);
    url.searchParams.set("start", start.toISOString());
    url.searchParams.set("end", end.toISOString());
    return url.toString();
}

export function recordedSessionsUrl(backendUrl) {
    return new URL("/api/state/history/sessions", backendUrl).toString();
}

export function recordedSessionsFromPayload(payload, userId) {
    if (payload?.v !== 1 || payload.status !== "success" || payload.user_id !== userId ||
        !Array.isArray(payload.sessions)) throw new Error("invalid session list");
    const seen = new Set();
    for (const session of payload.sessions) {
        const first = Date.parse(session?.first_measured_at);
        const last = Date.parse(session?.last_measured_at);
        if (!SESSION_ID.test(session?.session_id) || seen.has(session.session_id) ||
            !Number.isFinite(first) || !Number.isFinite(last) || first > last ||
            !Number.isInteger(session.snapshot_count) || session.snapshot_count < 1) {
            throw new Error("invalid recorded session");
        }
        seen.add(session.session_id);
    }
    return payload.sessions;
}

export function recordedHistoryRequestUrl(backendUrl, session, minutes) {
    if (!SESSION_ID.test(session?.session_id) || !Number.isFinite(Date.parse(session.last_measured_at))) {
        throw new Error("invalid recorded session");
    }
    // Anchor archived history to its last saved snapshot, never today's clock.
    const url = new URL(historyRequestUrl(backendUrl, minutes, session.last_measured_at));
    url.pathname = `/api/state/history/sessions/${session.session_id}`;
    return url.toString();
}

export function recordedSessionLabel(session) {
    const options = { dateStyle: "short", timeStyle: "short" };
    const first = new Date(session.first_measured_at).toLocaleString('ko-KR', options);
    const last = new Date(session.last_measured_at).toLocaleString('ko-KR', options);
    return `${first} ~ ${last} · ${session.snapshot_count}개 · ${session.session_id.slice(0, 8)}`;
}

export function aggregateHistory(payload, { bucketMinutes = 1, gapLimitMs = HISTORY_GAP_LIMIT_MS } = {}) {
    const periodStart = Date.parse(payload?.period?.start);
    const periodEnd = Date.parse(payload?.period?.end);
    if (!Number.isFinite(periodStart) || !Number.isFinite(periodEnd) || periodStart >= periodEnd) {
        throw new Error("invalid history period");
    }
    const states = (payload.states || []).filter(validSnapshot)
        .sort((a, b) => Date.parse(a.measured_at) - Date.parse(b.measured_at));
    const bucketMs = bucketMinutes * 60_000;
    const grouped = new Map();
    for (const sample of states) {
        const time = Date.parse(sample.measured_at);
        const key = Math.floor(time / bucketMs) * bucketMs;
        const bucket = grouped.get(key) || { scores: [], last: sample };
        bucket.scores.push(sample.score);
        bucket.last = sample;
        grouped.set(key, bucket);
    }
    const points = [...grouped.entries()].sort(([a], [b]) => a - b).map(([time, bucket]) => ({
        measured_at: new Date(time).toISOString(),
        score: Math.round(bucket.scores.reduce((sum, value) => sum + value, 0) / bucket.scores.length),
        state: bucket.last.state,
    }));

    const durationMs = { NORMAL: 0, CAUTION: 0, DANGER: 0, ABSENT: 0 };
    const baseline = validSnapshot(payload.baseline) ? payload.baseline : null;
    const timeline = [...(baseline ? [{ ...baseline, measured_at: payload.period.start }] : []), ...states];
    for (let index = 0; index < timeline.length; index++) {
        const current = timeline[index];
        const start = Math.max(Date.parse(current.measured_at), periodStart);
        const next = index + 1 < timeline.length ? Date.parse(timeline[index + 1].measured_at) : periodEnd;
        const elapsed = Math.max(0, Math.min(next, periodEnd) - start);
        // A snapshot is never stretched across an arbitrary outage.
        if (elapsed <= gapLimitMs) durationMs[current.state] += elapsed;
    }
    const transitions = [];
    let previous = baseline?.state || null;
    for (const sample of states) {
        if (sample.state !== previous) {
            transitions.push({ measured_at: sample.measured_at, from: previous, to: sample.state });
            previous = sample.state;
        }
    }
    return { points, durationMs, transitions, sampleCount: states.length };
}

export function exampleHistoryPayload() {
    return {
        period: { start: EXAMPLE_HISTORY[0].measured_at, end: EXAMPLE_HISTORY.at(-1).measured_at },
        states: EXAMPLE_HISTORY.map(value => ({ ...value })),
    };
}
