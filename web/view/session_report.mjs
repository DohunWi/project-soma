const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATES = ["NORMAL", "CAUTION", "DANGER", "ABSENT"];
const STATE_SET = new Set(STATES);
const REASONS = new Set(["low_blink", "close_distance", "imbalance", "static_hold", "no_backrest"]);
export const SESSION_REPORT_GAP_LIMIT_MS = 60_000;

const finiteNonnegative = value => Number.isFinite(value) && value >= 0;

export function sessionReportUrl(backendUrl, sessionId) {
    if (!SESSION_ID.test(sessionId)) throw new Error("invalid session id");
    return new URL(`/api/state/history/sessions/${sessionId}/report`, backendUrl).toString();
}

export function sessionReportFromPayload(payload, userId, sessionId) {
    if (payload?.v !== 1 || payload.status !== "success" ||
        payload.stream?.user_id !== userId || payload.stream?.session_id !== sessionId ||
        payload.period?.basis !== "snapshot_bounds" || payload.period?.lifecycle_exact !== false ||
        !Number.isFinite(Date.parse(payload.period.start)) ||
        !Number.isFinite(Date.parse(payload.period.end))) {
        throw new Error("invalid session report");
    }
    const summary = payload.summary;
    const quality = payload.quality;
    if (!summary || !quality || !Array.isArray(payload.timeseries) ||
        ![summary.snapshot_span_sec, summary.observed_coverage_sec,
            summary.seated_observed_sec, summary.absent_observed_sec,
            summary.unknown_gap_sec].every(finiteNonnegative) ||
        !(summary.average_score === null || (Number.isFinite(summary.average_score) &&
            summary.average_score >= 0 && summary.average_score <= 100)) ||
        !(summary.minimum_score === null || (Number.isInteger(summary.minimum_score) &&
            summary.minimum_score >= 0 && summary.minimum_score <= 100)) ||
        !STATES.every(state => finiteNonnegative(summary.state_seconds?.[state])) ||
        !Array.isArray(summary.major_reasons) ||
        summary.major_reasons.some(item => !REASONS.has(item?.reason) || !finiteNonnegative(item.seconds)) ||
        summary.break_feedback?.supported !== false || summary.break_feedback?.occurred !== null ||
        !Number.isInteger(quality.snapshot_count) || quality.snapshot_count < 1 ||
        !finiteNonnegative(quality.gap_limit_sec) || quality.gap_limit_sec === 0 ||
        !Array.isArray(quality.score_model_versions) ||
        typeof quality.legacy_unversioned !== "boolean" ||
        payload.timeseries.length !== quality.snapshot_count) {
        throw new Error("invalid session report");
    }
    let previous = -Infinity;
    for (const point of payload.timeseries) {
        const timestamp = Date.parse(point?.measured_at);
        if (!Number.isFinite(timestamp) || timestamp < previous || !STATE_SET.has(point?.state) ||
            !Number.isInteger(point?.score) || point.score < 0 || point.score > 100 ||
            typeof point.gap_before !== "boolean") {
            throw new Error("invalid report timeseries");
        }
        previous = timestamp;
    }
    return payload;
}

export function sessionReportChartSeries(timeseries) {
    const series = [];
    let previousTime = null;
    for (const point of timeseries) {
        const time = Date.parse(point.measured_at);
        const gap = previousTime !== null &&
            (point.gap_before || time - previousTime > SESSION_REPORT_GAP_LIMIT_MS);
        if (gap) series.push({ measured_at: point.measured_at, score: null, state: null, gap: true });
        series.push({ ...point, gap: false });
        previousTime = time;
    }
    return series;
}

export function formatObservedDuration(seconds) {
    if (!finiteNonnegative(seconds)) return "—";
    const rounded = Math.round(seconds);
    const hours = Math.floor(rounded / 3600);
    const minutes = Math.floor((rounded % 3600) / 60);
    const secs = rounded % 60;
    if (hours) return `${hours}시간 ${minutes}분 ${secs}초`;
    if (minutes) return `${minutes}분 ${secs}초`;
    return `${secs}초`;
}
