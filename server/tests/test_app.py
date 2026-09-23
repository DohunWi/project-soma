"""Chair → server → fusion → state 실시간 경로 테스트."""
import sys
import uuid
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
from server.auth import AuthIdentity, AuthenticationError  # noqa: E402
from server.config import DEMO_PROFILE, NORMAL_PROFILE  # noqa: E402
from server.state_history import HistoryUnavailable  # noqa: E402
from tools.mock.stream import build, chair_sample  # noqa: E402

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
USER_TOKEN = "user-token"
OTHER_TOKEN = "other-token"
SENSOR_TOKEN = "sensor-token"


class FakeAuthVerifier:
    def verify(self, token):
        if token == USER_TOKEN:
            return AuthIdentity(USER_ID)
        if token == OTHER_TOKEN:
            return AuthIdentity(OTHER_USER_ID)
        raise AuthenticationError("invalid token")


def make_app(**kwargs):
    return create_app(
        testing=True,
        auth_verifier=FakeAuthVerifier(),
        sensor_auth_token=SENSOR_TOKEN,
        **kwargs,
    )


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


def emitted_states(socketio, app, payload, *, activate=True):
    if activate and app.extensions["measurement_sessions"].active() is None:
        app.extensions["measurement_sessions"].start(USER_ID)
        app.extensions["chair_pipeline"].reset()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})
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

    def fetch(self, user_id, session_id, start, end):
        self.calls.append((user_id, session_id, start, end))
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


def activate_history_session(app, user_id=USER_ID):
    session, created = app.extensions["measurement_sessions"].start(user_id)
    assert created is True
    return session


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
    app, socketio = make_app()

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
    app, _socketio = make_app(
        history_reader=reader,
        history_now=lambda: now,
    )
    session = activate_history_session(app)

    response = app.test_client().get(
        "/api/state/history",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["period"] == {
        "start": "2026-09-23T03:00:00Z",
        "end": "2026-09-23T03:05:00Z",
    }
    assert body["baseline"]["measured_at"] == "2026-09-23T02:59:59Z"
    assert len(body["states"]) == 1
    assert body["stream"] == {
        "user_id": str(USER_ID),
        "session_id": str(session.session_id),
    }
    assert STATE_HISTORY_VALIDATOR.is_valid(body)
    assert reader.calls[0][:2] == (USER_ID, session.session_id)


def test_history_endpoint_accepts_explicit_sixty_minute_period():
    reader = RecordingHistoryReader()
    app, _socketio = make_app(history_reader=reader)
    activate_history_session(app)

    response = app.test_client().get(
        "/api/state/history",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
        query_string={
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
        {"start": "2026-09-23"},
        {
            "start": "2026-09-23T01:59:59Z",
            "end": "2026-09-23T03:00:00Z",
        },
        {"user_name": "guest"},
        {"device_id": "chair-1"},
        {"user_id": str(OTHER_USER_ID)},
        {"session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
    ],
)
def test_history_endpoint_rejects_invalid_request(query_string):
    reader = RecordingHistoryReader()
    app, _socketio = make_app(history_reader=reader)
    activate_history_session(app)

    response = app.test_client().get(
        "/api/state/history",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
        query_string=query_string,
    )

    assert response.status_code == 400
    assert reader.calls == []


@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_history_endpoint_requires_valid_access_token(token):
    app, _socketio = make_app(history_reader=RecordingHistoryReader())
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    response = app.test_client().get("/api/state/history", headers=headers)

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"


def test_history_endpoint_rejects_user_without_active_session_without_leaking_owner():
    reader = RecordingHistoryReader()
    app, _socketio = make_app(history_reader=reader)
    activate_history_session(app, USER_ID)

    response = app.test_client().get(
        "/api/state/history",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "no_active_session"
    assert reader.calls == []


def test_history_endpoint_without_any_active_session_is_409():
    reader = RecordingHistoryReader()
    app, _socketio = make_app(history_reader=reader)

    response = app.test_client().get(
        "/api/state/history",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "no_active_session"
    assert reader.calls == []


def test_history_database_failure_is_503_and_realtime_still_emits():
    reader = RecordingHistoryReader(
        error=HistoryUnavailable("credentials rejected: secret"),
    )
    app, socketio = make_app(history_reader=reader)
    activate_history_session(app)

    response = app.test_client().get(
        "/api/state/history",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    states = emitted_states(socketio, app, chair_payload(), activate=False)

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
        def handle(self, _payload, _decision, **_identity):
            order.append("persistence")

        def end_session(self, _user_id, _session_id):
            pass

    app, socketio = make_app(
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
        def handle(self, _payload, _decision, **_identity):
            raise RuntimeError("database unavailable")

        def end_session(self, _user_id, _session_id):
            pass

    app, socketio = make_app(
        state_persistence=FailingPersistence(),
    )

    states = emitted_states(socketio, app, chair_payload())

    assert states[0]["state"] == "NORMAL"


def test_persistence_receives_only_verified_user_and_active_session_identity():
    calls = []

    class CapturingPersistence:
        def handle(self, _payload, _decision, **identity):
            calls.append(identity)

        def end_session(self, _user_id, _session_id):
            pass

    app, socketio = make_app(state_persistence=CapturingPersistence())
    app.extensions["measurement_sessions"].start(USER_ID)
    app.extensions["chair_pipeline"].reset()
    payload = chair_payload()
    payload["user_id"] = str(OTHER_USER_ID)

    emitted_states(socketio, app, payload, activate=False)

    active = app.extensions["measurement_sessions"].active()
    assert calls == [{"user_id": USER_ID, "session_id": active.session_id}]


def test_missing_db_credentials_disable_persistence(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    app, _socketio = create_app(
        testing=False,
        auth_verifier=FakeAuthVerifier(),
        sensor_auth_token=SENSOR_TOKEN,
    )

    assert app.extensions["state_persistence"] is None
    assert app.extensions["db_writer"] is None


def test_chair_only_demo_reaches_caution_with_low_confidence():
    app, _socketio = make_app(runtime_profile=DEMO_PROFILE)
    pipeline = app.extensions["chair_pipeline"]

    decision = None
    for elapsed in range(11):
        decision = pipeline.process(chair_payload(t=1000.0 + elapsed))

    assert decision["state"] == "CAUTION"
    assert decision["confidence"] == 0.45
    assert STATE_VALIDATOR.is_valid(decision)


def test_socketio_does_not_emit_state_for_invalid_payload():
    app, socketio = make_app()

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
    app, socketio = make_app(runtime_profile=NORMAL_PROFILE)
    pipeline = app.extensions["chair_pipeline"]
    app.extensions["measurement_sessions"].start(USER_ID)
    pipeline.reset()
    payload = chair_payload(t=1000.0, pressure=pressure, user=expected)

    for offset in range(seconds):
        payload["t"] = 1000.0 + offset
        pipeline.process(payload)
    payload["t"] = 1000.0 + seconds

    states = emitted_states(socketio, app, payload, activate=False)

    assert states[-1]["state"] == expected


def test_measurement_off_validates_but_does_not_run_fusion_or_emit():
    app, socketio = make_app()
    pipeline = app.extensions["chair_pipeline"]
    validations = []
    fusion_calls = []
    original_validate = pipeline.validate
    original_process = pipeline.process_validated

    def recording_validate(payload):
        validations.append(payload)
        return original_validate(payload)

    def recording_process(payload):
        fusion_calls.append(payload)
        return original_process(payload)

    pipeline.validate = recording_validate
    pipeline.process_validated = recording_process
    payload = chair_payload()
    states = emitted_states(socketio, app, payload, activate=False)

    assert states == []
    assert validations == [payload]
    assert fusion_calls == []


def test_start_stop_are_authenticated_and_idempotent():
    app, _socketio = make_app()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {USER_TOKEN}"}

    first = client.post("/api/measurement/start", headers=headers)
    duplicate = client.post("/api/measurement/start", headers=headers)
    stopped = client.post("/api/measurement/stop", headers=headers)
    stopped_again = client.post("/api/measurement/stop", headers=headers)

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.get_json()["measurement"]["session_id"] == first.get_json()[
        "measurement"
    ]["session_id"]
    assert stopped.status_code == 200
    assert stopped_again.status_code == 200
    assert stopped_again.get_json()["already_stopped"] is True


def test_start_uses_verified_subject_and_rejects_second_user():
    app, _socketio = make_app()
    client = app.test_client()

    first = client.post(
        "/api/measurement/start",
        json={"user_id": str(OTHER_USER_ID)},
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    conflict = client.post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )

    assert first.get_json()["measurement"]["user_id"] == str(USER_ID)
    assert conflict.status_code == 409


def test_measurement_endpoints_reject_missing_token():
    app, _socketio = make_app()

    response = app.test_client().post("/api/measurement/start")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"


def test_state_is_emitted_only_to_authenticated_active_user_room():
    app, socketio = make_app()
    app.extensions["measurement_sessions"].start(USER_ID)
    app.extensions["chair_pipeline"].reset()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    active_user = socketio.test_client(app, auth={"token": USER_TOKEN})
    other_user = socketio.test_client(app, auth={"token": OTHER_TOKEN})

    producer.emit("sensor_data", chair_payload())

    assert [e for e in active_user.get_received() if e["name"] == "state"]
    assert [e for e in other_user.get_received() if e["name"] == "state"] == []


def test_new_session_resets_accumulated_fusion_state():
    app, socketio = make_app(runtime_profile=DEMO_PROFILE)
    http = app.test_client()
    headers = {"Authorization": f"Bearer {USER_TOKEN}"}
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})
    http.post("/api/measurement/start", headers=headers)

    for elapsed in range(11):
        producer.emit(
            "sensor_data",
            chair_payload(t=1000.0 + elapsed, pressure=[850, 850, 850, 850]),
        )
    first_states = [
        event["args"][0]
        for event in front.get_received()
        if event["name"] == "state"
    ]
    assert first_states[-1]["state"] == "CAUTION"

    http.post("/api/measurement/stop", headers=headers)
    restarted = http.post("/api/measurement/start", headers=headers)
    producer.emit(
        "sensor_data",
        chair_payload(t=2000.0, pressure=[850, 850, 850, 850]),
    )
    restarted_states = [
        event["args"][0]
        for event in front.get_received()
        if event["name"] == "state"
    ]

    assert restarted.status_code == 201
    assert restarted_states[-1]["state"] == "NORMAL"
    assert restarted_states[-1]["metrics"]["static_hold_sec"] == 0.0
    assert restarted_states[-1]["metrics"]["session_sec"] == 0.0


def test_user_socket_cannot_submit_sensor_data():
    app, socketio = make_app()
    app.extensions["measurement_sessions"].start(USER_ID)
    user = socketio.test_client(app, auth={"token": USER_TOKEN})

    user.emit("sensor_data", chair_payload())

    assert [e for e in user.get_received() if e["name"] == "state"] == []
