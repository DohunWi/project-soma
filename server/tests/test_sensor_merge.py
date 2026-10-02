"""Hardware-independent tests for session-scoped Chair/Vision merging."""
import uuid

import pytest

from server.app import create_app
from server.auth import AuthIdentity, AuthenticationError
from server.sensor_merge import SessionSensorCache, VisionAvailability


USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_TOKEN = "user-token"
SENSOR_TOKEN = "sensor-token"


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeAuthVerifier:
    def verify(self, token):
        if token == USER_TOKEN:
            return AuthIdentity(USER_ID)
        raise AuthenticationError("invalid token")


class RecordingPersistence:
    def __init__(self, order=None):
        self.calls = []
        self.ended = []
        self.order = order

    def handle(self, payload, decision, **identity):
        if self.order is not None:
            self.order.append("persistence")
        self.calls.append((payload, decision, identity))

    def end_session(self, user_id, session_id):
        self.ended.append((user_id, session_id))


def vision_payload(t=1000.0, **metrics):
    vision = {"face_detected": True}
    vision.update(metrics)
    return {
        "v": 1,
        "t": t,
        "source": "vision",
        "user_name": "vision-user",
        "vision": vision,
    }


def chair_payload(t=1000.0, pressure=None):
    return {
        "v": 1,
        "t": t,
        "source": "chair",
        "user_name": "chair-user",
        "device_id": "chair-test",
        "chair": {
            "pressure": pressure or [850, 850, 850, 850],
            "ir": [321],
        },
    }


def make_app(clock, persistence=None):
    return create_app(
        testing=True,
        auth_verifier=FakeAuthVerifier(),
        sensor_auth_token=SENSOR_TOKEN,
        sensor_monotonic=clock.monotonic,
        state_persistence=persistence,
    )


def start_measurement(app):
    response = app.test_client().post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    assert response.status_code in (200, 201)
    return response


def state_events(client):
    return [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == "state"
    ]


def test_new_cache_is_unseen_and_reset_forgets_both_sources():
    clock = FakeClock()
    cache = SessionSensorCache(monotonic=clock.monotonic)
    cache.update_vision(vision_payload(), received_at=clock.value)
    sample = chair_payload()
    cache.mark_chair_processed(sample, received_at=clock.value)

    cache.reset()

    assert cache.latest_chair is None
    assert cache.latest_vision is None
    assert cache.vision_availability is VisionAvailability.UNSEEN


@pytest.mark.parametrize("age", [0.0, 2.999])
def test_receipt_age_below_three_seconds_merges_vision(age):
    cache = SessionSensorCache()
    cache.update_vision(
        vision_payload(
            blink_rate=12.0,
            face_distance_cm=55.0,
            detect_rate=0.9,
            blink_rate_baseline=15.0,
            face_distance_baseline_cm=60.0,
        ),
        received_at=10.0,
    )

    sample = cache.merged_chair_sample(chair_payload(), received_at=10.0 + age)

    assert sample["blink_rate"] == 12.0
    assert sample["face_distance_cm"] == 55.0
    assert sample["face_detected"] is True
    assert sample["detect_rate"] == 0.9
    assert sample["blink_rate_baseline"] == 15.0
    assert sample["face_distance_baseline_cm"] == 60.0


def test_receipt_age_at_three_seconds_is_stale():
    cache = SessionSensorCache()
    cache.update_vision(vision_payload(blink_rate=12.0), received_at=10.0)

    sample = cache.merged_chair_sample(chair_payload(), received_at=13.0)

    assert "blink_rate" not in sample
    assert cache.vision_availability is VisionAvailability.STALE


@pytest.mark.parametrize("skew", [0.0, 2.999])
def test_sender_skew_below_three_seconds_merges_vision(skew):
    cache = SessionSensorCache()
    cache.update_vision(vision_payload(t=1000.0, blink_rate=9.0), received_at=1.0)

    sample = cache.merged_chair_sample(
        chair_payload(t=1000.0 + skew),
        received_at=1.0,
    )

    assert sample["blink_rate"] == 9.0


def test_sender_skew_at_three_seconds_omits_vision():
    cache = SessionSensorCache()
    cache.update_vision(vision_payload(t=1000.0, blink_rate=9.0), received_at=1.0)

    sample = cache.merged_chair_sample(chair_payload(t=1003.0), received_at=1.0)

    assert "blink_rate" not in sample


def test_older_vision_is_rejected_and_same_timestamp_is_last_arrival_wins():
    cache = SessionSensorCache()
    assert cache.update_vision(
        vision_payload(t=1000.0, blink_rate=10.0), received_at=1.0
    )
    assert not cache.update_vision(
        vision_payload(t=999.0, blink_rate=1.0), received_at=1.1
    )
    assert cache.update_vision(
        vision_payload(t=1000.0, blink_rate=15.0), received_at=1.2
    )

    sample = cache.merged_chair_sample(chair_payload(t=1000.0), received_at=1.2)

    assert sample["blink_rate"] == 15.0


def test_duplicate_and_older_chair_timestamps_are_rejected_after_commit():
    cache = SessionSensorCache()
    first = chair_payload(t=1000.0)
    assert cache.merged_chair_sample(first, received_at=1.0) is not None
    cache.mark_chair_processed(first, received_at=1.0)

    assert cache.merged_chair_sample(chair_payload(t=1000.0), received_at=2.0) is None
    assert cache.merged_chair_sample(chair_payload(t=999.0), received_at=2.0) is None
    assert cache.merged_chair_sample(chair_payload(t=1001.0), received_at=2.0) is not None


def test_stale_vision_recovers_with_a_new_sample():
    cache = SessionSensorCache()
    cache.update_vision(vision_payload(t=1000.0, blink_rate=8.0), received_at=0.0)
    stale = cache.merged_chair_sample(chair_payload(t=1003.0), received_at=3.0)
    assert "blink_rate" not in stale

    cache.update_vision(vision_payload(t=1003.0, blink_rate=14.0), received_at=3.0)
    assert cache.vision_availability is VisionAvailability.RECOVERED
    recovered = cache.merged_chair_sample(
        chair_payload(t=1003.1), received_at=3.1
    )

    assert recovered["blink_rate"] == 14.0
    assert cache.vision_availability is VisionAvailability.FRESH


def test_only_present_supported_metrics_are_merged():
    cache = SessionSensorCache()
    cache.update_vision(
        vision_payload(
            face_detected=False,
            detect_rate=0.2,
            calibrating=True,
            blink=True,
        ),
        received_at=0.0,
    )

    sample = cache.merged_chair_sample(chair_payload(), received_at=0.0)

    assert sample["face_detected"] is False
    assert sample["detect_rate"] == 0.2
    assert "blink_rate" not in sample
    assert "face_distance_cm" not in sample
    assert "calibrating" not in sample
    assert "blink" not in sample


def test_fresh_observational_lateral_metrics_merge_without_chair_overload():
    cache = SessionSensorCache()
    cache.update_vision(
        vision_payload(
            face_lateral_offset=0.2,
            head_roll_deg=3.0,
            head_roll_delta_deg=1.5,
            face_lateral_calibrated=True,
            face_lean_direction="LEFT",
        ),
        received_at=0.0,
    )

    sample = cache.merged_chair_sample(chair_payload(), received_at=0.0)

    assert sample["face_lateral_offset"] == 0.2
    assert sample["head_roll_deg"] == 3.0
    assert sample["head_roll_delta_deg"] == 1.5
    assert sample["face_lateral_calibrated"] is True
    assert sample["face_lean_direction"] == "LEFT"
    assert "balance" not in sample


def test_stale_vision_does_not_fabricate_face_lean_direction():
    cache = SessionSensorCache()
    cache.update_vision(
        vision_payload(face_lean_direction="RIGHT"),
        received_at=10.0,
    )

    sample = cache.merged_chair_sample(chair_payload(t=1003.0), received_at=13.0)

    assert "face_lean_direction" not in sample


def test_vision_event_only_updates_cache_without_fusion_emit_or_persistence(monkeypatch):
    clock = FakeClock()
    persistence = RecordingPersistence()
    app, socketio = make_app(clock, persistence)
    start_measurement(app)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})
    fusion_calls = []

    def forbidden_step(*args, **kwargs):
        fusion_calls.append((args, kwargs))
        raise AssertionError("Vision must not call Fusion")

    monkeypatch.setattr("server.app.step", forbidden_step)
    producer.emit("sensor_data", vision_payload(blink_rate=12.0))

    assert app.extensions["sensor_cache"].latest_vision is not None
    assert fusion_calls == []
    assert state_events(front) == []
    assert persistence.calls == []


def test_fresh_latest_vision_is_merged_on_next_chair_tick():
    clock = FakeClock()
    app, socketio = make_app(clock)
    start_measurement(app)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer.emit("sensor_data", vision_payload(t=1000.0, blink_rate=9.0))
    clock.advance(0.5)
    producer.emit("sensor_data", vision_payload(t=1000.5, blink_rate=13.0))
    assert state_events(front) == []

    clock.advance(0.5)
    producer.emit("sensor_data", chair_payload(t=1001.0))
    states = state_events(front)

    assert len(states) == 1
    assert states[0]["metrics"]["blink_rate"] == 13.0


def test_fresh_vision_is_reused_only_until_timeout():
    clock = FakeClock()
    app, socketio = make_app(clock)
    start_measurement(app)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer.emit("sensor_data", vision_payload(t=1000.0, blink_rate=13.0))

    clock.advance(2.999)
    producer.emit("sensor_data", chair_payload(t=1002.999))
    fresh = state_events(front)
    clock.advance(0.001)
    producer.emit("sensor_data", chair_payload(t=1003.0))
    stale = state_events(front)

    assert fresh[0]["metrics"]["blink_rate"] == 13.0
    assert "blink_rate" not in stale[0]["metrics"]


def test_zero_detection_rate_freezes_blink_after_merge_and_recovery_resumes():
    clock = FakeClock()
    app, socketio = make_app(clock)
    start_measurement(app)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})

    producer.emit(
        "sensor_data",
        vision_payload(t=1000.0, blink_rate=5.0, detect_rate=0.5),
    )
    producer.emit("sensor_data", chair_payload(t=1000.0))
    clock.advance(1.0)
    producer.emit("sensor_data", chair_payload(t=1001.0))
    observed = state_events(front)
    assert observed[-1]["metrics"]["low_blink_sec"] == 1.0

    producer.emit(
        "sensor_data",
        vision_payload(t=1002.0, blink_rate=0.0, detect_rate=0.0),
    )
    clock.advance(1.0)
    producer.emit("sensor_data", chair_payload(t=1002.0))
    unavailable = state_events(front)
    assert unavailable[-1]["metrics"]["low_blink_sec"] == 1.0
    assert unavailable[-1]["metrics"]["blink_rate"] == 0.0

    producer.emit(
        "sensor_data",
        vision_payload(t=1003.0, blink_rate=5.0, detect_rate=0.1),
    )
    clock.advance(1.0)
    producer.emit("sensor_data", chair_payload(t=1003.0))
    recovered = state_events(front)
    assert recovered[-1]["metrics"]["low_blink_sec"] == 2.0


def test_duplicate_and_older_chair_events_do_not_advance_fusion_time():
    clock = FakeClock()
    app, socketio = make_app(clock)
    start_measurement(app)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})

    for timestamp in (1000.0, 1000.0, 999.0, 1001.0):
        producer.emit("sensor_data", chair_payload(t=timestamp))
    states = state_events(front)

    assert len(states) == 2
    assert states[-1]["metrics"]["static_hold_sec"] == 1.0


def test_session_start_duplicate_stop_off_and_restart_cache_lifecycle():
    clock = FakeClock()
    app, socketio = make_app(clock)
    http = app.test_client()
    headers = {"Authorization": f"Bearer {USER_TOKEN}"}
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_TOKEN})
    first = http.post("/api/measurement/start", headers=headers)
    producer.emit("sensor_data", vision_payload(t=1000.0, blink_rate=12.0))

    duplicate = http.post("/api/measurement/start", headers=headers)
    assert duplicate.status_code == 200
    assert app.extensions["sensor_cache"].latest_vision is not None

    stopped = http.post("/api/measurement/stop", headers=headers)
    assert stopped.status_code == 200
    assert app.extensions["sensor_cache"].latest_vision is None
    producer.emit("sensor_data", vision_payload(t=2000.0, blink_rate=5.0))
    assert app.extensions["sensor_cache"].latest_vision is None

    restarted = http.post("/api/measurement/start", headers=headers)
    producer.emit("sensor_data", chair_payload(t=2000.0))
    states = state_events(front)

    assert first.status_code == 201
    assert restarted.status_code == 201
    assert "blink_rate" not in states[0]["metrics"]


def test_persistence_keeps_original_chair_metadata_and_merged_metrics():
    clock = FakeClock()
    persistence = RecordingPersistence()
    app, socketio = make_app(clock, persistence)
    start_measurement(app)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    producer.emit(
        "sensor_data",
        vision_payload(t=1000.0, blink_rate=12.5, face_distance_cm=55.0),
    )
    original_chair = chair_payload(t=1000.5)

    producer.emit("sensor_data", original_chair)

    persisted_payload, decision, identity = persistence.calls[0]
    assert persisted_payload == original_chair
    assert persisted_payload["device_id"] == "chair-test"
    assert persisted_payload["chair"]["ir"] == [321]
    assert decision["metrics"]["blink_rate"] == 12.5
    assert decision["metrics"]["face_distance_cm"] == 55.0
    assert identity["user_id"] == USER_ID
