import io
import json

import pytest

from fusion.state import STATIC_EPS, _activity
from tools.chair_activity_diagnostic import (
    ActivityRecorder,
    STAGES,
    activity_value,
    analyze_rows,
    chair_payload_parts,
    pressure_values,
    run_guided,
)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def observation(pressure, *, t=1.0, static_hold=0.0):
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
            "state": "NORMAL",
            "score": 100,
            "metrics": {"seated": True, "static_hold_sec": static_hold},
        },
    }


def saved_rows(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def test_activity_matches_fusion_implementation_and_boundary():
    previous = (100, 110, 120, 130)
    current = (105, 103, 129, 126)
    assert activity_value(current, previous) == _activity(current, previous) == 25
    assert activity_value(current, previous) <= STATIC_EPS
    assert activity_value((106, 103, 129, 126), previous) > STATIC_EPS


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"pressure": None},
        {"pressure": [1, 2, 3]},
        {"pressure": [1, 2, 3, "4"]},
        {"pressure": [1, 2, 3, float("nan")]},
    ],
)
def test_invalid_pressure_shape_is_rejected(payload):
    assert pressure_values(payload) is None


def test_first_recorded_sample_is_not_counted_as_movement():
    sink = io.StringIO()
    recorder = ActivityRecorder(sink, monotonic=Clock())
    recorder.start_stage("STILL")
    recorder.record(observation([100, 100, 100, 100]))
    row = saved_rows(sink)[0]
    assert row["activity"] is None
    assert row["activity_exceeds_static_eps"] is None


def test_extracts_actual_backend_cross_validation_observation_shape():
    payload = observation([900, 700, 1000, 910], t=1000.0)
    chair_payload, chair = chair_payload_parts(payload)
    assert chair_payload["source"] == "chair"
    assert pressure_values(chair) == (900, 700, 1000, 910)

    sink = io.StringIO()
    recorder = ActivityRecorder(sink, monotonic=Clock())
    recorder.start_stage("STILL")
    recorder.record(payload)
    row = saved_rows(sink)[0]
    assert row["timestamp"] == 1000.0
    assert row["valid_pressure"] is True
    assert (row["fl"], row["fr"], row["bl"], row["br"]) == (
        900,
        700,
        1000,
        910,
    )


def test_invalid_row_includes_credential_safe_shape_diagnostics():
    sink = io.StringIO()
    recorder = ActivityRecorder(sink, monotonic=Clock())
    recorder.start_stage("STILL")
    recorder.record(observation([1, 2, 3]))
    row = saved_rows(sink)[0]
    assert row["valid_pressure"] is False
    assert row["observed_shape"] == {
        "chair_payload_type": "dict",
        "chair_payload_keys": [
            "chair",
            "device_id",
            "source",
            "t",
            "user_name",
            "v",
        ],
        "chair_fields_type": "dict",
        "chair_fields_keys": ["ir", "pressure"],
        "pressure_type": "list",
        "pressure_length": 3,
        "pressure_value_types": ["int", "int", "int"],
    }
    serialized = json.dumps(row)
    assert "token" not in serialized.lower()
    assert "authorization" not in serialized.lower()


def test_prepare_updates_baseline_but_writes_nothing():
    sink = io.StringIO()
    clock = Clock()
    recorder = ActivityRecorder(sink, monotonic=clock)
    recorder.record(observation([100, 100, 100, 100], static_hold=5))
    assert sink.getvalue() == ""
    clock.value = 1
    recorder.start_stage("STILL")
    recorder.record(observation([101, 102, 103, 104], static_hold=6))
    row = saved_rows(sink)[0]
    assert row["activity"] == 10
    assert row["static_hold_previous_sec"] == 5


def test_invalid_sample_is_preserved_without_corrupting_pressure_baseline():
    sink = io.StringIO()
    clock = Clock()
    recorder = ActivityRecorder(sink, monotonic=clock)
    recorder.start_stage("STILL")
    recorder.record(observation([100, 100, 100, 100]))
    clock.value = 1
    recorder.record(observation([1, 2, 3]))
    clock.value = 2
    recorder.record(observation([101, 101, 101, 101]))
    rows = saved_rows(sink)
    assert rows[1]["valid_pressure"] is False
    assert rows[2]["activity"] == 4


def test_analysis_reports_distribution_gaps_and_timer_reset_relation():
    sink = io.StringIO()
    clock = Clock()
    recorder = ActivityRecorder(sink, monotonic=clock)
    recorder.record(observation([100, 100, 100, 100], static_hold=8))
    recorder.start_stage("STILL")
    for at, pressure, static_hold in (
        (1, [101, 101, 101, 101], 9),
        (2, [111, 111, 111, 111], 0),
        (3, [112, 112, 112, 112], 1),
        (7, [113, 113, 113, 113], 2),
    ):
        clock.value = at
        recorder.record(observation(pressure, t=at, static_hold=static_hold))
    stats = analyze_rows(saved_rows(sink))["stages"]["STILL"]
    assert stats["activity"]["median"] == 4
    assert stats["activity"]["max"] == 40
    assert stats["activity_over_threshold_count"] == 1
    assert stats["activity_over_threshold_ratio"] == 0.25
    assert stats["sensor_gap_count"] == 1
    assert stats["static_hold_reset_count"] == 1
    assert stats["over_threshold_with_static_reset_count"] == 1


def test_interrupted_guided_run_keeps_flushed_rows():
    sink = io.StringIO()
    recorder = ActivityRecorder(sink, monotonic=Clock())

    def sleep(_seconds):
        recorder.record(observation([100, 100, 100, 100]))
        raise KeyboardInterrupt

    completed = run_guided(
        recorder,
        record_sec=60,
        read=lambda _prompt: "",
        sleep=sleep,
        write=lambda _line: None,
    )
    assert completed == []
    assert len(saved_rows(sink)) == 1
    assert saved_rows(sink)[0]["stage"] == "STILL"


def test_all_three_stages_complete_without_hardware():
    sink = io.StringIO()
    clock = Clock()
    recorder = ActivityRecorder(sink, monotonic=clock)

    def sleep(_seconds):
        recorder.record(observation([100, 100, 100, 100]))
        clock.value += 1

    completed = run_guided(
        recorder,
        record_sec=1,
        read=lambda _prompt: "",
        sleep=sleep,
        write=lambda _line: None,
    )
    assert completed == [label for label, _ in STAGES]
    assert [row["stage"] for row in saved_rows(sink)] == completed


def test_q_aborts_before_recording():
    recorder = ActivityRecorder(io.StringIO(), monotonic=Clock())
    assert run_guided(
        recorder,
        record_sec=1,
        read=lambda _prompt: "q",
        sleep=lambda _seconds: None,
        write=lambda _line: None,
    ) == []
