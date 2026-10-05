"""Backend integration tests for session-scoped logical Feedback decisions."""
import uuid

import pytest

from feedback.config import DEMO_FEEDBACK_CONFIG, NORMAL_FEEDBACK_CONFIG
from feedback.policy.break_policy import FeedbackPolicyState, step as policy_step
from server.app import create_app
from server.auth import AuthIdentity, AuthenticationError
from server.config import DEMO_PROFILE, NORMAL_PROFILE
from server.feedback_coordinator import (
    FeedbackCoordinator,
    FeedbackDecisionError,
)


USER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
SESSION_A = uuid.UUID("33333333-3333-4333-8333-333333333333")
SESSION_B = uuid.UUID("44444444-4444-4444-8444-444444444444")
USER_A_TOKEN = "user-a-token"
USER_B_TOKEN = "user-b-token"
SENSOR_TOKEN = "sensor-token"


class FakeAuthVerifier:
    def verify(self, token):
        if token == USER_A_TOKEN:
            return AuthIdentity(USER_A)
        if token == USER_B_TOKEN:
            return AuthIdentity(USER_B)
        raise AuthenticationError("invalid token")


class RecordingPersistence:
    def __init__(self, order=None):
        self.calls = []
        self.order = order

    def handle(self, payload, decision, **identity):
        if self.order is not None:
            self.order.append("persistence")
        self.calls.append((payload, decision, identity))

    def end_session(self, _user_id, _session_id):
        pass


def make_app(**kwargs):
    return create_app(
        testing=True,
        auth_verifier=FakeAuthVerifier(),
        sensor_auth_token=SENSOR_TOKEN,
        _skip_calibration_for_testing=True,
        **kwargs,
    )


def chair_payload(t=1000.0, pressure=None):
    return {
        "v": 1,
        "t": t,
        "source": "chair",
        "user_name": "feedback-test",
        "device_id": "chair-test",
        "chair": {
            "pressure": pressure or [850, 850, 850, 850],
            "ir": [250],
        },
    }


def vision_payload(t=1000.0):
    return {
        "v": 1,
        "t": t,
        "source": "vision",
        "user_name": "feedback-test",
        "vision": {
            "face_detected": True,
            "detect_rate": 1.0,
            "blink_rate": 0.0,
            "face_distance_cm": 10.0,
            "face_lateral_offset": 0.2,
            "head_roll_deg": 3.0,
            "head_roll_delta_deg": 1.0,
            "face_lateral_calibrated": True,
            "face_lean_direction": "LEFT",
        },
    }


def start(app, token=USER_A_TOKEN):
    return app.test_client().post(
        "/api/measurement/start",
        headers={"Authorization": f"Bearer {token}"},
    )


def stop(app, token=USER_A_TOKEN):
    return app.test_client().post(
        "/api/measurement/stop",
        headers={"Authorization": f"Bearer {token}"},
    )


def events(client, name):
    return [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == name
    ]


def named_events(received, name):
    return [event["args"][0] for event in received if event["name"] == name]


def test_coordinator_keeps_sessions_independent_and_validates_before_commit():
    coordinator = FeedbackCoordinator(DEMO_FEEDBACK_CONFIG)
    coordinator.start_session(SESSION_A)
    coordinator.start_session(SESSION_B)

    for elapsed in range(11):
        decision = {"state": "DANGER", "score": 100}
        coordinator.process_decision(SESSION_A, decision, 1000.0 + elapsed)

    state_a = coordinator.state_for(SESSION_A)
    state_b = coordinator.state_for(SESSION_B)
    assert state_a.break_active is True
    assert state_b == FeedbackPolicyState()

    def invalid_step(previous, fusion_decision, now, session_id, *, config):
        current, decision = policy_step(
            previous,
            fusion_decision,
            now,
            session_id,
            config=config,
        )
        decision.pop("level")
        return current, decision

    invalid = FeedbackCoordinator(
        DEMO_FEEDBACK_CONFIG,
        policy_step=invalid_step,
    )
    invalid.start_session(SESSION_A)
    with pytest.raises(FeedbackDecisionError):
        invalid.process_decision(
            SESSION_A,
            {"state": "NORMAL", "score": 100},
            1000.0,
        )
    assert invalid.state_for(SESSION_A) == FeedbackPolicyState()


def test_runtime_profile_selects_matching_feedback_config():
    demo, _ = make_app(runtime_profile=DEMO_PROFILE)
    normal, _ = make_app(runtime_profile=NORMAL_PROFILE)

    assert demo.extensions["feedback_coordinator"].config is DEMO_FEEDBACK_CONFIG
    assert normal.extensions["feedback_coordinator"].config is NORMAL_FEEDBACK_CONFIG


def test_start_duplicate_stop_and_new_start_manage_policy_lifecycle():
    app, socketio = make_app(runtime_profile=DEMO_PROFILE)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    first = start(app)
    first_session = uuid.UUID(first.get_json()["measurement"]["session_id"])

    for elapsed in range(5):
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))
    before_duplicate = app.extensions["feedback_coordinator"].state_for(first_session)
    duplicate = start(app)

    assert duplicate.status_code == 200
    assert app.extensions["feedback_coordinator"].state_for(
        first_session
    ) == before_duplicate

    stopped = stop(app)
    assert stopped.status_code == 200
    assert app.extensions["feedback_coordinator"].state_for(first_session) is None

    restarted = start(app)
    second_session = uuid.UUID(restarted.get_json()["measurement"]["session_id"])
    assert second_session != first_session
    assert app.extensions["feedback_coordinator"].state_for(
        second_session
    ) == FeedbackPolicyState()


def test_off_and_vision_only_events_do_not_tick_feedback():
    app, socketio = make_app()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})

    producer.emit("sensor_data", chair_payload())
    assert events(front, "feedback") == []

    response = start(app)
    session_id = response.get_json()["measurement"]["session_id"]
    producer.emit("sensor_data", vision_payload())

    assert events(front, "state") == []
    assert events(front, "feedback") == []
    assert app.extensions["feedback_coordinator"].state_for(
        session_id
    ) == FeedbackPolicyState()


def test_chair_ticks_emit_current_feedback_after_state_through_break_lifecycle():
    order = []
    persistence = RecordingPersistence(order)
    app, socketio = make_app(
        runtime_profile=DEMO_PROFILE,
        state_persistence=persistence,
    )
    original_emit = socketio.emit

    def recording_emit(name, *args, **kwargs):
        order.append(name)
        return original_emit(name, *args, **kwargs)

    socketio.emit = recording_emit
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    start(app)
    order.clear()

    for elapsed in range(32):
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))

    received = front.get_received()
    state_decisions = named_events(received, "state")
    feedback_decisions = named_events(received, "feedback")
    assert len(state_decisions) == len(feedback_decisions) == 32
    assert feedback_decisions[0]["level"] == "NORMAL"
    assert feedback_decisions[10]["level"] == "NOTICE"
    assert feedback_decisions[20]["level"] == "WARNING"
    assert feedback_decisions[29]["level"] == "BREAK"
    assert feedback_decisions[29]["transition"] is True
    assert feedback_decisions[30]["level"] == "BREAK"
    assert feedback_decisions[30]["transition"] is False
    assert feedback_decisions[0]["t"] == state_decisions[0]["t"] == 1000.0
    assert order[:4] == [
        "state",
        "feedback",
        "feedback_device",
        "persistence",
    ]

    for elapsed in range(32, 42):
        producer.emit(
            "sensor_data",
            chair_payload(1000.0 + elapsed, pressure=[1, 1, 1, 1]),
        )
    released = events(front, "feedback")[-1]
    active = app.extensions["measurement_sessions"].active()
    policy_state = app.extensions["feedback_coordinator"].state_for(
        active.session_id
    )
    assert released["level"] == "NORMAL"
    assert released["reason"] == "ABSENT"
    assert released["transition"] is True
    assert policy_state.break_active is False
    assert policy_state.work_period_sec == 0.0
    assert policy_state.cooldown_remaining_sec == 30.0


def test_feedback_is_routed_only_to_the_authenticated_active_user_room():
    app, socketio = make_app()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    active_user = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    other_user = socketio.test_client(app, auth={"token": USER_B_TOKEN})
    start(app)

    producer.emit("sensor_data", chair_payload())

    assert len(events(active_user, "feedback")) == 1
    assert events(other_user, "feedback") == []


def test_cooldown_blocks_break_until_expiry_in_server_path(monkeypatch):
    def deterministic_fusion(state, sample, now, **_kwargs):
        seated = sum(sample["pressure"]) >= 100
        fusion_state = "DANGER" if seated else "ABSENT"
        return state, {
            "v": 1,
            "t": now,
            "user_name": sample["user_name"],
            "state": fusion_state,
            "confidence": 1.0,
            "score": 100,
            "reasons": [],
            "metrics": {
                "balance": "CENTER",
                "seated": seated,
                "static_hold_sec": 0.0,
                "session_sec": 0.0,
            },
        }

    monkeypatch.setattr("server.app.step", deterministic_fusion)
    app, socketio = make_app(runtime_profile=DEMO_PROFILE)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    start(app)

    for elapsed in range(11):
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))
    assert events(front, "feedback")[-1]["level"] == "BREAK"

    for elapsed in range(11, 21):
        producer.emit(
            "sensor_data",
            chair_payload(1000.0 + elapsed, pressure=[1, 1, 1, 1]),
        )
    assert events(front, "feedback")[-1]["level"] == "NORMAL"

    for elapsed in range(21, 50):
        producer.emit("sensor_data", chair_payload(1000.0 + elapsed))
    blocked = events(front, "feedback")
    assert blocked[-1]["level"] == "WARNING"

    producer.emit("sensor_data", chair_payload(1050.0))
    allowed = events(front, "feedback")[-1]
    assert allowed["level"] == "BREAK"
    assert allowed["reason"] == "SUSTAINED_DANGER"
    assert allowed["transition"] is True


def test_fresh_vision_changes_metrics_but_phase_c_score_penalty_remains_zero():
    app, socketio = make_app()
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    start(app)

    producer.emit("sensor_data", vision_payload(1000.0))
    assert events(front, "feedback") == []
    producer.emit("sensor_data", chair_payload(1000.0))

    received = front.get_received()
    decision = named_events(received, "state")[0]
    assert decision["metrics"]["blink_rate"] == 0.0
    assert decision["metrics"]["face_distance_cm"] == 10.0
    assert decision["metrics"]["vision_face_lateral_offset"] == 0.2
    assert decision["metrics"]["vision_face_lean_direction"] == "LEFT"
    assert decision["score"] == 100
    assert named_events(received, "feedback")[0]["level"] == "NORMAL"


def test_policy_exception_isolated_and_next_tick_and_persistence_continue():
    persistence = RecordingPersistence()
    app, socketio = make_app(state_persistence=persistence)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    start(app)
    coordinator = app.extensions["feedback_coordinator"]
    original = coordinator.process_decision

    def fail(*_args, **_kwargs):
        raise RuntimeError("feedback unavailable")

    coordinator.process_decision = fail
    producer.emit("sensor_data", chair_payload(1000.0))
    coordinator.process_decision = original
    producer.emit("sensor_data", chair_payload(1001.0))

    received = front.get_received()
    assert len(named_events(received, "state")) == 2
    assert len(named_events(received, "feedback")) == 1
    assert len(persistence.calls) == 2


def test_contract_validation_failure_isolated_from_state_and_persistence():
    def invalid_step(previous, fusion_decision, now, session_id, *, config):
        current, decision = policy_step(
            previous,
            fusion_decision,
            now,
            session_id,
            config=config,
        )
        decision["level"] = "INVALID"
        return current, decision

    persistence = RecordingPersistence()
    coordinator = FeedbackCoordinator(
        DEMO_FEEDBACK_CONFIG,
        policy_step=invalid_step,
    )
    app, socketio = make_app(
        feedback_coordinator=coordinator,
        state_persistence=persistence,
    )
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    response = start(app)

    producer.emit("sensor_data", chair_payload())

    session_id = response.get_json()["measurement"]["session_id"]
    assert len(events(front, "state")) == 1
    assert events(front, "feedback") == []
    assert len(persistence.calls) == 1
    assert coordinator.state_for(session_id) == FeedbackPolicyState()


def test_feedback_emit_failure_isolated_and_current_level_resyncs_next_tick():
    persistence = RecordingPersistence()
    app, socketio = make_app(state_persistence=persistence)
    producer = socketio.test_client(app, auth={"token": SENSOR_TOKEN})
    front = socketio.test_client(app, auth={"token": USER_A_TOKEN})
    start(app)
    original_emit = socketio.emit

    def fail_feedback(name, *args, **kwargs):
        if name == "feedback":
            raise RuntimeError("feedback transport unavailable")
        return original_emit(name, *args, **kwargs)

    socketio.emit = fail_feedback
    producer.emit("sensor_data", chair_payload(1000.0))
    socketio.emit = original_emit
    producer.emit("sensor_data", chair_payload(1001.0))

    received = front.get_received()
    state_decisions = named_events(received, "state")
    feedback_decisions = named_events(received, "feedback")
    assert len(state_decisions) == 2
    assert len(feedback_decisions) == 1
    assert feedback_decisions[0]["level"] == "NORMAL"
    assert feedback_decisions[0]["transition"] is False
    assert len(persistence.calls) == 2
