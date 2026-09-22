"""Chair → server → fusion → state 실시간 경로 테스트."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import (  # noqa: E402
    STATE_VALIDATOR,
    ChairPipeline,
    PayloadError,
    create_app,
)
from server.config import DEMO_PROFILE, NORMAL_PROFILE  # noqa: E402
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
    app, socketio = create_app(testing=True)

    states = emitted_states(socketio, app, chair_payload())

    assert len(states) == 1
    assert states[0]["state"] == "NORMAL"


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
