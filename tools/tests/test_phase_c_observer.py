"""Tests for the read-only authenticated Phase C observer."""

from tools.phase_c_observer import format_observation, run_observer


class FakeSocket:
    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.handlers = {}
        self.connect_args = None

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self, url, *, auth, transports):
        self.connected = True
        self.connect_args = (url, auth, transports)

    def disconnect(self):
        self.connected = False
        self.disconnected = True


def observation():
    return {
        "vision_availability": "FRESH",
        "distance_evidence": {
            "classification": "FACE_ONLY_CLOSE",
            "face_approach_delta_cm": 12.25,
            "backrest_departure_delta_mm": 17.0,
        },
        "distance_temporal": {
            "candidate_classification": "FACE_ONLY_CLOSE",
            "sustained_classification": "FACE_ONLY_CLOSE",
            "accumulated_sec": 21.0,
            "active": True,
            "recovery_sec": 0.0,
        },
        "state": {"score": 89},
    }


def test_formats_requested_phase_c_fields_on_one_line():
    assert format_observation(observation()) == (
        "[phase-c-observer] vision=FRESH class=FACE_ONLY_CLOSE "
        "face_delta_cm=12.250 ir_delta_mm=17.000 "
        "candidate=FACE_ONLY_CLOSE sustained=FACE_ONLY_CLOSE "
        "accumulated_sec=21.000 active=true recovery_sec=0.000 score=89"
    )


def test_missing_optional_diagnostics_are_explicit():
    assert format_observation({"state": {"score": 100}}) == (
        "[phase-c-observer] vision=- class=- face_delta_cm=- ir_delta_mm=- "
        "candidate=- sustained=- accumulated_sec=- active=- recovery_sec=- "
        "score=100"
    )


def test_connects_with_existing_authenticated_opt_in_without_lifecycle_calls():
    client = FakeSocket()
    lines = []

    run_observer(
        "http://127.0.0.1:5000",
        "credential-must-not-be-printed",
        socket_client=client,
        wait=lambda: (_ for _ in ()).throw(KeyboardInterrupt),
        write=lines.append,
    )

    assert client.connect_args == (
        "http://127.0.0.1:5000",
        {
            "token": "credential-must-not-be-printed",
            "observe_sensor_data": True,
        },
        ["polling"],
    )
    assert client.disconnected is True
    assert "cross_validation_observation" in client.handlers
    assert all("credential-must-not-be-printed" not in line for line in lines)
