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
        **kwargs,
    )


def chair_payload(t=1000.0):
    return {
        "v": 1,
        "t": t,
        "source": "chair",
        "user_name": "device-test",
        "device_id": "chair-test",
        "chair": {"pressure": [850, 850, 850, 850], "ir": [250]},
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
