"""Hardware-free tests for the Chair/Vision cross-validation recorder."""

import base64
import json

import pytest

import tools.chair_vision_cross_validation as cross_validation
from tools.chair_vision_cross_validation import (
    FRESH_VISION,
    SCENARIOS,
    GuidedExperiment,
    JsonlSink,
    ObservationRecorder,
    STATE_COMPLETE,
    STATE_PREPARE,
    STATE_RECORDING,
    build_row,
    connect_observation_client,
    cross_validation_required_lifetime,
    report_run_result,
    run_guided,
    summarize_rows,
)
from tools.chair_e2e import ChairE2EError


TOKEN_NOW = 1_000.0


def _jwt(*, expires_at, subject="private-user-id"):
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expires_at, 'sub': subject})}.sig"


def observation(*, seated=True, balance="LEFT", availability="FRESH",
                direction="RIGHT", offset=-0.25):
    pressure = [500, 100, 500, 100] if seated else [1, 1, 1, 1]
    return {
        "v": 1,
        "chair": {
            "v": 1,
            "t": 1000.0,
            "source": "chair",
            "device_id": "chair-test",
            "user_name": "guest",
            "chair": {"pressure": pressure, "ir": [250]},
        },
        "vision": {
            "v": 1,
            "t": 999.9,
            "source": "vision",
            "user_name": "guest",
            "vision": {
                "face_lean_direction": direction,
                "face_lateral_offset": offset,
                "head_roll_delta_deg": 1.5,
                "face_detected": True,
                "detect_rate": 1.0,
                "yaw_dropped_rate": 0.0,
            },
        },
        "vision_availability": availability,
        "vision_receipt_age_sec": 0.1,
        "chair_vision_sender_delta_sec": 0.1,
        "state": {
            "state": "NORMAL" if seated else "ABSENT",
            "score": 98,
            "confidence": 0.9,
            "metrics": {
                "balance": balance if seated else "CENTER",
                "seated": seated,
                "static_hold_sec": 2.0,
            },
        },
    }


def row_for(payload, scenario=None):
    return build_row(
        payload,
        scenario=scenario or SCENARIOS[1],
        stage_index=1,
        experiment_started_at=10.0,
        received_at=1000.25,
        monotonic_now=12.5,
        scenario_started_at=11.0,
    )


def test_connection_uses_polling_and_preserves_opt_in_auth():
    calls = []

    class FakeSocket:
        def connect(self, url, **options):
            calls.append((url, options))

    connect_observation_client(
        FakeSocket(),
        "http://127.0.0.1:5000",
        "secret-token",
    )

    assert calls == [(
        "http://127.0.0.1:5000",
        {
            "auth": {
                "token": "secret-token",
                "observe_sensor_data": True,
            },
            "transports": ["polling"],
        },
    )]


def test_required_lifetime_uses_protocol_recording_prepare_and_shutdown_time():
    assert len(SCENARIOS) == 15
    assert cross_validation_required_lifetime(5.0) == 360.0
    assert cross_validation_required_lifetime(2.0, stage_count=3) == 111.0


def test_cli_preflight_rejects_before_measurement_start(
    monkeypatch,
    capsys,
):
    token = _jwt(
        expires_at=TOKEN_NOW + 359,
        subject="must-not-leak",
    )
    post_calls = []
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", token)
    monkeypatch.setattr(
        cross_validation,
        "post_measurement",
        lambda *_args, **_kwargs: post_calls.append("post"),
    )

    with pytest.raises(SystemExit):
        cross_validation.main([], token_now=lambda: TOKEN_NOW)

    output = capsys.readouterr()
    assert "required at least 360s" in output.err
    assert "remaining 359s" in output.err
    assert "copy a fresh access token" in output.err
    assert token not in output.err
    assert "must-not-leak" not in output.err
    assert post_calls == []


def test_completed_protocol_and_shutdown_auth_failure_are_distinct():
    output = []
    errors = []

    status = report_run_result(
        "recording.jsonl",
        75,
        STATE_COMPLETE,
        ChairE2EError("HTTP 401 (invalid_token)"),
        write=output.append,
        write_error=errors.append,
    )

    assert status == 1
    assert any("protocol result: COMPLETE (75 rows)" in line for line in output)
    assert any("shutdown HTTP result: FAILED" in line for line in errors)
    assert any("recording data remains valid" in line for line in errors)


def test_protocol_has_required_order_and_prepare_does_not_record(tmp_path):
    assert [scenario.label for scenario in SCENARIOS] == [
        "CENTER", "WEIGHT_LEFT", "CENTER", "WEIGHT_RIGHT", "CENTER",
        "UPPER_BODY_LEFT", "CENTER", "UPPER_BODY_RIGHT", "CENTER",
        "HEAD_TURN_LEFT", "CENTER", "HEAD_TURN_RIGHT", "CENTER",
        "STAND_UP", "CENTER",
    ]
    assert len(SCENARIOS) == 15
    assert (SCENARIOS[1].expected_chair, SCENARIOS[1].expected_vision) == (
        "LEFT", "ANY",
    )
    assert (SCENARIOS[5].expected_chair, SCENARIOS[5].expected_vision) == (
        "ANY", "LEFT",
    )
    workflow = GuidedExperiment(record_sec=1.0)
    sink = JsonlSink(tmp_path / "recording.jsonl")
    recorder = ObservationRecorder(
        workflow,
        sink,
        monotonic=lambda: 10.0,
        wall_time=lambda: 1000.0,
    )

    assert workflow.state == STATE_PREPARE
    assert recorder.record(observation()) is None
    assert (tmp_path / "recording.jsonl").read_text(encoding="utf-8") == ""
    sink.close()


def test_guided_confirm_and_all_stages_can_complete():
    workflow = GuidedExperiment(record_sec=1.0)
    now = 0.0
    for _scenario in SCENARIOS:
        assert workflow.confirm(now) is True
        assert workflow.state == STATE_RECORDING
        assert workflow.advance(now + 0.99) is False
        assert workflow.advance(now + 1.0) is True
        now += 2.0
    assert workflow.state == STATE_COMPLETE


def test_guided_enter_or_space_confirmation_starts_recording():
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class Sink:
        def write(self, _row):
            pass

    clock = Clock()
    workflow = GuidedExperiment(scenarios=(SCENARIOS[0],), record_sec=0.2)
    recorder = ObservationRecorder(
        workflow,
        Sink(),
        monotonic=clock.monotonic,
        wall_time=lambda: 1000.0,
    )
    lines = []

    result = run_guided(
        workflow,
        recorder,
        read=lambda: " ",
        write=lines.append,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result == STATE_COMPLETE
    assert any("[PREPARE" in line for line in lines)
    assert any("[RECORDING] CENTER" in line for line in lines)


def test_row_contains_raw_chair_vision_freshness_and_sender_timestamps():
    row = row_for(observation(direction="RIGHT"))

    assert row["timestamp"] == "1970-01-01T00:16:40.000Z"
    assert row["elapsed_sec"] == 2.5
    assert row["scenario_elapsed_sec"] == 1.5
    assert row["scenario_label"] == "WEIGHT_LEFT"
    assert row["chair"] == {
        "sender_t": 1000.0,
        "fl": 500,
        "fr": 100,
        "bl": 500,
        "br": 100,
        "ir": [250],
        "pressure_sum": 1200,
        "balance": "LEFT",
        "static_hold_sec": 2.0,
        "seated": True,
        "occupancy": "PRESENT",
    }
    assert row["vision"]["sender_t"] == 999.9
    assert row["vision"]["availability"] in FRESH_VISION
    assert row["vision"]["face_lean_direction"] == "RIGHT"
    assert row["vision"]["chair_sender_delta_sec"] == 0.1


def test_row_preserves_optional_observational_distance_evidence():
    payload = observation(direction="CENTER")
    payload["distance_evidence"] = {
        "classification": "BODY_FORWARD_CLOSE",
        "face_approach_active": True,
        "backrest_departure_active": True,
        "vision_available": True,
        "chair_ir_available": True,
        "seated": True,
        "reasons": ["face_approach", "backrest_departure"],
        "face_distance_cm": 43.9,
        "face_approach_delta_cm": 18.0,
        "backrest_departure_delta_mm": 128.5,
    }

    row = row_for(payload)

    assert row["distance_evidence"] == payload["distance_evidence"]
    assert "distance_evidence" not in row_for(observation())


def test_absent_and_all_direction_categories_are_preserved():
    absent = row_for(observation(seated=False, balance="CENTER"), SCENARIOS[13])
    assert absent["chair"]["occupancy"] == "ABSENT"
    assert absent["fusion"]["state"] == "ABSENT"

    for direction in ("CENTER", "LEFT", "RIGHT", "UNKNOWN"):
        row = row_for(observation(direction=direction))
        assert row["vision"]["face_lean_direction"] == direction


def test_row_preserves_optional_distance_temporal_without_modifying_fusion():
    payload = observation(direction="CENTER")
    payload["distance_temporal"] = {
        "instantaneous_classification": "BODY_FORWARD_CLOSE",
        "sustained_classification": "BODY_FORWARD_CLOSE",
        "candidate_classification": "BODY_FORWARD_CLOSE",
        "accumulated_sec": 10.0,
        "recovery_sec": 0.0,
        "active": True,
        "reasons": ["sustained"],
    }
    row = row_for(payload)
    assert row["distance_temporal"] == payload["distance_temporal"]
    assert row["fusion"] == row_for(observation(direction="CENTER"))["fusion"]
    assert "distance_temporal" not in row_for(observation())


def test_stale_or_unseen_vision_is_unknown_not_center():
    stale = row_for(observation(availability="STALE", direction="CENTER"))
    unseen_payload = observation(availability="UNSEEN")
    unseen_payload["vision"] = None
    unseen = row_for(unseen_payload)

    assert stale["vision"]["fresh"] is False
    assert stale["vision"]["availability"] == "STALE"
    assert stale["vision"]["face_lean_direction"] == "UNKNOWN"
    assert stale["vision"]["face_lateral_offset"] is None
    assert unseen["vision"]["availability"] == "UNSEEN"
    assert unseen["vision"]["face_lean_direction"] == "UNKNOWN"


def test_line_buffered_file_keeps_existing_rows_after_abort(tmp_path):
    path = tmp_path / "interrupted.jsonl"
    sink = JsonlSink(path)
    workflow = GuidedExperiment(record_sec=5.0)
    workflow.confirm(10.0)
    recorder = ObservationRecorder(
        workflow,
        sink,
        monotonic=lambda: 11.0,
        wall_time=lambda: 1000.25,
    )
    recorder.started_at = 9.0
    recorder.record(observation())
    workflow.abort()
    sink.close()

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(saved) == 1
    assert saved[0]["scenario_label"] == "CENTER"


def test_summary_keeps_unknown_and_unavailable_separate():
    rows = [
        row_for(observation(direction="LEFT", offset=0.25)),
        row_for(observation(direction="UNKNOWN", offset=None)),
        row_for(observation(availability="STALE", direction="CENTER")),
    ]
    summary = summarize_rows(rows)["WEIGHT_LEFT"]
    vision = {name: count for name, count, _percent in summary["vision"]}

    assert vision == {"LEFT": 1, "UNKNOWN": 1, "UNAVAILABLE": 1}
    assert summary["median_face_lateral_offset"] == 0.25
