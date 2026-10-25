import test from 'node:test';
import assert from 'node:assert/strict';
import {
    formatObservedDuration, sessionReportChartSeries, sessionReportFromPayload,
    sessionReportUrl,
} from '../view/session_report.mjs';

const USER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const SID = '11111111-1111-4111-8111-111111111111';

function report() {
    return {
        v: 1, status: 'success', stream: { user_id: USER, session_id: SID },
        period: { start: '2026-10-25T01:00:00Z', end: '2026-10-25T01:02:10Z',
            basis: 'snapshot_bounds', lifecycle_exact: false },
        summary: {
            snapshot_span_sec: 130, observed_coverage_sec: 60,
            seated_observed_sec: 40, absent_observed_sec: 20, unknown_gap_sec: 70,
            average_score: 87.5, minimum_score: 70,
            state_seconds: { NORMAL: 20, CAUTION: 20, DANGER: 0, ABSENT: 20 },
            major_reasons: [{ reason: 'static_hold', seconds: 20 }],
            break_feedback: { supported: false, occurred: null,
                message: '현재 저장 데이터로 확인 불가' },
        },
        quality: {
            snapshot_count: 3, valid_interval_count: 1, long_gap_count: 1,
            nonpositive_interval_count: 0, gap_limit_sec: 60, single_snapshot: false,
            score_model_versions: ['soma_load_v1'], legacy_unversioned: false,
            trigger_counts: { state_change: 2, periodic: 1 },
        },
        timeseries: [
            { measured_at: '2026-10-25T01:00:00Z', state: 'NORMAL', score: 100, gap_before: false },
            { measured_at: '2026-10-25T01:00:20Z', state: 'CAUTION', score: 85, gap_before: false },
            { measured_at: '2026-10-25T01:02:10Z', state: 'DANGER', score: 70, gap_before: true },
        ],
    };
}

test('report URL is Backend scoped and contains no client user identity', () => {
    const url = new URL(sessionReportUrl('http://127.0.0.1:5000', SID));
    assert.equal(url.pathname, `/api/state/history/sessions/${SID}/report`);
    assert.equal(url.search, '');
    assert.throws(() => sessionReportUrl('http://127.0.0.1:5000', '../other'));
});

test('report validation enforces authenticated user and selected session ownership', () => {
    assert.equal(sessionReportFromPayload(report(), USER, SID).summary.average_score, 87.5);
    assert.throws(() => sessionReportFromPayload(report(), 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', SID));
    assert.throws(() => sessionReportFromPayload(report(), USER, '22222222-2222-4222-8222-222222222222'));
    const invalidGap = report();
    invalidGap.quality.gap_limit_sec = 0;
    assert.throws(() => sessionReportFromPayload(invalidGap, USER, SID));
});

test('ABSENT-only report accepts null seated score summaries', () => {
    const absentOnly = report();
    absentOnly.summary.average_score = null;
    absentOnly.summary.minimum_score = null;
    absentOnly.summary.seated_observed_sec = 0;
    absentOnly.summary.absent_observed_sec = 20;
    absentOnly.summary.state_seconds = { NORMAL: 0, CAUTION: 0, DANGER: 0, ABSENT: 20 };
    assert.equal(sessionReportFromPayload(absentOnly, USER, SID).summary.minimum_score, null);
});

test('full-session chart inserts a null point before gaps over sixty seconds', () => {
    const series = sessionReportChartSeries(report().timeseries);
    assert.deepEqual(series.map(point => point.score), [100, 85, null, 70]);
    assert.equal(series[2].gap, true);
});

test('duration labels remain explicit and do not imply lifecycle time', () => {
    assert.equal(formatObservedDuration(0), '0초');
    assert.equal(formatObservedDuration(65), '1분 5초');
    assert.equal(formatObservedDuration(3661), '1시간 1분 1초');
    assert.equal(formatObservedDuration(null), '—');
});
