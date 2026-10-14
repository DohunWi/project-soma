import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
    authRedirectNotice, clearAuthRedirectUrl, confirmationRedirectUrl,
} from '../view/auth_redirect.mjs';
import {
    EXAMPLE_HISTORY, aggregateHistory, exampleHistoryPayload, historyRequestUrl,
} from '../view/measurement_history.mjs';

const indexHtml = readFileSync(new URL('../view/index.html', import.meta.url), 'utf8');
const authSource = readFileSync(new URL('../view/auth.js', import.meta.url), 'utf8');

test('email confirmation redirect targets the existing login page', () => {
    assert.equal(
        confirmationRedirectUrl({ href: 'http://127.0.0.1:5500/view/signup.html' }),
        'http://127.0.0.1:5500/view/login.html',
    );
    assert.match(authSource, /emailRedirectTo:\s*confirmationRedirectUrl\(\)/);
});

test('expired and invalid confirmation links produce safe actionable messages', () => {
    const expired = authRedirectNotice({ search: '?error=access_denied&error_code=otp_expired&error_description=secret', hash: '' });
    assert.equal(expired.kind, 'error');
    assert.match(expired.message, /만료/);
    assert.doesNotMatch(expired.message, /secret/);
    const invalid = authRedirectNotice({ search: '?error=access_denied', hash: '' });
    assert.match(invalid.message, /사용할 수 없습니다/);
    assert.equal(authRedirectNotice({ search: '', hash: '' }), null);
});

test('successful callback is recognized and callback parameters are removed', () => {
    assert.equal(authRedirectNotice({ search: '?code=test-code', hash: '' }).kind, 'success');
    let replaced;
    clearAuthRedirectUrl(
        { href: 'http://127.0.0.1:5500/view/login.html?code=test-code&keep=yes#error=ignored' },
        { replaceState: (_state, _title, url) => { replaced = url; } },
    );
    assert.equal(replaced, '/view/login.html?keep=yes');
});

test('history URL is authenticated-backend scoped and limited to supported 30/60 minute choices', () => {
    const now = new Date('2026-10-14T03:00:00Z');
    const url = new URL(historyRequestUrl('http://127.0.0.1:5000', 30, now));
    assert.equal(url.pathname, '/api/state/history');
    assert.equal(Date.parse(url.searchParams.get('end')) - Date.parse(url.searchParams.get('start')), 30 * 60_000);
    assert.throws(() => historyRequestUrl('http://127.0.0.1:5000', 240, now));
});

test('history aggregation excludes baseline from score points and does not fill long gaps', () => {
    const payload = {
        period: { start: '2026-10-14T00:00:00Z', end: '2026-10-14T00:10:00Z' },
        baseline: { measured_at: '2026-10-13T23:59:00Z', score: 100, state: 'NORMAL' },
        states: [
            { measured_at: '2026-10-14T00:01:00Z', score: 98, state: 'NORMAL' },
            { measured_at: '2026-10-14T00:02:00Z', score: 90, state: 'CAUTION' },
            { measured_at: '2026-10-14T00:09:00Z', score: 80, state: 'DANGER' },
        ],
    };
    const result = aggregateHistory(payload);
    assert.equal(result.sampleCount, 3);
    assert.equal(result.points.length, 3);
    assert.ok(result.points.every(point => point.measured_at !== payload.period.start));
    // The baseline -> first sample gap is two minutes, beyond the one-minute limit.
    assert.equal(Object.values(result.durationMs).reduce((sum, value) => sum + value, 0), 2 * 60_000);
    assert.deepEqual(result.transitions.map(item => item.to), ['CAUTION', 'DANGER']);
});

test('multiple snapshots in one minute are aggregated without fabricating missing buckets', () => {
    const payload = {
        period: { start: '2026-10-14T00:00:00Z', end: '2026-10-14T00:03:00Z' },
        states: [
            { measured_at: '2026-10-14T00:00:05Z', score: 100, state: 'NORMAL' },
            { measured_at: '2026-10-14T00:00:35Z', score: 90, state: 'CAUTION' },
            { measured_at: '2026-10-14T00:02:05Z', score: 80, state: 'DANGER' },
        ],
    };
    const result = aggregateHistory(payload);
    assert.deepEqual(result.points.map(point => point.score), [95, 80]);
    assert.equal(result.points.length, 2);
});

test('example history is a separate static four-hour fixture', () => {
    const payload = exampleHistoryPayload();
    assert.notEqual(payload.states, EXAMPLE_HISTORY);
    assert.equal(Date.parse(payload.period.end) - Date.parse(payload.period.start), 4 * 60 * 60_000);
    assert.deepEqual(new Set(payload.states.map(item => item.state)), new Set(['NORMAL', 'CAUTION', 'DANGER']));
});

test('production page has no legacy direct DB report and keeps a clearly marked Front-only popup demo', () => {
    assert.doesNotMatch(indexHtml, /\.from\(['"]fatigue_logs['"]\)/);
    assert.doesNotMatch(indexHtml, /23분째/);
    assert.match(indexHtml, /Front 전용 · 측정\/점수\/DB 영향 없음/);
    assert.match(indexHtml, /showTestFeedback\('NORMAL'\)/);
    assert.match(indexHtml, /showTestFeedback\('CAUTION'\)/);
    assert.match(indexHtml, /showTestFeedback\('DANGER'\)/);
    assert.match(indexHtml, /showTestFeedback\('BREAK'\)/);
    assert.doesNotMatch(indexHtml, /addEventListener\(['"]load['"],\s*initCamera/);
    assert.match(indexHtml, /카메라는 테스트 버튼을 누를 때만 켜집니다/);
    assert.match(indexHtml, /실제 기록 아님/);
});
