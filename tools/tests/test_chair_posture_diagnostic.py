import io
import json

import pytest

from tools.chair_posture_diagnostic import (
    PostureRecorder,
    STAGES,
    analyze_rows,
    pressure_distribution,
    run_guided,
)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def observation(pressure, *, t=1000.0, static_hold=5.0):
    return {
        "v": 1,
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


def record_stage(recorder, clock, label, pressures):
    recorder.start_stage(label)
    for index, pressure in enumerate(pressures):
        clock.value += 1
        recorder.record(observation(pressure, t=1000 + clock.value + index))
    recorder.stop_stage()


def test_real_backend_nested_observation_records_four_channels():
    sink = io.StringIO()
    recorder = PostureRecorder(sink, monotonic=Clock())
    recorder.start_stage("CENTER")
    recorder.record(observation([300, 100, 200, 400]))
    row = saved_rows(sink)[0]
    assert row["timestamp"] == 1000.0
    assert row["valid_pressure"] is True
    assert (row["fl"], row["fr"], row["bl"], row["br"]) == (300, 100, 200, 400)
    assert row["fusion_state"] == "NORMAL"
    assert row["fusion_seated"] is True
    assert row["static_hold_sec"] == 5.0


def test_pressure_shares_and_normalized_axes():
    result = pressure_distribution((300, 100, 200, 400))
    assert result["valid"] is True
    assert result["pressure_sum"] == 1000
    assert result["channel_share"] == {
        "fl": 0.3,
        "fr": 0.1,
        "bl": 0.2,
        "br": 0.4,
    }
    assert result["left_right_ratio"] == pytest.approx(0.0)
    assert result["front_back_ratio"] == pytest.approx(-0.2)


def test_zero_sum_keeps_pressure_valid_but_invalidates_ratios():
    sink = io.StringIO()
    recorder = PostureRecorder(sink, monotonic=Clock())
    recorder.start_stage("CENTER")
    recorder.record(observation([0, 0, 0, 0]))
    row = saved_rows(sink)[0]
    assert row["valid_pressure"] is True
    assert row["valid_distribution"] is False
    assert row["distribution_invalid_reason"] == "pressure_sum_must_be_positive"
    assert row["channel_share"] is None
    assert row["left_right_ratio"] is None
    assert row["front_back_ratio"] is None


@pytest.mark.parametrize("pressure", [None, [1, 2, 3], [1, 2, 3, "4"]])
def test_invalid_pressure_is_preserved_with_safe_shape(pressure):
    sink = io.StringIO()
    recorder = PostureRecorder(sink, monotonic=Clock())
    recorder.start_stage("CENTER")
    recorder.record(observation(pressure))
    row = saved_rows(sink)[0]
    assert row["valid_pressure"] is False
    assert row["invalid_reason"] == "pressure_must_have_four_finite_numbers"
    serialized = json.dumps(row)
    assert "token" not in serialized.lower()
    assert "authorization" not in serialized.lower()


def test_prepare_frames_are_not_written_and_first_activity_is_null():
    sink = io.StringIO()
    recorder = PostureRecorder(sink, monotonic=Clock())
    recorder.record(observation([250, 250, 250, 250]))
    assert sink.getvalue() == ""
    recorder.start_stage("CENTER")
    recorder.record(observation([300, 200, 300, 200]))
    assert saved_rows(sink)[0]["activity"] is None


def test_stage_statistics_and_center_comparisons_are_descriptive():
    sink = io.StringIO()
    clock = Clock()
    recorder = PostureRecorder(sink, monotonic=clock)
    center = [[250, 250, 250, 250], [252, 248, 251, 249], [249, 251, 248, 252]]
    left = [[400, 100, 400, 100], [398, 102, 402, 98], [401, 99, 399, 101]]
    right = [[100, 400, 100, 400], [102, 398, 98, 402], [99, 401, 101, 399]]
    forward = [[400, 400, 100, 100], [398, 402, 102, 98], [401, 399, 99, 101]]
    back = [[100, 100, 400, 400], [102, 98, 398, 402], [99, 101, 401, 399]]
    for label, values in (
        ("CENTER", center),
        ("LEFT", left),
        ("RIGHT", right),
        ("FORWARD", forward),
        ("BACK", back),
        ("RETURN", center),
    ):
        record_stage(recorder, clock, label, values)
    summary = analyze_rows(saved_rows(sink))
    assert summary["classification_thresholds_selected"] is False
    assert summary["stages"]["CENTER"]["valid_samples"] == 3
    assert summary["stages"]["CENTER"]["pressure_sum_median"] == 1000
    assert summary["stages"]["LEFT"]["left_right_ratio"]["median"] == pytest.approx(0.6)
    assert summary["stages"]["FORWARD"]["front_back_ratio"]["median"] == pytest.approx(0.6)
    assert summary["center_comparisons"]["LEFT"]["channel_share_centroid_distance"] > 0
    assert summary["center_return_similarity"]["channel_share_centroid_distance"] == 0
    assert summary["center_comparisons"]["LEFT"]["robust_p95_regions_overlap"] is False


def test_six_stage_guided_sequence_completes_without_hardware():
    sink = io.StringIO()
    clock = Clock()
    recorder = PostureRecorder(sink, monotonic=clock)

    def sleep(_seconds):
        recorder.record(observation([250, 250, 250, 250]))
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
