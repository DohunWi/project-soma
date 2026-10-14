export const HISTORY_GAP_LIMIT_MS = 60_000;

const SESSION_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function recordedHistoryRequestUrl(backendUrl, session, minutes) {
    if (!SESSION_UUID.test(session?.session_id) || !Number.isFinite(Date.parse(session.last_measured_at))) {
        throw new Error("invalid recorded session");
    }
    const url = new URL(historyRequestUrl(backendUrl, minutes, session.last_measured_at));
    url.pathname = "/api/state/history/sessions/" + session.session_id;
    return url.toString();
}

export function historyFailure(error) {
    if (error?.code === "history_unavailable") return { status: "unavailable", message: "DB 기록 조회를 사용할 수 없습니다. 실시간 측정과는 별개입니다. 잠시 후 새로고침해 주세요." };
    if (error?.kind === "auth" || error?.code === "auth_unavailable") return { status: "auth", message: "인증을 확인할 수 없습니다. 연결을 확인하거나 다시 로그인해 주세요." };
    if (error?.code === "no_active_session") return { status: "no_active_session", message: "현재 ACTIVE 측정이 없습니다. 측정 종료 후 저장된 session을 선택해 주세요." };
    if (error?.code === "session_not_found") return { status: "missing_session", message: "선택한 기록을 찾을 수 없습니다. 목록을 새로고침해 주세요." };
    if (error?.kind === "network") return { status: "network", message: "Backend에 연결할 수 없습니다. 연결 후 새로고침해 주세요." };
    return { status: "error", message: "측정 기록을 불러오지 못했습니다. 새로고침해 주세요." };
}

export function createHistoryBrowser({ authenticatedFetch, backendUrl, onChange = () => {}, onData = () => {} }) {
    let revision = 0;
    let model = { userId: null, activeSessionId: null, sessions: [], selectedSessionId: null,
        mode: "actual", status: "signed_out", message: "실제 측정 기록을 조회하려면 로그인해 주세요." };
    const update = values => { model = { ...model, ...values }; onChange(model); };
    const checked = (payload, expectedSession) => {
        if (payload?.v !== 1 || payload.status !== "success" || payload.stream?.user_id !== model.userId ||
            payload.stream.session_id !== expectedSession || !Array.isArray(payload.states) ||
            !payload.states.every(validSnapshot)) throw new Error("invalid history response");
        return aggregateHistory(payload);
    };
    async function load({ refreshSessions = true } = {}) {
        const request = ++revision;
        const owner = model.userId;
        const active = model.activeSessionId;
        const valid = () => request === revision && owner === model.userId && active === model.activeSessionId;
        update({ mode: "actual", status: owner ? "loading" : "signed_out",
            message: owner ? "Backend에서 저장된 snapshot을 불러오는 중입니다..." : "실제 측정 기록을 조회하려면 로그인해 주세요." });
        if (!owner) return;
        try {
            let selected;
            if (!active) {
                if (refreshSessions) {
                    const response = await authenticatedFetch(new URL("/api/state/history/sessions", backendUrl).toString(), { method: "GET" }, owner);
                    const payload = await response.json();
                    if (!valid()) return;
                    if (payload?.v !== 1 || payload.status !== "success" || payload.user_id !== owner ||
                        !Array.isArray(payload.sessions) || !payload.sessions.every(session =>
                            SESSION_UUID.test(session?.session_id) && Number.isInteger(session.snapshot_count) &&
                            session.snapshot_count > 0 && Number.isFinite(Date.parse(session.first_measured_at)) &&
                            Number.isFinite(Date.parse(session.last_measured_at)) &&
                            Date.parse(session.first_measured_at) <= Date.parse(session.last_measured_at))) {
                        throw new Error("invalid session list");
                    }
                    update({ sessions: payload.sessions });
                }
                selected = model.sessions.find(session => session.session_id === model.selectedSessionId) || model.sessions[0];
                update({ selectedSessionId: selected?.session_id || null });
                if (!selected) {
                    update({ status: "empty_sessions", message: "저장된 session 기록이 없습니다. snapshot 시각은 실제 Start/Stop 시각이 아닙니다." });
                    return;
                }
            }
            const url = active ? historyRequestUrl(backendUrl, model.minutes || 30) :
                recordedHistoryRequestUrl(backendUrl, selected, model.minutes || 30);
            const response = await authenticatedFetch(url, { method: "GET" }, owner);
            const payload = await response.json();
            if (!valid()) return;
            const result = checked(payload, active || selected.session_id);
            update({ status: result.sampleCount ? "ready" : "empty_snapshots",
                message: result.sampleCount ? (active ?
                    "현재 ACTIVE session의 저장 snapshot입니다." :
                    "선택 session의 마지막 snapshot 기준 기록입니다. 첫·마지막 snapshot은 실제 Start/Stop 시각이 아닙니다.") :
                    "선택 구간에 저장된 snapshot이 없습니다." });
            onData(result, active ? "현재 측정 기록" : "저장된 session 기록");
        } catch (error) {
            if (valid()) update(historyFailure(error));
        }
    }
    return {
        get model() { return model; },
        setContext(userId, activeSessionId = null) {
            if (userId === model.userId && activeSessionId === model.activeSessionId) return;
            revision++;
            update({ userId, activeSessionId, sessions: [], selectedSessionId: null, mode: "actual",
                status: userId ? "loading" : "signed_out", message: userId ? "기록을 확인하는 중입니다..." : "실제 측정 기록을 조회하려면 로그인해 주세요." });
            // Defer REST auth outside Supabase's synchronous auth-state callback.
            const request = revision;
            queueMicrotask(() => { if (request === revision) void load(); });
        },
        refresh: () => load(),
        select(sessionId) {
            if (model.activeSessionId || !model.sessions.some(session => session.session_id === sessionId)) return Promise.resolve();
            update({ selectedSessionId: sessionId });
            return load({ refreshSessions: false });
        },
        setMinutes(minutes) {
            if (![30, 60].includes(minutes)) throw new Error("invalid history range");
            update({ minutes });
            return load({ refreshSessions: false });
        },
        showExample() {
            revision++;
            update({ mode: "example", status: "ready", message: "시연용 4시간 예시입니다. 사용자 DB 및 실제 측정 기록이 아닙니다." });
            onData(aggregateHistory(exampleHistoryPayload(), { bucketMinutes: 20, gapLimitMs: 21 * 60_000 }), "예시 데이터");
        },
    };
}
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
    const timeline = [...(baseline ? [baseline] : []), ...states];
    for (let index = 0; index < timeline.length; index++) {
        const current = timeline[index];
        const start = Math.max(Date.parse(current.measured_at), periodStart);
        const next = index + 1 < timeline.length ? Date.parse(timeline[index + 1].measured_at) : periodEnd;
        const elapsed = Math.max(0, Math.min(next, periodEnd) - start);
        // A snapshot is never stretched across an arbitrary outage.
        if (next - Date.parse(current.measured_at) <= gapLimitMs) durationMs[current.state] += elapsed;
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
