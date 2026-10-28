"""Backend routing tests for the optional Feedback Nano device room."""
import uuid

from server.app import create_app
from server.auth import AuthIdentity, AuthenticationError


USER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
USER_A_TOKEN = "user-a-token"
USER_B_TOKEN = "user-b-token"
DEVICE_TOKEN = "device-token"


class FakeAuthVerifier:
    def verify(self, token):
        if token == USER_A_TOKEN:
            return AuthIdentity(USER_A)
        if token == USER_B_TOKEN:
            return AuthIdentity(USER_B)
        raise AuthenticationError("invalid token")


class RecordingPersistence:
    def __init__(self):
        self.calls = []

    def handle(self, payload, decision, **identity):
        self.calls.append((payload, decision, identity))

    def end_session(self, _user_id, _session_id):
        pass


def make_app(**kwargs):
    return create_app(
        testing=True,
        auth_verifier=FakeAuthVerifier(),
        sensor_auth_token=DEVICE_TOKEN,
        _skip_calibration_for_testing=True,
        **kwargs,
    )


def chair_payload(t=1000.0, pressure=None):
    return {
        "v": 1,
        "t": t,
        "source": "chair",
        "user_name": "device-test",
        "device_id": "chair-test",
        "chair": {"pressure": pressure or [850, 850, 850, 850], "ir": [250]},
    }


def named_events(client, name):
    return [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == name
    ]


def start(app):
    return app.test_client().post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_A_TOKEN}"},
    )


def stop(app):
    return app.test_client().post(
        "/api/measurement/stop",
        headers={"Authorization": f"Bearer {USER_A_TOKEN}"},
    )


def test_feedback_device_auth_is_role_scoped_and_initially_syncs_off():
    app, socketio = make_app()
    device = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    sensor = socketio.test_client(app, auth={"token": DEVICE_TOKEN})
    invalid = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "unknown"},
    )
    user_with_device_role = socketio.test_client(
        app,
        auth={"token": USER_A_TOKEN, "role": "feedback_device"},
    )

    assert device.is_connected()
    assert named_events(device, "feedback_device_off") == [{"v": 1}]
    assert sensor.is_connected()
    assert named_events(sensor, "feedback_device_off") == []
    assert invalid.is_connected() is False
    assert user_with_device_role.is_connected()
    assert named_events(user_with_device_role, "feedback_device_off") == []


def test_device_event_is_separate_from_user_and_sensor_rooms_and_stop_is_off():
    app, socketio = make_app()
    device = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    producer = socketio.test_client(app, auth={"token": DEVICE_TOKEN})
    user = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    other_user = socketio.test_client(app, auth={"token": USER_B_TOKEN})
    device.get_received()

    start(app)
    assert named_events(device, "feedback_device_off") == [{"v": 1}]
    producer.emit("sensor_data", chair_payload())

    device_decisions = named_events(device, "feedback_device")
    assert len(device_decisions) == 1
    assert device_decisions[0]["level"] == "NORMAL"
    assert named_events(producer, "feedback_device") == []
    assert named_events(user, "feedback_device") == []
    assert named_events(other_user, "feedback_device") == []

    stop(app)
    assert named_events(device, "feedback_device_off") == [{"v": 1}]


def test_new_start_and_device_reconnect_do_not_replay_old_alert():
    app, socketio = make_app()
    producer = socketio.test_client(app, auth={"token": DEVICE_TOKEN})
    first_device = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    first_device.get_received()
    start(app)
    first_device.get_received()

    for elapsed in range(30):
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))
    decisions = named_events(first_device, "feedback_device")
    assert decisions[-1]["level"] == "BREAK"
    assert any(
        decision["level"] == "BREAK" and decision["transition"]
        for decision in decisions
    )

    active_reconnect = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    synchronized = named_events(active_reconnect, "feedback_device")
    assert synchronized[-1]["level"] == "BREAK"
    assert synchronized[-1]["transition"] is False

    stop(app)
    start(app)
    reconnect = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    received = reconnect.get_received()
    assert [event for event in received if event["name"] == "feedback_device"] == []
    assert [event for event in received if event["name"] == "feedback_device_off"]


def test_break_display_holds_three_seconds_then_nano_tracks_fusion_without_new_alert(
    monkeypatch,
):
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()

    def deterministic_fusion(state, sample, now, **_kwargs):
        marker = sample["pressure"][0]
        fusion_state = {
            850: "DANGER",
            851: "CAUTION",
            852: "NORMAL",
            0: "ABSENT",
        }[marker]
        return state, {
            "v": 1,
            "t": now,
            "user_name": sample["user_name"],
            "state": fusion_state,
            "confidence": 1.0,
            "score": 50 if fusion_state == "DANGER" else 80,
            "reasons": [],
            "metrics": {
                "balance": "CENTER",
                "seated": fusion_state != "ABSENT",
                "static_hold_sec": 0.0,
                "session_sec": 0.0,
            },
        }

    monkeypatch.setattr("server.app.step", deterministic_fusion)
    app, socketio = make_app(feedback_device_monotonic=clock)
    producer = socketio.test_client(app, auth={"token": DEVICE_TOKEN})
    user = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    device = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    device.get_received()
    start(app)
    device.get_received()

    for elapsed in range(11):
        clock.value = float(elapsed)
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))
    logical = named_events(user, "feedback")
    physical = named_events(device, "feedback_device")
    assert logical[-1]["level"] == physical[-1]["level"] == "BREAK"
    assert logical[-1]["transition"] is physical[-1]["transition"] is True

    clock.value = 12.9
    producer.emit("sensor_data", chair_payload(1011.0, pressure=[851, 850, 850, 850]))
    assert named_events(user, "feedback")[-1]["level"] == "BREAK"
    held = named_events(device, "feedback_device")[-1]
    assert held["level"] == "BREAK"
    assert held["transition"] is False

    clock.value = 13.0
    producer.emit("sensor_data", chair_payload(1012.0, pressure=[851, 850, 850, 850]))
    assert named_events(user, "feedback")[-1]["level"] == "BREAK"
    notice = named_events(device, "feedback_device")[-1]
    assert notice["level"] == "NOTICE"
    assert notice["reason"] == "FUSION_CAUTION"
    assert notice["transition"] is False

    clock.value = 14.0
    producer.emit("sensor_data", chair_payload(1013.0, pressure=[850, 850, 850, 850]))
    warning = named_events(device, "feedback_device")[-1]
    assert warning["level"] == "WARNING"
    assert warning["transition"] is False

    clock.value = 15.0
    producer.emit("sensor_data", chair_payload(1014.0, pressure=[852, 850, 850, 850]))
    normal = named_events(device, "feedback_device")[-1]
    assert normal["level"] == "NORMAL"
    assert normal["transition"] is False

    reconnect = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    synchronized = named_events(reconnect, "feedback_device")[-1]
    assert synchronized["level"] == "NORMAL"
    assert synchronized["transition"] is False

    clock.value = 15.1
    producer.emit("sensor_data", chair_payload(1015.0, pressure=[0, 0, 0, 0]))
    assert named_events(device, "feedback_device_off") == [{"v": 1}]
    assert app.extensions["latest_device_feedback"]["decision"] is None


def test_device_emit_failure_does_not_block_user_state_feedback_or_persistence():
    persistence = RecordingPersistence()
    app, socketio = make_app(state_persistence=persistence)
    producer = socketio.test_client(app, auth={"token": DEVICE_TOKEN})
    user = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    start(app)
    original_emit = socketio.emit

    def fail_device(name, *args, **kwargs):
        if name == "feedback_device":
            raise RuntimeError("device transport unavailable")
        return original_emit(name, *args, **kwargs)

    socketio.emit = fail_device
    producer.emit("sensor_data", chair_payload())
    received = user.get_received()

    assert [event for event in received if event["name"] == "state"]
    assert [event for event in received if event["name"] == "feedback"]
    assert len(persistence.calls) == 1


def test_break_is_physically_off_and_reseat_starts_normal_without_old_alert():
    app, socketio = make_app()
    producer = socketio.test_client(app, auth={"token": DEVICE_TOKEN})
    device = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    device.get_received()
    start(app)
    device.get_received()
    for elapsed in range(30):
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))
    assert named_events(device, "feedback_device")[-1]["level"] == "BREAK"

    for elapsed in range(1, 6):
        absent = chair_payload(1029.0 + elapsed)
        absent["chair"]["pressure"] = [0, 0, 0, 0]
        producer.emit("sensor_data", absent)

    assert named_events(device, "feedback_device_off") == [{"v": 1}]
    assert app.extensions["latest_device_feedback"]["decision"] is None

    producer.emit("sensor_data", chair_payload(1035.0))
    resumed = named_events(device, "feedback_device")[-1]
    assert resumed["level"] == "NORMAL"
    assert resumed["transition"] is False
    active = app.extensions["measurement_sessions"].active()
    policy = app.extensions["feedback_coordinator"].state_for(active.session_id)
    assert policy.break_active is False
    assert policy.work_period_sec == 1.0


def test_authenticated_demo_is_hardware_only_and_blocked_during_measurement():
    persistence = RecordingPersistence()
    app, socketio = make_app(state_persistence=persistence)
    device = socketio.test_client(
        app,
        auth={"token": DEVICE_TOKEN, "role": "feedback_device"},
    )
    device.get_received()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {USER_A_TOKEN}"}

    response = client.post(
        "/api/feedback/demo",
        headers=headers,
        json={"level": "WARNING"},
    )
    assert response.status_code == 200
    assert named_events(device, "feedback_device_demo") == [
        {"v": 1, "level": "WARNING"}
    ]
    assert persistence.calls == []
    assert app.extensions["latest_device_feedback"]["decision"] is None

    start(app)
    response = client.post(
        "/api/feedback/demo",
        headers=headers,
        json={"level": "BREAK"},
    )
    assert response.status_code == 409
    assert named_events(device, "feedback_device_demo") == []


def test_feedback_demo_requires_user_auth_and_rejects_unknown_levels():
    app, _socketio = make_app()
    client = app.test_client()

    assert client.post(
        "/api/feedback/demo", json={"level": "WARNING"}
    ).status_code == 401
    assert client.post(
        "/api/feedback/demo",
        headers={"Authorization": f"Bearer {USER_A_TOKEN}"},
        json={"level": "DANGER"},
    ).status_code == 400
