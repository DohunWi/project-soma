"""State history period validation and database query tests."""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.state_history import (  # noqa: E402
    DEFAULT_HISTORY_WINDOW_SEC,
    HISTORY_SQL,
    MAX_HISTORY_WINDOW_SEC,
    HistoryRequestError,
    HistoryUnavailable,
    StateHistoryReader,
    resolve_history_period,
    utc_iso8601,
)

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
SESSION_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class FakeCursor:
    def __init__(self, rows=(), error=None):
        self.rows = rows
        self.error = error
        self.execution = None
        self.closed = False

    def execute(self, sql, params):
        self.execution = (sql, params)
        if self.error is not None:
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


def db_row(measured_at, state="NORMAL", *, is_baseline=False, null_metrics=False):
    metrics = (None,) * 7 if null_metrics else (
        "CENTER",
        3.0,
        0.0,
        8.0,
        250,
        12.5,
        55.0,
    )
    return (
        measured_at,
        state,
        90,
        0.45,
        ["static_hold"] if state == "CAUTION" else [],
        *metrics,
        is_baseline,
    )


def test_omitted_period_defaults_to_recent_five_minutes():
    now = datetime(2026, 9, 23, 3, 5, tzinfo=timezone.utc)

    start, end = resolve_history_period(None, None, now=now)

    assert end == now
    assert (end - start).total_seconds() == DEFAULT_HISTORY_WINDOW_SEC


def test_explicit_period_accepts_exactly_sixty_minutes_and_normalizes_utc():
    start, end = resolve_history_period(
        "2026-09-23T11:00:00+09:00",
        "2026-09-23T12:00:00+09:00",
    )

    assert (end - start).total_seconds() == MAX_HISTORY_WINDOW_SEC
    assert utc_iso8601(start) == "2026-09-23T02:00:00Z"
    assert utc_iso8601(end) == "2026-09-23T03:00:00Z"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-23T02:00:00Z", None),
        (None, "2026-09-23T03:00:00Z"),
        ("2026-09-23T02:00:00", "2026-09-23T03:00:00Z"),
        ("not-a-time", "2026-09-23T03:00:00Z"),
        ("2026-09-23T03:00:00Z", "2026-09-23T03:00:00Z"),
        ("2026-09-23T04:00:00Z", "2026-09-23T03:00:00Z"),
        ("2026-09-23T01:59:59Z", "2026-09-23T03:00:00Z"),
    ],
)
def test_invalid_period_is_rejected(start, end):
    with pytest.raises(HistoryRequestError):
        resolve_history_period(start, end)


def test_reader_separates_baseline_and_states_and_omits_null_metrics():
    start = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    cursor = FakeCursor([
        db_row(start - timedelta(seconds=1), is_baseline=True, null_metrics=True),
        db_row(start, state="CAUTION"),
        db_row(end, state="DANGER"),
    ])
    connection = FakeConnection(cursor)
    reader = StateHistoryReader(connection_factory=lambda: connection)

    baseline, states = reader.fetch(USER_ID, SESSION_ID, start, end)

    assert baseline["measured_at"] == "2026-09-23T02:59:59Z"
    assert "metrics" not in baseline
    assert [item["state"] for item in states] == ["CAUTION", "DANGER"]
    assert states[0]["measured_at"] == "2026-09-23T03:00:00Z"
    assert states[-1]["measured_at"] == "2026-09-23T03:05:00Z"
    assert states[0]["metrics"]["chair_distance_mm"] == 250
    assert cursor.closed is True
    assert connection.closed is True


def test_reader_query_uses_user_and_session_boundaries_for_range_and_baseline():
    start = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    cursor = FakeCursor()
    reader = StateHistoryReader(
        connection_factory=lambda: FakeConnection(cursor),
    )

    reader.fetch(USER_ID, SESSION_ID, start, end)

    sql, params = cursor.execution
    assert sql == HISTORY_SQL
    assert "created_at" not in sql
    assert sql.count("user_id = %s") == 2
    assert sql.count("session_id = %s") == 2
    assert "user_name" not in sql
    assert "device_id" not in sql
    assert "measured_at < %s" in sql
    assert "measured_at >= %s" in sql
    assert "measured_at <= %s" in sql
    assert params == (
        str(USER_ID),
        str(SESSION_ID),
        start,
        str(USER_ID),
        str(SESSION_ID),
        start,
        end,
    )


def test_reader_without_preceding_state_returns_no_baseline():
    start = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    cursor = FakeCursor([db_row(start)])
    reader = StateHistoryReader(
        connection_factory=lambda: FakeConnection(cursor),
    )

    baseline, states = reader.fetch(USER_ID, SESSION_ID, start, end)

    assert baseline is None
    assert len(states) == 1


def test_reader_without_db_credentials_is_unavailable(monkeypatch):
    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    reader = StateHistoryReader()
    start = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)

    with pytest.raises(HistoryUnavailable, match="state history query failed"):
        reader.fetch(USER_ID, SESSION_ID, start, start + timedelta(minutes=5))


def test_reader_closes_resources_and_hides_driver_error():
    cursor = FakeCursor(error=RuntimeError("password leaked in driver error"))
    connection = FakeConnection(cursor)
    reader = StateHistoryReader(connection_factory=lambda: connection)
    start = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)

    with pytest.raises(HistoryUnavailable, match="state history query failed") as caught:
        reader.fetch(USER_ID, SESSION_ID, start, start + timedelta(minutes=5))

    assert "password leaked" not in str(caught.value)
    assert cursor.closed is True
    assert connection.closed is True
