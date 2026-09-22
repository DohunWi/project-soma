"""Asynchronous state_logs DBWriter tests with an in-memory fake connection."""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.db_writer import DBWriter  # noqa: E402


class FakeDatabase:
    def __init__(self, failures=0):
        self.failures = failures
        self.executions = []
        self.commits = 0

    def connect(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database):
        self.database = database
        self.closed = 0

    def cursor(self):
        return FakeCursor(self.database)

    def commit(self):
        self.database.commits += 1

    def close(self):
        self.closed = 1


class FakeCursor:
    def __init__(self, database):
        self.database = database

    def execute(self, sql, params):
        self.database.executions.append((sql, params))
        if self.database.failures:
            self.database.failures -= 1
            raise RuntimeError("transient database failure")

    def close(self):
        pass


def state_log_row(event_id="event-1"):
    return {
        "event_id": event_id,
        "user_id": None,
        "user_name": "guest",
        "device_id": "chair-1",
        "measured_at": datetime.fromtimestamp(1000.0, timezone.utc),
        "trigger": "state_change",
        "state": "NORMAL",
        "score": 90,
        "confidence": 0.45,
        "reasons": [],
        "balance": "CENTER",
        "static_hold_sec": 0.0,
        "low_blink_sec": 0.0,
        "session_sec": 0.0,
        "chair_distance_mm": 250,
        "blink_rate": None,
        "face_distance_cm": None,
    }


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_successful_state_log_insert_runs_in_worker():
    database = FakeDatabase()
    writer = DBWriter(connection_factory=database.connect, sleep=lambda _delay: None)
    writer.start()

    assert writer.put_state_log(state_log_row()) is True
    assert wait_until(lambda: writer.written == 1)
    writer.stop()

    sql, params = database.executions[0]
    assert "INSERT INTO state_logs" in sql
    assert "ON CONFLICT (event_id) DO NOTHING" in sql
    assert params[0] == "event-1"
    assert database.commits == 1


def test_transient_failure_retries_same_event_id():
    database = FakeDatabase(failures=1)
    sleeps = []
    writer = DBWriter(
        connection_factory=database.connect,
        sleep=sleeps.append,
    )
    writer.start()

    assert writer.put_state_log(state_log_row("stable-event")) is True
    assert wait_until(lambda: writer.written == 1)
    writer.stop()

    attempted_ids = [params[0] for _sql, params in database.executions]
    assert attempted_ids == ["stable-event", "stable-event"]
    assert sleeps == [0.5]


def test_worker_survives_retry_limit_and_processes_next_item():
    database = FakeDatabase(failures=3)
    writer = DBWriter(connection_factory=database.connect, sleep=lambda _delay: None)
    writer.start()

    assert writer.put_state_log(state_log_row("will-fail")) is True
    assert writer.put_state_log(state_log_row("will-succeed")) is True
    assert wait_until(lambda: writer.failed == 1 and writer.written == 1)
    assert writer._t.is_alive()
    writer.stop()

    attempted_ids = [params[0] for _sql, params in database.executions]
    assert attempted_ids == [
        "will-fail",
        "will-fail",
        "will-fail",
        "will-succeed",
    ]


def test_queue_full_does_not_block_or_raise_in_producer():
    writer = DBWriter(queue_max=1)
    assert writer.put_state_log(state_log_row("queued")) is True

    started = time.monotonic()
    accepted = writer.put_state_log(state_log_row("dropped"))
    elapsed = time.monotonic() - started

    assert accepted is False
    assert elapsed < 0.1
    assert writer.dropped == 1


def test_connection_exception_stays_in_worker_thread():
    def broken_connection():
        raise RuntimeError("credentials rejected")

    writer = DBWriter(
        connection_factory=broken_connection,
        sleep=lambda _delay: None,
    )
    writer.start()

    assert writer.put_state_log(state_log_row()) is True
    assert wait_until(lambda: writer.failed == 1)
    assert writer._t.is_alive()
    writer.stop()
