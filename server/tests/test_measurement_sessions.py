"""Single-chair in-memory measurement lifecycle tests."""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.measurement_sessions import (  # noqa: E402
    MeasurementInUse,
    MeasurementSessionRegistry,
    NoMeasurementSession,
)

USER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def registry():
    return MeasurementSessionRegistry(
        uuid_factory=lambda: SESSION_ID,
        now=Clock(),
    )


def test_start_creates_one_active_session_and_duplicate_is_idempotent():
    sessions = registry()
    first, created = sessions.start(USER_A)
    duplicate, duplicate_created = sessions.start(USER_A)

    assert created is True
    assert duplicate_created is False
    assert first == duplicate == sessions.active()


def test_single_chair_rejects_another_active_user():
    sessions = registry()
    sessions.start(USER_A)

    with pytest.raises(MeasurementInUse):
        sessions.start(USER_B)


def test_stop_and_duplicate_stop_are_idempotent():
    clock = Clock()
    sessions = MeasurementSessionRegistry(
        uuid_factory=lambda: SESSION_ID,
        now=clock,
    )
    sessions.start(USER_A)
    clock.value += timedelta(minutes=5)

    stopped, already = sessions.stop(USER_A)
    duplicate, duplicate_already = sessions.stop(USER_A)

    assert already is False
    assert duplicate_already is True
    assert stopped == duplicate
    assert stopped.status == "STOPPED"
    assert sessions.active() is None


def test_user_cannot_stop_another_users_active_session():
    sessions = registry()
    sessions.start(USER_A)

    with pytest.raises(NoMeasurementSession):
        sessions.stop(USER_B)


def test_new_registry_represents_server_restart_with_all_sessions_off():
    old = registry()
    old.start(USER_A)

    restarted = registry()

    assert restarted.active() is None
