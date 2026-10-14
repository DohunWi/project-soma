import uuid
from dataclasses import replace

from server.app import MEASUREMENT_PHASE_VALIDATOR, STATE_VALIDATOR, ChairPipeline, create_app
from server.auth import AuthIdentity, AuthenticationError
from server.measurement_calibration import MeasurementPhase
from server.measurement_calibration import MeasurementCalibration
from server.config import DEMO_PROFILE, NORMAL_PROFILE
from server.feedback_coordinator import FeedbackCoordinator
from feedback.config import feedback_config_for_mode
from fusion.distance_evidence import DistanceEvidenceClass
from fusion.state import OCCUPANCY_MIN
from fusion.load import score_integer


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


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def wall(self):
        return 1_800_000_000.0 + self.value

    def set(self, value):
        self.value = float(value)


class FakeTimer:
    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class RecordingPersistence:
    def __init__(self):
        self.calls = []
        self.ended = []

    def handle(self, payload, decision, **identity):
        self.calls.append((payload, decision, identity))

    def end_session(self, user_id, session_id):
        self.ended.append((user_id, session_id))


def vision_payload(t, distance=60.0, detected=True):
    vision = {"face_detected": detected}
    if distance is not None:
        vision["face_distance_cm"] = distance
    return {
        "v": 1,
        "t": 1000.0 + t,
        "source": "vision",
        "user_name": "vision",
        "vision": vision,
    }


def chair_payload(t, ir=250, pressure=None):
    return {
        "v": 1,
        "t": 1000.0 + t,
        "source": "chair",
        "device_id": "chair-test",
        "user_name": "chair",
        "chair": {
            "pressure": pressure or [300, 300, 300, 300],
            "ir": [ir],
        },
    }


def make_runtime(profile=DEMO_PROFILE):
    clock = FakeClock()
    timers = []

    def timer_factory(interval, callback):
        timer = FakeTimer(interval, callback)
        timers.append(timer)
        return timer

    persistence = RecordingPersistence()
    app, socketio = create_app(
        testing=True,
        runtime_profile=profile,
        auth_verifier=FakeAuthVerifier(),
        sensor_auth_token=SENSOR_TOKEN,
        sensor_monotonic=clock.monotonic,
        calibration_monotonic=clock.monotonic,
        calibration_wall_clock=clock.wall,
        calibration_timer_factory=timer_factory,
        state_persistence=persistence,
    )
    return app, socketio, clock, timers, persistence


def named(client, event_name):
    return [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == event_name
    ]


def start(app):
    return app.test_client().post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )


def calibrate(app, producer, clock):
    for elapsed in range(6):
        clock.set(elapsed)
        producer.emit("sensor_data", vision_payload(elapsed, distance=60))
        producer.emit("sensor_data", chair_payload(elapsed, ir=250))


def test_boundary_sample_transitions_ready_then_measuring_once_and_starts_clean():
    app, socketio, clock, timers, persistence = make_runtime()
    user = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})

    response = start(app)
    assert response.status_code == 201
    initial = named(user, "measurement_phase")
    assert [item["phase"] for item in initial] == ["CALIBRATING"]

    for elapsed in range(5):
        clock.set(elapsed)
        producer.emit("sensor_data", vision_payload(elapsed))
        producer.emit("sensor_data", chair_payload(elapsed))
        assert named(user, "state") == []
    assert app.extensions["measurement_calibration"].phase is MeasurementPhase.CALIBRATING
    assert persistence.calls == []

    clock.set(5)
    producer.emit("sensor_data", vision_payload(5))
    producer.emit("sensor_data", chair_payload(5))
    phases = named(user, "measurement_phase")
    assert [item["phase"] for item in phases[-2:]] == ["READY", "MEASURING"]
    assert sum(item["phase"] == "READY" for item in phases) == 1
    assert sum(item["phase"] == "MEASURING" for item in phases) == 1
    assert all(MEASUREMENT_PHASE_VALIDATOR.is_valid(item) for item in phases)
    assert named(user, "state") == []
    assert timers[0].cancelled is True

    clock.set(6)
    producer.emit("sensor_data", chair_payload(6))
    states = named(user, "state")
    assert len(states) == 1
    assert states[0]["metrics"]["static_hold_sec"] == 0.0
    assert states[0]["metrics"]["session_sec"] == 0.0
    assert len(persistence.calls) == 1


def test_fresh_vision_counts_but_stale_missing_and_duplicate_do_not():
    app, socketio, clock, _timers, _persistence = make_runtime()
    user = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    named(user, "measurement_phase")

    producer.emit("sensor_data", vision_payload(0))
    producer.emit("sensor_data", chair_payload(0))
    clock.set(1)
    producer.emit("sensor_data", chair_payload(1))
    clock.set(3)
    producer.emit("sensor_data", chair_payload(3))
    latest = named(user, "measurement_phase")[-1]

    assert latest["calibration"]["valid_samples"]["vision"] == 1
    assert latest["calibration"]["valid_samples"]["chair_ir"] == 3


def test_calibration_blocks_feedback_state_and_persistence_then_prepares_deltas():
    app, socketio, clock, _timers, persistence = make_runtime()
    user = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    calibrate(app, producer, clock)

    received = user.get_received()
    assert [event for event in received if event["name"] in ("state", "feedback")] == []
    assert persistence.calls == []

    clock.set(6)
    producer.emit("sensor_data", vision_payload(6, distance=55.0))
    producer.emit("sensor_data", chair_payload(6, ir=270))
    evidence = app.extensions["measurement_calibration"].latest_relative_evidence
    assert evidence == {
        "face_approach_delta_cm": 5.0,
        "backrest_departure_delta_mm": 20.0,
    }
    classification = app.extensions[
        "measurement_calibration"
    ].latest_distance_evidence
    assert classification.classification is DistanceEvidenceClass.NORMAL
    state = named(user, "state")[-1]
    assert state["metrics"]["face_distance_cm"] == 55.0
    assert "face_approach_delta_cm" not in state["metrics"]
    assert "distance_evidence" not in state["metrics"]


def test_missing_vision_invalid_ir_and_absent_never_create_posture_classification():
    app, socketio, clock, _timers, _persistence = make_runtime()
    user = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    assert app.extensions["measurement_calibration"].latest_distance_evidence is None
    calibrate(app, producer, clock)
    named(user, "measurement_phase")

    clock.set(6)
    producer.emit("sensor_data", chair_payload(6))
    missing = app.extensions["measurement_calibration"].latest_distance_evidence
    assert missing.classification is DistanceEvidenceClass.UNKNOWN
    assert missing.vision_available is False

    clock.set(7)
    producer.emit("sensor_data", vision_payload(7, distance=42.0))
    producer.emit("sensor_data", chair_payload(7, ir=-1))
    invalid_ir = app.extensions[
        "measurement_calibration"
    ].latest_distance_evidence
    assert invalid_ir.classification is DistanceEvidenceClass.UNKNOWN
    assert invalid_ir.chair_ir_available is False

    clock.set(8)
    producer.emit("sensor_data", vision_payload(8, distance=42.0))
    producer.emit(
        "sensor_data",
        chair_payload(8, pressure=[1, 1, 1, 1]),
    )
    absent = app.extensions["measurement_calibration"].latest_distance_evidence
    assert absent.classification is DistanceEvidenceClass.UNKNOWN
    assert absent.seated is False
    assert named(user, "state")[-1]["state"] == "ABSENT"


def test_stale_vision_is_unknown_even_when_last_face_was_close():
    app, socketio, clock, _timers, _persistence = make_runtime()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    calibrate(app, producer, clock)

    clock.set(6)
    producer.emit("sensor_data", vision_payload(6, distance=42.0))
    clock.set(9)
    producer.emit("sensor_data", chair_payload(9, ir=380))
    evidence = app.extensions["measurement_calibration"].latest_distance_evidence

    assert app.extensions["sensor_cache"].vision_availability.value == "STALE"
    assert evidence.classification is DistanceEvidenceClass.UNKNOWN
    assert evidence.vision_available is False


def test_fresh_vision_without_face_distance_is_unknown_and_freezes_penalty():
    app, socketio, clock, _timers, _persistence = make_runtime()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    calibrate(app, producer, clock)
    pipeline = app.extensions["chair_pipeline"]
    calibration = app.extensions["measurement_calibration"]

    for t in range(6, 21):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=42.0))
        producer.emit(
            "sensor_data",
            chair_payload(t, ir=380, pressure=[300 + (t % 2) * 20] * 4),
        )
    penalty_before_dropout = pipeline._state.load.distance_penalty
    assert penalty_before_dropout > 0

    clock.set(21)
    producer.emit("sensor_data", vision_payload(21, distance=None, detected=True))
    producer.emit("sensor_data", chair_payload(21, ir=380, pressure=[320] * 4))

    assert pipeline.sensor_cache.vision_availability.value == "FRESH"
    assert (
        calibration.latest_distance_evidence.classification
        is DistanceEvidenceClass.UNKNOWN
    )
    assert calibration.latest_distance_evidence.vision_available is False
    assert calibration.latest_distance_temporal.reasons == ("unavailable_freeze",)
    assert pipeline._state.load.distance_penalty == penalty_before_dropout


def test_opt_in_observer_receives_distance_evidence_without_state_contract_change():
    app, socketio, clock, _timers, _persistence = make_runtime()
    observer = socketio.test_client(
        app,
        auth={"token": USER_TOKEN, "observe_sensor_data": True},
    )
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    calibrate(app, producer, clock)
    observer.get_received()

    clock.set(6)
    producer.emit("sensor_data", vision_payload(6, distance=42.0))
    producer.emit("sensor_data", chair_payload(6, ir=380))
    observations = named(observer, "cross_validation_observation")

    distance = observations[-1]["distance_evidence"]
    assert distance["classification"] == "BODY_FORWARD_CLOSE"
    assert distance["face_approach_active"] is True
    assert distance["backrest_departure_active"] is True
    assert "distance_evidence" not in observations[-1]["state"]["metrics"]


def test_timeout_stays_calibrating_and_late_callback_after_stop_cannot_resurrect():
    app, socketio, clock, timers, persistence = make_runtime()
    user = socketio.test_client(app, auth={"token": USER_TOKEN})
    start(app)
    named(user, "measurement_phase")

    clock.set(30)
    timers[0].fire()
    failed = named(user, "measurement_phase")
    assert failed[-1]["phase"] == "CALIBRATING"
    assert failed[-1]["calibration"]["status"] == "FAILED"
    assert failed[-1]["calibration"]["error"] == "calibration_timeout"
    assert app.extensions["measurement_calibration"].phase is MeasurementPhase.CALIBRATING

    response = app.test_client().post(
        "/api/measurement/stop",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    assert response.status_code == 200
    assert named(user, "measurement_phase")[-1]["phase"] == "OFF"
    ended_count = len(persistence.ended)
    timers[0].fire()
    assert app.extensions["measurement_calibration"].phase is MeasurementPhase.OFF
    assert len(persistence.ended) == ended_count


def test_last_owner_disconnect_during_calibration_clears_session():
    app, socketio, _clock, timers, _persistence = make_runtime()
    owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    start(app)

    owner.disconnect()

    assert app.extensions["measurement_sessions"].active() is None
    assert app.extensions["measurement_calibration"].phase is MeasurementPhase.OFF
    assert app.extensions["measurement_calibration"].baselines is None
    assert timers[0].cancelled is True


def test_authenticated_stop_during_calibration_emits_off_and_cancels_timeout():
    app, socketio, _clock, timers, _persistence = make_runtime()
    owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    start(app)
    named(owner, "measurement_phase")

    response = app.test_client().post(
        "/api/measurement/stop",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )

    assert response.status_code == 200
    off = named(owner, "measurement_phase")[-1]
    assert off["phase"] == "OFF"
    assert off["session_id"] == response.get_json()["measurement"]["session_id"]
    assert app.extensions["measurement_calibration"].phase is MeasurementPhase.OFF
    assert timers[0].cancelled is True


def test_measurement_phase_is_scoped_to_the_authenticated_session_owner():
    app, socketio, _clock, _timers, _persistence = make_runtime()
    owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    other = socketio.test_client(app, auth={"token": OTHER_TOKEN})

    start(app)

    assert named(owner, "measurement_phase")[-1]["phase"] == "CALIBRATING"
    assert named(other, "measurement_phase") == []
    reconnected_owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    assert named(reconnected_owner, "measurement_phase")[-1]["phase"] == "CALIBRATING"


def test_normal_profile_requires_eight_seconds_even_after_eight_valid_samples():
    calibration = MeasurementCalibration(
        NORMAL_PROFILE.calibration,
        occupancy_min=OCCUPANCY_MIN,
    )
    calibration.start(uuid.uuid4(), 0.0)
    merged = {
        "pressure": [300, 300, 300, 300],
        "ir": [250],
        "face_detected": True,
        "face_distance_cm": 60.0,
    }
    for elapsed in range(8):
        merged["_vision_sender_t"] = float(elapsed)
        evaluation = calibration.observe(merged, float(elapsed))

    assert evaluation.became_ready is False
    assert calibration.phase is MeasurementPhase.CALIBRATING
    assert calibration.evaluate(8.0).became_ready is True


def test_timeout_callback_accepts_already_ready_samples_exactly_once():
    app, socketio, clock, timers, _persistence = make_runtime()
    user = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    named(user, "measurement_phase")
    for elapsed in range(5):
        clock.set(elapsed)
        producer.emit("sensor_data", vision_payload(elapsed))
        producer.emit("sensor_data", chair_payload(elapsed))
        named(user, "measurement_phase")

    clock.set(30)
    timers[0].fire()
    phases = named(user, "measurement_phase")

    assert [item["phase"] for item in phases] == ["READY", "MEASURING"]
    timers[0].fire()
    assert named(user, "measurement_phase") == []


def test_distance_temporal_never_accumulates_in_off_calibrating_or_ready():
    calibration = MeasurementCalibration(
        DEMO_PROFILE.calibration,
        occupancy_min=OCCUPANCY_MIN,
        distance_evidence_timing=DEMO_PROFILE.distance_evidence_timing,
    )
    sample = {
        "pressure": [300] * 4, "ir": [250],
        "face_detected": True, "face_distance_cm": 60.0,
    }
    assert calibration.relative_evidence(sample, now=0) == {}
    assert calibration.latest_distance_temporal is None
    calibration.start(uuid.uuid4(), 0)
    for t in range(6):
        sample["_vision_sender_t"] = t
        calibration.observe(sample, t)
        assert calibration.relative_evidence(sample, now=t) == {}
        assert calibration.latest_distance_temporal is None
    assert calibration.phase is MeasurementPhase.READY
    assert calibration.accept_ready() is True
    assert calibration.latest_distance_temporal is None
    sample.update(ir=[380], face_distance_cm=42)
    calibration.relative_evidence(sample, now=100)
    assert calibration.latest_distance_temporal.accumulated_sec == 0
    assert calibration.latest_distance_temporal.active is False


def test_temporal_measuring_path_changes_only_score_and_resets_between_sessions():
    app, socketio, clock, _timers, persistence = make_runtime()
    observer = socketio.test_client(
        app, auth={"token": USER_TOKEN, "observe_sensor_data": True},
    )
    ordinary_owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    other = socketio.test_client(
        app, auth={"token": OTHER_TOKEN, "observe_sensor_data": True},
    )
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    session_id = start(app).get_json()["measurement"]["session_id"]
    calibrate(app, producer, clock)
    observer.get_received()
    ordinary_owner.get_received()
    assert persistence.calls == []

    # Legacy path remains the reference for static/balance, state, reasons and metrics.
    reference = ChairPipeline(monotonic=clock.monotonic)
    reference_feedback = FeedbackCoordinator(feedback_config_for_mode("demo"))
    reference_feedback.start_session(session_id)
    calibration = app.extensions["measurement_calibration"]
    assert calibration.distance_evidence_timing is DEMO_PROFILE.distance_evidence_timing
    diagnostics = {}
    for t in range(6, 57):
        distance = 42 if t <= 16 or 26 <= t <= 47 else 60
        detected = not 17 <= t <= 19
        ir = 380 if t <= 16 or t >= 50 else 250
        pressure = [1] * 4 if 48 <= t <= 49 else [300] * 4
        vision = vision_payload(t, distance=distance, detected=detected)
        chair = chair_payload(t, ir=ir, pressure=pressure)
        clock.set(t)
        before = calibration.latest_distance_temporal
        producer.emit("sensor_data", vision)
        assert calibration.latest_distance_temporal is before  # Chair ticks only
        producer.emit("sensor_data", chair)
        reference.process(vision)
        expected = reference.process(chair)
        distance_penalty = app.extensions["chair_pipeline"]._state.load.distance_penalty
        expected["score"] = score_integer(replace(
            reference._state.load, distance_penalty=distance_penalty,
        ))
        events = observer.get_received()
        states = [event["args"][0] for event in events if event["name"] == "state"]
        assert states == [expected]
        assert STATE_VALIDATOR.is_valid(states[0])
        assert persistence.calls[-1][1] == expected
        feedback = [event["args"][0] for event in events if event["name"] == "feedback"]
        assert feedback == [
            reference_feedback.process_decision(session_id, expected, chair["t"])
        ]
        diagnostics[t] = [
            event["args"][0]["distance_temporal"]
            for event in events if event["name"] == "cross_validation_observation"
        ][0]
        assert not any(
            event["name"] == "cross_validation_observation"
            for event in ordinary_owner.get_received()
        )
        assert other.get_received() == []

    assert diagnostics[6]["accumulated_sec"] == 0
    assert diagnostics[15]["active"] is False
    assert diagnostics[16]["sustained_classification"] == "BODY_FORWARD_CLOSE"
    assert diagnostics[17]["accumulated_sec"] == diagnostics[16]["accumulated_sec"]
    assert diagnostics[19]["instantaneous_classification"] == "UNKNOWN"
    assert diagnostics[24]["active"] is True
    assert diagnostics[25]["active"] is False
    assert diagnostics[45]["active"] is False
    assert diagnostics[46]["sustained_classification"] == "FACE_ONLY_CLOSE"
    assert diagnostics[48]["reasons"] == ["absent_reset"]
    assert diagnostics[56]["active"] is False

    response = app.test_client().post(
        "/api/measurement/stop", headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    assert response.status_code == 200
    assert calibration.latest_distance_temporal is None
    observer.get_received()
    count = len(persistence.calls)
    producer.emit("sensor_data", chair_payload(57, ir=380))
    assert observer.get_received() == []
    assert calibration.latest_distance_temporal is None
    assert len(persistence.calls) == count

    clock.set(60)
    next_session_id = start(app).get_json()["measurement"]["session_id"]
    assert next_session_id != session_id
    for t in range(60, 66):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t))
        producer.emit("sensor_data", chair_payload(t))
    assert calibration.latest_distance_temporal is None
    clock.set(66)
    producer.emit("sensor_data", vision_payload(66, distance=42))
    producer.emit("sensor_data", chair_payload(66, ir=380))
    assert calibration.latest_distance_temporal.accumulated_sec == 0
    assert calibration.latest_distance_temporal.active is False
    assert app.extensions["chair_pipeline"]._state.load.distance_penalty == 0


def test_ir_dropout_and_stale_vision_freeze_temporal_candidate_in_real_merge_path():
    app, socketio, clock, _timers, _persistence = make_runtime()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    calibrate(app, producer, clock)
    calibration = app.extensions["measurement_calibration"]
    for t in range(6, 11):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=42))
        producer.emit("sensor_data", chair_payload(t, ir=380))
    assert calibration.latest_distance_temporal.accumulated_sec == 4
    clock.set(11)
    producer.emit("sensor_data", vision_payload(11, distance=42))
    producer.emit("sensor_data", chair_payload(11, ir=-1))
    assert calibration.latest_distance_temporal.accumulated_sec == 4
    clock.set(14)  # Cached Vision is stale at the existing 3-second boundary.
    producer.emit("sensor_data", chair_payload(14, ir=380))
    assert calibration.latest_distance_temporal.accumulated_sec == 4
    assert calibration.latest_distance_temporal.reasons == ("unavailable_freeze",)
    for t in (15, 16):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=42))
        producer.emit("sensor_data", chair_payload(t, ir=380))
    assert calibration.latest_distance_temporal.accumulated_sec == 5


def test_active_distance_score_is_emitted_before_persistence_and_session_reset_is_clean():
    app, socketio, clock, _timers, persistence = make_runtime()
    owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    for t in range(6):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=80))
        producer.emit("sensor_data", chair_payload(t))
    owner.get_received()
    pipeline = app.extensions["chair_pipeline"]
    assert pipeline._state.load.distance_penalty == 0
    assert persistence.calls == []

    original_handle = persistence.handle
    observed_states = []

    def assert_emit_before_store(payload, decision, **identity):
        states = named(owner, "state")
        assert states == [decision]
        assert STATE_VALIDATOR.is_valid(decision)
        assert "distance_penalty" not in decision["metrics"]
        observed_states.append(decision)
        original_handle(payload, decision, **identity)

    persistence.handle = assert_emit_before_store
    for t in range(6, 21):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=60))
        producer.emit("sensor_data", chair_payload(
            t, ir=380, pressure=[300 + (t % 2) * 20] * 4,
        ))
    assert pipeline._state.load.distance_penalty == 2
    assert observed_states[-1]["score"] == 98
    assert observed_states[-1]["state"] == "NORMAL"
    assert observed_states[-1]["reasons"] == []

    # Existing 3-second freshness cutoff and invalid IR both freeze the scored residual.
    clock.set(23)
    producer.emit("sensor_data", chair_payload(23, ir=380, pressure=[320] * 4))
    assert pipeline._state.load.distance_penalty == 2
    clock.set(24)
    producer.emit("sensor_data", vision_payload(24, distance=60))
    producer.emit("sensor_data", chair_payload(24, ir=-1, pressure=[300] * 4))
    assert pipeline._state.load.distance_penalty == 2

    response = app.test_client().post(
        "/api/measurement/stop", headers={"Authorization": f"Bearer {USER_TOKEN}"},
    )
    assert response.status_code == 200
    owner.get_received()
    count = len(persistence.calls)
    producer.emit("sensor_data", chair_payload(25, ir=380))
    assert owner.get_received() == []
    assert len(persistence.calls) == count
    assert pipeline._state.load.distance_penalty == 0

    clock.set(26)
    start(app)
    for t in range(26, 32):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=80))
        producer.emit("sensor_data", chair_payload(t))
    owner.get_received()
    assert pipeline._state.load.distance_penalty == 0
    clock.set(32)
    producer.emit("sensor_data", vision_payload(32, distance=60))
    producer.emit("sensor_data", chair_payload(32, ir=380))
    assert observed_states[-1]["score"] == 100
    assert pipeline._state.load.distance_penalty == 0


def test_normal_runtime_injects_normal_entry_and_penalty_rate():
    app, socketio, clock, _timers, _persistence = make_runtime(NORMAL_PROFILE)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    for t in range(9):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=80))
        producer.emit("sensor_data", chair_payload(t))
    pipeline = app.extensions["chair_pipeline"]
    for t in range(9, 311):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=60))
        producer.emit("sensor_data", chair_payload(
            t, ir=380, pressure=[300 + (t % 2) * 20] * 4,
        ))
        if t <= 309:
            assert pipeline._state.load.distance_penalty == 0
    assert abs(pipeline._state.load.distance_penalty - 0.01) < 1e-12


def test_producer_cannot_inject_distance_penalty_from_untrusted_payload_fields():
    app, socketio, clock, _timers, _persistence = make_runtime()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    start(app)
    calibrate(app, producer, clock)
    for t in range(6, 36):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t))
        payload = chair_payload(t)
        payload["distance_penalty"] = 25
        payload["distance_evidence"] = {"active": True, "classification": "BODY_FORWARD_CLOSE"}
        producer.emit("sensor_data", payload)
    assert app.extensions["chair_pipeline"]._state.load.distance_penalty == 0


def test_new_score_feeds_existing_feedback_low_score_threshold_without_policy_changes():
    app, socketio, clock, _timers, _persistence = make_runtime()
    owner = socketio.test_client(app, auth={"token": USER_TOKEN})
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    session_id = start(app).get_json()["measurement"]["session_id"]
    for t in range(6):
        clock.set(t)
        producer.emit("sensor_data", vision_payload(t, distance=80))
        producer.emit("sensor_data", chair_payload(t))
    owner.get_received()
    reference = ChairPipeline(monotonic=clock.monotonic)
    legacy_feedback = FeedbackCoordinator(feedback_config_for_mode("demo"))
    legacy_feedback.start_session(session_id)
    for t in range(6, 61):
        clock.set(t)
        offset = (t % 2) * 20
        vision = vision_payload(t, distance=60)
        chair = chair_payload(t, ir=380, pressure=[700 + offset, 300 + offset] * 2)
        producer.emit("sensor_data", vision)
        producer.emit("sensor_data", chair)
        reference.process(vision)
        legacy_decision = reference.process(chair)
        legacy = legacy_feedback.process_decision(session_id, legacy_decision, chair["t"])
        events = owner.get_received()
        current = [event["args"][0] for event in events if event["name"] == "state"][0]
        current_feedback = [
            event["args"][0] for event in events if event["name"] == "feedback"
        ][0]
    assert current["state"] == legacy_decision["state"] == "CAUTION"
    assert legacy_decision["score"] == 75
    assert current["score"] == 60
    assert legacy["level"] != "BREAK"
    assert current_feedback["level"] == "BREAK"
    assert current_feedback["reason"] == "LOW_SCORE"
