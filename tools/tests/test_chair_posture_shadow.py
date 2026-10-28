import base64
import io
import json

import pytest

from tools.chair_posture_shadow import (
    IR_OUT_OF_RANGE_MM,
    ShadowConnectionError,
    ShadowLogRecorder,
    analyze_rows,
    extract_chair_ir,
    inspect_chair_ir,
    main,
    replay_file,
    run_live_observer,
    write_summary,
)
from tools.posture_shadow import PostureShadowObserver, ShadowConfig, ShadowMode
from tools.posture_ir_shadow import (
    PressureIrPostureShadowObserver,
    PressureIrShadowConfig,
    PressureIrShadowMode,
)


def observe_config():
    return ShadowConfig(
        shadow_mode=ShadowMode.OBSERVE_ONLY,
        shadow_anchor_samples=3,
        shadow_anchor_stability_radius=0.02,
    )


def experimental_config():
    return ShadowConfig(
        shadow_mode=ShadowMode.EXPERIMENTAL,
        shadow_anchor_samples=3,
        shadow_anchor_stability_radius=0.02,
        experimental_candidate_distance=0.2,
        experimental_candidate_sec=2.0,
        experimental_candidate_radius=0.02,
    )


def backend_observation(t, pressure, *, seated=True, static_hold=3.0):
    return {
        "chair": {
            "v": 1,
            "t": t,
            "source": "chair",
            "device_id": "chair-test",
            "user_name": "guest",
            "chair": {"pressure": pressure, "ir": [250]},
        },
        "state": {
            "state": "NORMAL" if seated else "ABSENT",
            "score": 100,
            "metrics": {"seated": seated, "static_hold_sec": static_hold},
        },
    }


def decoded(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


class FakeSocket:
    def __init__(self, *, fail=False, phase=None, observation=None):
        self.connected = False
        self.handlers = {}
        self.connect_args = None
        self.disconnected = False
        self.fail = fail
        self.phase = phase
        self.observation = observation

    def on(self, name, handler):
        self.handlers[name] = handler

    def connect(self, url, auth, transports):
        self.connect_args = (url, auth, transports)
        if self.fail:
            self.handlers["connect_error"]({"message": "rejected"})
            raise RuntimeError(f"rejected token={auth['token']}")
        self.connected = True
        self.handlers["connect"]()
        if self.phase is not None:
            self.handlers["measurement_phase"](self.phase)
        if self.observation is not None:
            self.handlers["cross_validation_observation"](self.observation)

    def disconnect(self):
        self.connected = False
        self.disconnected = True


def test_live_recorder_uses_backend_shape_and_phase_session_reset():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    phase = recorder.handle_phase(
        {"active": True, "phase": "MEASURING", "session_id": "session-a"}
    )
    sample = recorder.record_observation(
        backend_observation(1000.0, [250, 250, 250, 250])
    )
    assert phase["shadow_reset"] is True
    assert sample["pressure"] == [250, 250, 250, 250]
    assert [sample[name] for name in ("fl", "fr", "bl", "br")] == [
        250,
        250,
        250,
        250,
    ]
    assert sample["pressure_sum"] == 1000
    assert sample["left_right_ratio"] == pytest.approx(0.0)
    assert sample["front_back_ratio"] == pytest.approx(0.0)
    assert sample["chair_distance_mm"] == 250
    assert sample["chair_ir_status"] == "valid"
    assert sample["chair_ir_analysis_usable"] is True
    assert sample["chair_ir_delta_mm"] is None
    assert sample["pressure_ir_shared_payload_timestamp"] is True
    assert sample["pressure_ir_sensor_offset_sec"] is None
    assert sample["shadow_state"] == "UNINITIALIZED"
    assert sample["pressure_ir_shadow"]["mode"] == "observe-only"
    assert sample["pressure_ir_shadow"]["state"] == "UNINITIALIZED"
    assert sample["shadow"]["session_id"] == "session-a"
    assert sample["shadow"]["shadow_state"] == "UNINITIALIZED"
    assert sample["session_id"] == "session-a"
    assert sample["measurement_phase"] == "MEASURING"


def test_live_recorder_preserves_integrated_observer_diagnostics():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    recorder.handle_phase(
        {"active": True, "phase": "MEASURING", "session_id": "session-r22"}
    )
    observation = backend_observation(1000.0, [250, 250, 250, 250])
    observation.update(
        {
            "vision": {
                "t": 999.9,
                "source": "vision",
                "vision": {
                    "face_detected": True,
                    "blink": True,
                    "blink_rate": 9.0,
                    "blink_count": 9,
                    "window_sec": 60.0,
                    "blink_duration_ms": 182.5,
                    "detect_rate": 1.0,
                    "yaw_dropped_rate": 0.25,
                    "ear_threshold_mode": "personalized",
                    "open_ear_baseline": 0.24,
                    "ear_calibration_status": "loaded_personalized",
                    "face_distance_cm": 58.5,
                },
            },
            "vision_availability": "FRESH",
            "vision_receipt_age_sec": 0.1,
            "chair_vision_sender_delta_sec": 0.1,
            "posture_tracking": {"mode": "clock", "canonical_event": None},
            "static_burden": {"policy": "cumulative-v2", "penalty": 2.0},
            "blink_policy": {"mode": "score-v1", "penalty": 1.0},
            "distance_evidence": {"classification": "NORMAL"},
            "distance_temporal": {"active": False},
            "penalties": {
                "static": 2.0,
                "balance": 3.0,
                "blink": 1.0,
                "distance": 4.0,
                "total": 10.0,
            },
        }
    )
    observation["state"].update(
        {"score": 90, "confidence": 0.9, "reasons": ["static_hold"]}
    )

    sample = recorder.record_observation(observation)

    assert sample["timestamp"] == 1000.0
    assert sample["vision"] == {
        "timestamp": 999.9,
        "availability": "FRESH",
        "receipt_age_sec": 0.1,
        "chair_sender_delta_sec": 0.1,
        "face_detected": True,
        "blink": True,
        "blink_rate": 9.0,
        "blink_count": 9,
        "window_sec": 60.0,
        "blink_duration_ms": 182.5,
        "detect_rate": 1.0,
        "yaw_dropped_rate": 0.25,
        "ear_threshold_mode": "personalized",
        "open_ear_baseline": 0.24,
        "ear_calibration_status": "loaded_personalized",
        "face_distance_cm": 58.5,
    }
    assert sample["posture_tracking"]["mode"] == "clock"
    assert sample["static_burden"]["policy"] == "cumulative-v2"
    assert sample["blink_policy"]["mode"] == "score-v1"
    assert sample["distance_evidence"]["classification"] == "NORMAL"
    assert sample["distance_temporal"]["active"] is False
    assert sample["penalties"]["total"] == 10.0
    assert sample["fusion"] == {
        "timestamp": None,
        "state": "NORMAL",
        "score": 90,
        "confidence": 0.9,
        "reasons": ["static_hold"],
        "metrics": {"seated": True, "static_hold_sec": 3.0},
    }


def test_ir_delta_uses_consecutive_seated_samples_and_resets_with_session():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    recorder.handle_phase(
        {"active": True, "phase": "MEASURING", "session_id": "session-a"}
    )
    recorder.record_observation(backend_observation(1000.0, [250] * 4))
    second = backend_observation(1001.0, [250] * 4)
    second["chair"]["chair"]["ir"] = [310]
    delta = recorder.record_observation(second)
    assert delta["chair_ir_delta_mm"] == 60
    assert delta["chair_ir_abs_delta_mm"] == 60
    assert delta["chair_sample_interval_sec"] == pytest.approx(1.0)

    recorder.handle_phase(
        {"active": True, "phase": "MEASURING", "session_id": "session-b"}
    )
    after_reset = recorder.record_observation(
        backend_observation(1002.0, [250] * 4)
    )
    assert after_reset["chair_ir_delta_mm"] is None
    assert after_reset["chair_sample_interval_sec"] is None


@pytest.mark.parametrize(
    ("ir", "status", "valid"),
    [
        (None, "null", False),
        (0, "zero", False),
        (-1, "unavailable", False),
        (-2, "invalid_negative", False),
        (IR_OUT_OF_RANGE_MM, "out_of_range_sentinel", False),
        (40, "valid", True),
    ],
)
def test_ir_diagnostics_separate_invalid_values_without_posture_decision(
    ir,
    status,
    valid,
):
    result = inspect_chair_ir(ir, seated=True, previous_valid_mm=35)
    assert result["chair_ir_status"] == status
    assert result["chair_ir_analysis_valid"] is valid
    assert result["chair_ir_analysis_usable"] is valid
    assert result["chair_ir_delta_mm"] == (5 if valid else None)


def test_missing_ir_shape_is_reported_without_values_or_credentials():
    value, shape, status = extract_chair_ir({"pressure": [1, 2, 3, 4]})
    assert value is None
    assert status == "missing"
    assert shape == {
        "ir_type": "NoneType",
        "ir_length": None,
        "ir_value_type": None,
    }


def test_confirmed_result_writes_separate_shadow_event():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(experimental_config()))
    recorder.handle_phase(
        {"active": True, "phase": "MEASURING", "session_id": "session-a"}
    )
    for t in range(3):
        recorder.record_observation(backend_observation(t, [250, 250, 250, 250]))
    for t in range(3, 6):
        recorder.record_observation(backend_observation(t, [400, 100, 400, 100]))
    rows = decoded(sink)
    events = [row for row in rows if row["record_type"] == "shadow_event"]
    assert len(events) == 1
    assert events[0]["event"]["type"] == "posture_change_confirmed"


def test_r19_transition_jsonl_replays_without_waiting_or_using_stage_as_input(tmp_path):
    source = tmp_path / "r19-transition.jsonl"
    lines = [
        {
            "record_type": "stage_transition",
            "from_stage": None,
            "to_stage": "CENTER",
            "experiment_elapsed_sec": 0.0,
        },
    ]
    for index, pressure in enumerate(
        ([250, 250, 250, 250], [251, 249, 250, 250], [250, 250, 250, 250])
    ):
        lines.append(
            {
                "record_type": "sample",
                "timestamp": 1000.0 + index,
                "stage": "CENTER",
                "valid_pressure": True,
                "fl": pressure[0],
                "fr": pressure[1],
                "bl": pressure[2],
                "br": pressure[3],
                "fusion_seated": True,
                "static_hold_sec": float(index),
            }
        )
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in lines),
        encoding="utf-8",
    )
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    count = replay_file(source, recorder)
    rows = decoded(sink)
    samples = [row for row in rows if row["record_type"] == "shadow_sample"]
    assert count == 3
    assert samples[-1]["shadow"]["shadow_state"] == "ANCHOR_READY"
    assert samples[-1]["source_stage"] == "CENTER"
    assert samples[-1]["shadow"]["shadow_mode"] == "observe-only"


def test_r19_5a_shadow_jsonl_replays_pressure_and_ir_into_experimental_observer(
    tmp_path,
):
    source = tmp_path / "r19-5a.jsonl"
    rows = []
    for index, (pressure, ir) in enumerate(
        [
            ([250, 250, 250, 250], 50),
            ([250, 250, 250, 250], 51),
            ([250, 250, 250, 250], 49),
            ([252, 252, 248, 248], 150),
            ([252, 252, 248, 248], 151),
            ([252, 252, 248, 248], 149),
        ]
    ):
        rows.append(
            {
                "record_type": "shadow_sample",
                "timestamp": 1000.0 + index,
                "seated": True,
                "valid_pressure": True,
                "pressure": pressure,
                "chair_distance_mm": ir,
                "shadow": {"static_hold_sec": index},
            }
        )
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    pressure_ir_config = PressureIrShadowConfig(
        mode=PressureIrShadowMode.EXPERIMENTAL,
        anchor_samples=3,
        candidate_sec=2,
    )
    sink = io.StringIO()
    recorder = ShadowLogRecorder(
        sink,
        PostureShadowObserver(observe_config()),
        PressureIrPostureShadowObserver(pressure_ir_config),
    )
    count = replay_file(source, recorder)
    output = decoded(sink)
    samples = [row for row in output if row["record_type"] == "shadow_sample"]
    events = [
        row
        for row in output
        if row["record_type"] == "pressure_ir_shadow_event"
    ]
    assert count == 6
    assert samples[-1]["pressure_ir_shadow"]["state"] == "FORWARD_CONFIRMED"
    assert any(
        row["event"]["type"] == "forward_change_confirmed" for row in events
    )


def test_summary_reports_states_events_and_stage_labels_only_for_evaluation(tmp_path):
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    recorder.session_id = "offline"
    recorder.observer.set_session("offline")
    for index, pressure in enumerate(
        ([250, 250, 250, 250], [251, 249, 250, 250], [250, 250, 250, 250])
    ):
        recorder.record_sample(
            timestamp=1000 + index,
            pressure=pressure,
            seated=True,
            static_hold_sec=index,
            source_stage="CENTER",
            chair_ir=40 + index,
        )
    summary = analyze_rows(decoded(sink))
    assert summary["samples"] == 3
    assert summary["shadow_states"]["ANCHOR_READY"] == 1
    assert summary["source_stage_summary"]["CENTER"]["samples"] == 3
    assert summary["chair_ir"]["analysis_usable_samples"] == 3
    assert summary["chair_ir"]["chair_distance_mm"]["median"] == 41
    assert summary["chair_ir"]["chair_ir_abs_delta_mm"]["max"] == 1
    assert summary["pressure_ir_timing"] == {
        "shared_payload_timestamp": True,
        "per_sensor_timestamp_available": False,
    }
    assert summary["stage_labels_used_by_observer"] is False
    assert summary["production_behavior_changed"] is False
    path = write_summary(tmp_path / "shadow.jsonl", summary)
    assert json.loads(path.read_text(encoding="utf-8")) == summary


def test_live_connection_matches_existing_polling_auth_and_reports_no_session():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    client = FakeSocket()
    lines = []
    status = run_live_observer(
        client,
        recorder,
        url="http://127.0.0.1:5000",
        token="secret-token",
        duration=0,
        write=lines.append,
    )
    assert client.connect_args == (
        "http://127.0.0.1:5000",
        {"token": "secret-token", "observe_sensor_data": True},
        ["polling"],
    )
    assert status.namespace_connected is True
    assert status.observation_count == 0
    assert any("phase=UNSEEN" in line for line in lines)
    assert client.disconnected is True


def test_measuring_without_observation_is_reported_after_successful_subscription():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    client = FakeSocket(
        phase={
            "active": True,
            "phase": "MEASURING",
            "session_id": "session-a",
        }
    )
    lines = []
    status = run_live_observer(
        client,
        recorder,
        url="http://127.0.0.1:5000",
        token="secret-token",
        duration=0,
        write=lines.append,
    )
    assert status.latest_phase == "MEASURING"
    assert any("verify Chair sensor ticks" in line for line in lines)


def test_received_observation_is_counted_separately_from_connection():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    client = FakeSocket(
        phase={
            "active": True,
            "phase": "MEASURING",
            "session_id": "session-a",
        },
        observation=backend_observation(1000.0, [250, 250, 250, 250]),
    )
    lines = []
    status = run_live_observer(
        client,
        recorder,
        url="http://127.0.0.1:5000",
        token="secret-token",
        duration=0,
        write=lines.append,
    )
    assert status.observation_count == 1
    assert any("status=received count=1" in line for line in lines)


def test_namespace_rejection_reports_stage_without_token_or_server_payload():
    sink = io.StringIO()
    recorder = ShadowLogRecorder(sink, PostureShadowObserver(observe_config()))
    client = FakeSocket(fail=True)
    token = "must-not-leak"
    with pytest.raises(ShadowConnectionError) as captured:
        run_live_observer(
            client,
            recorder,
            url="http://127.0.0.1:5000",
            token=token,
            duration=0,
            write=lambda _line: None,
        )
    message = str(captured.value)
    assert "stage=socketio_namespace_auth_or_room_setup" in message
    assert "error=RuntimeError" in message
    assert token not in message
    assert "rejected" not in message


def test_expired_token_preflight_stops_before_output_or_connection(
    tmp_path,
    monkeypatch,
    capsys,
):
    token = jwt({"exp": 1, "sub": "must-not-leak"})
    output = tmp_path / "must-not-exist.jsonl"
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", token)
    result = main(["--duration", "180", "--output", str(output)])
    captured = capsys.readouterr()
    assert result == 2
    assert "authentication preflight failed" in captured.err
    assert "has expired" in captured.err
    assert token not in captured.err
    assert "must-not-leak" not in captured.err
    assert output.exists() is False
