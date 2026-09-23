"""Chair → server → fusion → state 실시간 경로 테스트."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import (  # noqa: E402
    STATE_VALIDATOR,
    STATE_HISTORY_VALIDATOR,
    ChairPipeline,
    PayloadError,
    create_app,
)
from server.config import DEMO_PROFILE, NORMAL_PROFILE  # noqa: E402
from server.state_history import HistoryUnavailable  # noqa: E402
from tools.mock.stream import build, chair_sample  # noqa: E402


def chair_payload(*, t=1000.0, pressure=None, user="test", device="chair-test"):
    return {
        "v": 1,
        "t": t,
        "source": "chair",
        "device_id": device,
        "user_name": user,
        "chair": {
            "pressure": pressure or [900, 700, 1000, 910],
            "ir": [250],
        },
    }


def emitted_states(socketio, app, payload):
    producer = socketio.test_client(app)
    front = socketio.test_client(app)
    producer.emit("sensor_data", payload)
    events = [
        event["args"][0]
        for event in front.get_received()
        if event["name"] == "state"
    ]
    producer.disconnect()
    front.disconnect()
    return events


class RecordingHistoryReader:
    def __init__(self, *, baseline=None, states=None, error=None):
        self.baseline = baseline
        self.states = states or []
        self.error = error
        self.calls = []

    def fetch(self, user_name, device_id, start, end):
        self.calls.append((user_name, device_id, start, end))
        if self.error is not None:
            raise self.error
        return self.baseline, self.states


def history_snapshot(measured_at="2026-09-23T02:59:59Z"):
    return {
        "measured_at": measured_at,
        "state": "NORMAL",
        "score": 90,
        "confidence": 0.45,
        "reasons": [],
        "metrics": {"balance": "CENTER", "static_hold_sec": 3.0},
    }


def test_normal_chair_payload_without_vision_creates_state():
    decision = ChairPipeline().process(chair_payload())

    assert decision["state"] == "NORMAL"
    assert decision["user_name"] == "test"
    assert decision["metrics"]["balance"] == "LEFT"
    assert STATE_VALIDATOR.is_valid(decision)
    assert "blink_rate" not in decision["metrics"]
    assert "face_distance_cm" not in decision["metrics"]


def test_absent_chair_payload_creates_absent_state():
    decision = ChairPipeline().process(chair_payload(pressure=[1, 1, 1, 1]))

    assert decision["state"] == "ABSENT"
    assert decision["metrics"]["seated"] is False


def test_pressure_must_have_exactly_four_values():
    with pytest.raises(PayloadError, match="sensor_data 계약 위반"):
        ChairPipeline().process(chair_payload(pressure=[1, 2, 3]))


def test_required_field_must_be_present():
    payload = chair_payload()
    del payload["t"]

    with pytest.raises(PayloadError, match="'t' is a required property"):
        ChairPipeline().process(payload)


def test_socketio_emits_state_without_supabase(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    app, socketio = create_app(testing=True)

    states = emitted_states(socketio, app, chair_payload())

    assert len(states) == 1
    assert states[0]["state"] == "NORMAL"
    assert app.extensions["state_persistence"] is None


def test_history_endpoint_defaults_to_recent_five_minutes():
    now = datetime(2026, 9, 23, 3, 5, tzinfo=timezone.utc)
    reader = RecordingHistoryReader(
        baseline=history_snapshot(),
        states=[history_snapshot("2026-09-23T03:00:00Z")],
    )
    app, _socketio = create_app(
        testing=True,
        history_reader=reader,
        history_now=lambda: now,
    )

    response = app.test_client().get(
        "/api/state/history?user_name=guest&device_id=chair-1"
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["period"] == {
        "start": "2026-09-23T03:00:00Z",
        "end": "2026-09-23T03:05:00Z",
    }
    assert body["baseline"]["measured_at"] == "2026-09-23T02:59:59Z"
    assert len(body["states"]) == 1
    assert STATE_HISTORY_VALIDATOR.is_valid(body)
    assert reader.calls[0][:2] == ("guest", "chair-1")


def test_history_endpoint_accepts_explicit_sixty_minute_period():
    reader = RecordingHistoryReader()
    app, _socketio = create_app(testing=True, history_reader=reader)

    response = app.test_client().get(
        "/api/state/history",
        query_string={
            "user_name": "guest",
            "device_id": "chair-1",
            "start": "2026-09-23T02:00:00Z",
            "end": "2026-09-23T03:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["states"] == []
    assert "baseline" not in response.get_json()


@pytest.mark.parametrize(
    "query_string",
    [
        {},
        {"user_name": "guest", "device_id": "chair-1", "start": "2026-09-23"},
        {
            "user_name": "guest",
            "device_id": "chair-1",
            "start": "2026-09-23T01:59:59Z",
            "end": "2026-09-23T03:00:00Z",
        },
    ],
)
def test_history_endpoint_rejects_invalid_request(query_string):
    app, _socketio = create_app(
        testing=True,
        history_reader=RecordingHistoryReader(),
    )

    response = app.test_client().get(
        "/api/state/history",
        query_string=query_string,
    )

    assert response.status_code == 400


def test_history_database_failure_is_503_and_realtime_still_emits():
    reader = RecordingHistoryReader(
        error=HistoryUnavailable("credentials rejected: secret"),
    )
    app, socketio = create_app(testing=True, history_reader=reader)

    response = app.test_client().get(
        "/api/state/history?user_name=guest&device_id=chair-1"
    )
    states = emitted_states(socketio, app, chair_payload())

    assert response.status_code == 503
    assert response.get_json() == {
        "v": 1,
        "status": "error",
        "error": {
            "code": "history_unavailable",
            "message": "최근 상태 기록을 불러올 수 없습니다.",
        },
    }
    assert "secret" not in response.get_data(as_text=True)
    assert states[-1]["state"] == "NORMAL"


def test_state_emit_happens_before_persistence_enqueue():
    order = []

    class RecordingPersistence:
        def handle(self, _payload, _decision):
            order.append("persistence")

    app, socketio = create_app(
        testing=True,
        state_persistence=RecordingPersistence(),
    )
    original_emit = socketio.emit

    def recording_emit(*args, **kwargs):
        order.append("emit")
        return original_emit(*args, **kwargs)

    socketio.emit = recording_emit
    states = emitted_states(socketio, app, chair_payload())

    assert states[0]["state"] == "NORMAL"
    assert order == ["emit", "persistence"]


def test_persistence_failure_does_not_prevent_state_emit():
    class FailingPersistence:
        def handle(self, _payload, _decision):
            raise RuntimeError("database unavailable")

    app, socketio = create_app(
        testing=True,
        state_persistence=FailingPersistence(),
    )

    states = emitted_states(socketio, app, chair_payload())

    assert states[0]["state"] == "NORMAL"


def test_missing_db_credentials_disable_persistence(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    app, _socketio = create_app(testing=False)

    assert app.extensions["state_persistence"] is None
    assert app.extensions["db_writer"] is None


def test_chair_only_demo_reaches_caution_with_low_confidence():
    app, _socketio = create_app(testing=True, runtime_profile=DEMO_PROFILE)
    pipeline = app.extensions["chair_pipeline"]

    decision = None
    for elapsed in range(11):
        decision = pipeline.process(chair_payload(t=1000.0 + elapsed))

    assert decision["state"] == "CAUTION"
    assert decision["confidence"] == 0.45
    assert STATE_VALIDATOR.is_valid(decision)


def test_socketio_does_not_emit_state_for_invalid_payload():
    app, socketio = create_app(testing=True)

    states = emitted_states(socketio, app, chair_payload(pressure=[1, 2, 3]))

    assert states == []


def test_chair_only_mock_payload_matches_server_contract():
    payload = build("chair", 1000.0, 0.0, "normal", "mock_user")

    decision = ChairPipeline().process(payload)

    assert payload["source"] == "chair"
    assert "vision" not in payload
    assert decision["state"] == "NORMAL"


def test_absent_mock_remains_absent_after_sixty_seconds():
    assert sum(chair_sample(0, 61, "absent")["pressure"]) < 100
    assert sum(chair_sample(0, 180, "absent")["pressure"]) < 100
    assert sum(chair_sample(0, 3600, "absent")["pressure"]) < 100


@pytest.mark.parametrize(
    ("pressure", "seconds", "expected"),
    [
        ([1000, 700, 1000, 700], 300, "CAUTION"),
        ([850, 850, 850, 850], 2700, "DANGER"),
    ],
)
def test_accumulated_chair_state_can_be_emitted(pressure, seconds, expected):
    app, socketio = create_app(testing=True, runtime_profile=NORMAL_PROFILE)
    pipeline = app.extensions["chair_pipeline"]
    payload = chair_payload(t=1000.0, pressure=pressure, user=expected)

    for offset in range(seconds):
        payload["t"] = 1000.0 + offset
        pipeline.process(payload)
    payload["t"] = 1000.0 + seconds

    states = emitted_states(socketio, app, payload)

    assert states[-1]["state"] == expected
