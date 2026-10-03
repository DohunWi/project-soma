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
from server.auth import (  # noqa: E402
    AuthIdentity,
    AuthenticationError,
    AuthenticationUnavailable,
)
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
        measurement, _created = app.extensions["measurement_sessions"].start(USER_ID)
        app.extensions["chair_pipeline"].reset()
        app.extensions["feedback_coordinator"].start_session(
            measurement.session_id
        )
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
        order.append(args[0])
        return original_emit(*args, **kwargs)

    socketio.emit = recording_emit
    states = emitted_states(socketio, app, chair_payload())

    assert states[0]["state"] == "NORMAL"
    assert order[:4] == ["state", "feedback", "feedback_device", "persistence"]
    assert order[4:] == ["feedback_device_off"]


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
    measurement, _created = app.extensions["measurement_sessions"].start(USER_ID)
    app.extensions["chair_pipeline"].reset()
    payload = chair_payload()
    payload["user_id"] = str(OTHER_USER_ID)

    emitted_states(socketio, app, payload, activate=False)

    assert calls == [{"user_id": USER_ID, "session_id": measurement.session_id}]
    assert app.extensions["measurement_sessions"].active() is None


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
    app, socketio = make_app()
    client = app.test_client()
    socketio.test_client(app, auth={"token": USER_TOKEN})

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


def test_different_authenticated_user_can_replace_orphaned_session():
    app, _socketio = make_app()
    client = app.test_client()
    first = client.post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )

    replacement = client.post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )

    assert first.status_code == 201
    assert replacement.status_code == 201
    assert replacement.get_json()["created"] is True
    assert replacement.get_json()["measurement"]["user_id"] == str(OTHER_USER_ID)
    assert replacement.get_json()["measurement"]["session_id"] != first.get_json()[
        "measurement"
    ]["session_id"]


def test_measurement_endpoints_reject_missing_token():
    app, _socketio = make_app()

    response = app.test_client().post("/api/measurement/start")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthenticationError("invalid"), "invalid_user_token"),
        (AuthenticationUnavailable("offline"), "auth_unavailable"),
    ],
)
def test_socket_user_rejection_logs_safe_reason_category(
    error,
    category,
    caplog,
):
    class RejectingVerifier:
        def verify(self, _token):
            raise error

    app, socketio = create_app(
        testing=True,
        auth_verifier=RejectingVerifier(),
        sensor_auth_token=SENSOR_TOKEN,
    )

    with caplog.at_level("WARNING", logger="soma.server"):
        rejected = socketio.test_client(
            app,
            auth={"token": "credential-must-not-be-logged", "observe_sensor_data": True},
        )

    assert rejected.is_connected() is False
    assert category in caplog.text
    assert "observer_opt_in=True" in caplog.text
    assert "credential-must-not-be-logged" not in caplog.text


def test_socket_acceptance_logs_role_and_observer_without_token(caplog):
    app, socketio = make_app()

    with caplog.at_level("INFO", logger="soma.server"):
        observer = socketio.test_client(
            app,
            auth={"token": USER_TOKEN, "observe_sensor_data": True},
        )

    assert observer.is_connected() is True
    assert "role=user" in caplog.text
    assert "observer_opt_in=True" in caplog.text
    assert USER_TOKEN not in caplog.text
    observer.disconnect()


def test_invalid_stop_token_cannot_stop_but_last_owner_disconnect_releases_session():
    app, socketio = make_app()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    other_user = socketio.test_client(app, auth={"token": OTHER_TOKEN})
    http = app.test_client()
    started = http.post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )

    rejected = http.post(
        "/api/measurement/stop",
        headers={"Authorization": "Bearer expired-token"},
    )
    assert started.status_code == 201
    assert rejected.status_code == 401
    assert app.extensions["measurement_sessions"].active().user_id == USER_ID

    owner.disconnect()
    assert app.extensions["measurement_sessions"].active() is None

    restarted = http.post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )
    producer.emit("sensor_data", chair_payload(t=2000.0))
    other_states = [
        event for event in other_user.get_received() if event["name"] == "state"
    ]
    assert restarted.status_code == 201
    assert restarted.get_json()["measurement"]["user_id"] == str(OTHER_USER_ID)
    assert len(other_states) == 1


def test_measurement_remains_active_until_last_owner_socket_disconnects():
    app, socketio = make_app()
    first = socketio.test_client(app, auth={"token": USER_TOKEN})
    second = socketio.test_client(app, auth={"token": USER_TOKEN})
    response = app.test_client().post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    assert response.status_code == 201

    first.disconnect()
    assert app.extensions["measurement_sessions"].active() is not None

    second.disconnect()
    assert app.extensions["measurement_sessions"].active() is None


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


def test_cross_validation_observation_is_opt_in_and_owner_scoped():
    receipt_times = iter((10.0, 10.5, 10.5))
    app, socketio = make_app(sensor_monotonic=lambda: next(receipt_times))
    app.extensions["measurement_sessions"].start(USER_ID)
    app.extensions["chair_pipeline"].reset()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    ordinary_user = socketio.test_client(app, auth={"token": USER_TOKEN})
    observer = socketio.test_client(
        app,
        auth={"token": USER_TOKEN, "observe_sensor_data": True},
    )
    other_observer = socketio.test_client(
        app,
        auth={"token": OTHER_TOKEN, "observe_sensor_data": True},
    )

    producer.emit(
        "sensor_data",
        {
            "v": 1,
            "t": 999.9,
            "source": "vision",
            "user_name": "test",
            "vision": {
                "face_detected": True,
                "detect_rate": 1.0,
                "yaw_dropped_rate": 0.0,
                "face_lateral_offset": 0.25,
                "head_roll_delta_deg": 1.0,
                "face_lean_direction": "LEFT",
            },
        },
    )
    producer.emit("sensor_data", chair_payload(t=1000.0))

    ordinary_events = ordinary_user.get_received()
    observed = [
        event["args"][0]
        for event in observer.get_received()
        if event["name"] == "cross_validation_observation"
    ]
    other_events = other_observer.get_received()
    assert [
        event for event in ordinary_events
        if event["name"] == "cross_validation_observation"
    ] == []
    assert [
        event for event in other_events
        if event["name"] == "cross_validation_observation"
    ] == []
    assert len(observed) == 1
    assert observed[0]["chair"]["chair"]["pressure"] == [900, 700, 1000, 910]
    assert observed[0]["vision"]["vision"]["face_lean_direction"] == "LEFT"
    assert observed[0]["vision_availability"] == "FRESH"
    assert observed[0]["vision_receipt_age_sec"] == pytest.approx(0.5)
    assert observed[0]["chair_vision_sender_delta_sec"] == pytest.approx(0.1)
    assert observed[0]["state"]["metrics"]["balance"] == "LEFT"


def test_real_chair_identity_fields_do_not_block_authenticated_session_routing():
    app, socketio = make_app()
    producer = socketio.test_client(
        app,
        auth={"token": SENSOR_TOKEN, "role": "sensor"},
    )
    observer = socketio.test_client(app, auth={"token": USER_TOKEN})
    other_user = socketio.test_client(app, auth={"token": OTHER_TOKEN})
    started = app.test_client().post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )

    producer.emit(
        "sensor_data",
        chair_payload(
            t=1790741123.079,
            pressure=[973, 957, 999, 943],
            user="guest",
            device="smart_chair_01",
        ),
    )

    observer_events = observer.get_received()
    state_events = [event for event in observer_events if event["name"] == "state"]
    feedback_events = [
        event for event in observer_events if event["name"] == "feedback"
    ]
    assert started.status_code == 201
    assert len(state_events) == 1
    assert state_events[0]["args"][0]["user_name"] == "guest"
    assert len(feedback_events) == 1
    assert [
        event for event in other_user.get_received() if event["name"] == "state"
    ] == []


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
    assert first_states[-1]["score"] == 95

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
    assert restarted_states[-1]["score"] == 100
    assert restarted_states[-1]["metrics"]["static_hold_sec"] == 0.0
    assert restarted_states[-1]["metrics"]["session_sec"] == 0.0


def test_user_socket_cannot_submit_sensor_data():
    app, socketio = make_app()
    app.extensions["measurement_sessions"].start(USER_ID)
    user = socketio.test_client(app, auth={"token": USER_TOKEN})

    user.emit("sensor_data", chair_payload())

    assert [e for e in user.get_received() if e["name"] == "state"] == []
