"""Session report aggregation and authenticated API tests."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.app import SESSION_REPORT_VALIDATOR
from server.session_report import (
    SESSION_REPORT_SQL,
    SessionReportNotFound,
    SessionReportReader,
    SessionReportUnavailable,
    build_session_report,
)
from server.tests.test_app import OTHER_TOKEN, USER_ID, USER_TOKEN, make_app


SID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_SID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
START = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)


def snapshot(offset, state, score, *, reasons=(), model="soma_load_v1", trigger="periodic"):
    return {
        "measured_at": START + timedelta(seconds=offset),
        "trigger": trigger,
        "state": state,
        "score": score,
        "reasons": list(reasons),
        "score_model_version": model,
    }


def test_report_uses_only_positive_intervals_up_to_sixty_seconds():
    rows = [
        snapshot(0, "NORMAL", 100, trigger="state_change"),
        snapshot(10, "CAUTION", 80, reasons=("static_hold",)),
        snapshot(30, "ABSENT", 100, trigger="state_change"),
        snapshot(50, "DANGER", 60, reasons=("low_blink",), trigger="state_change"),
        snapshot(120, "DANGER", 50, reasons=("low_blink",)),
        snapshot(125, "NORMAL", 90, trigger="state_change"),
    ]

    report = build_session_report(USER_ID, SID, rows)
    SESSION_REPORT_VALIDATOR.validate(report)
    summary = report["summary"]

    assert summary["snapshot_span_sec"] == 125
    assert summary["observed_coverage_sec"] == 55
    assert summary["seated_observed_sec"] == 35
    assert summary["absent_observed_sec"] == 20
    assert summary["unknown_gap_sec"] == 70
    assert summary["average_score"] == pytest.approx(81.43)
    assert summary["minimum_score"] == 50
    assert summary["state_seconds"] == {
        "NORMAL": 10, "CAUTION": 20, "DANGER": 5, "ABSENT": 20,
    }
    assert summary["major_reasons"] == [
        {"reason": "static_hold", "seconds": 20},
        {"reason": "low_blink", "seconds": 5},
    ]
    assert summary["break_feedback"] == {
        "supported": False, "occurred": None, "message": "현재 저장 데이터로 확인 불가",
    }
    assert report["timeseries"][4]["gap_before"] is True
    assert report["quality"]["long_gap_count"] == 1


def test_single_legacy_snapshot_has_no_invented_duration_or_average():
    report = build_session_report(USER_ID, SID, [
        snapshot(0, "CAUTION", 73, model=None, trigger="state_change"),
    ])

    assert report["period"]["start"] == report["period"]["end"]
    assert report["summary"]["snapshot_span_sec"] == 0
    assert report["summary"]["observed_coverage_sec"] == 0
    assert report["summary"]["seated_observed_sec"] == 0
    assert report["summary"]["average_score"] is None
    assert report["summary"]["minimum_score"] == 73
    assert report["quality"]["single_snapshot"] is True
    assert report["quality"]["legacy_unversioned"] is True
    assert report["quality"]["score_model_versions"] == []
    SESSION_REPORT_VALIDATOR.validate(report)


def test_last_snapshot_adds_no_tail_and_score_summary_excludes_absent():
    report = build_session_report(USER_ID, SID, [
        snapshot(0, "NORMAL", 80),
        snapshot(60, "ABSENT", 10),
        snapshot(120, "NORMAL", 20),
    ])

    assert report["summary"]["average_score"] == 80
    assert report["summary"]["minimum_score"] == 20
    assert report["summary"]["seated_observed_sec"] == 60
    assert report["summary"]["absent_observed_sec"] == 60


def test_absent_only_session_has_no_seated_average_or_minimum_score():
    report = build_session_report(USER_ID, SID, [
        snapshot(0, "ABSENT", 15, trigger="state_change"),
        snapshot(5, "ABSENT", 10),
    ])

    assert report["summary"]["average_score"] is None
    assert report["summary"]["minimum_score"] is None
    assert report["summary"]["seated_observed_sec"] == 0
    assert report["summary"]["absent_observed_sec"] == 5
    SESSION_REPORT_VALIDATOR.validate(report)


class FakeCursor:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.execution = None
        self.closed = False

    def execute(self, sql, params):
        self.execution = (sql, params)
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def test_reader_scopes_query_to_authenticated_user_and_session_and_closes_resources():
    row = (START, "state_change", "NORMAL", 100, [], "soma_load_v1")
    cursor = FakeCursor([row])
    connection = FakeConnection(cursor)
    reader = SessionReportReader(connection_factory=lambda: connection)

    assert reader.fetch(USER_ID, SID)[0]["score"] == 100
    assert cursor.execution == (SESSION_REPORT_SQL, (str(USER_ID), str(SID)))
    assert cursor.closed and connection.closed


class RecordingReportReader:
    def __init__(self, rows=None, error=None):
        self.rows = rows or [snapshot(0, "NORMAL", 100), snapshot(5, "NORMAL", 95)]
        self.error = error
        self.calls = []

    def fetch(self, user_id, session_id):
        self.calls.append((user_id, session_id))
        if self.error:
            raise self.error
        if user_id != USER_ID or session_id != SID:
            raise SessionReportNotFound("not found")
        return self.rows


def get(client, path, token=USER_TOKEN, **kwargs):
    return client.get(path, headers={"Authorization": "Bearer " + token}, **kwargs)


def test_report_endpoint_is_authenticated_owned_and_contract_valid():
    reader = RecordingReportReader()
    app, _ = make_app(session_report_reader=reader)
    path = f"/api/state/history/sessions/{SID}/report"

    assert app.test_client().get(path).status_code == 401
    assert not reader.calls
    response = get(app.test_client(), path)
    assert response.status_code == 200
    SESSION_REPORT_VALIDATOR.validate(response.get_json())
    assert reader.calls == [(USER_ID, SID)]
    assert get(app.test_client(), path, OTHER_TOKEN).status_code == 404
    assert get(app.test_client(), f"/api/state/history/sessions/{OTHER_SID}/report").status_code == 404


def test_report_endpoint_rejects_bad_uuid_queries_and_hides_db_errors():
    reader = RecordingReportReader()
    app, _ = make_app(session_report_reader=reader)
    assert get(app.test_client(), "/api/state/history/sessions/not-a-uuid/report").status_code == 400
    assert get(app.test_client(), f"/api/state/history/sessions/{SID}/report",
               query_string={"user_id": str(USER_ID)}).status_code == 400

    reader.error = SessionReportUnavailable("private database credentials")
    response = get(app.test_client(), f"/api/state/history/sessions/{SID}/report")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "history_unavailable"
    assert "private" not in response.get_data(as_text=True)
