"""Startup distance and OPEN EAR calibration tests without a camera."""

import json
import math

import pytest

from vision.calibrator import Calibrator, calibration_sample


def sample(open_ear=0.80, face_width_px=200.0, blink_rate=None):
    return {
        "face_width_px": face_width_px,
        "blink_rate": blink_rate,
        "open_ear": open_ear,
    }


def complete_calibration(calibrator, values, *, start=100.0, duration=3.0):
    step = duration / (len(values) - 1)
    for index, value in enumerate(values):
        calibrator.add_sample(sample(value), now=start + index * step)


def test_sufficient_valid_open_samples_create_median_baseline(tmp_path):
    path = tmp_path / "baseline.json"
    calibrator = Calibrator(
        path,
        min_valid_samples=5,
        duration_sec=3.0,
    )
    calibrator.start()

    complete_calibration(calibrator, [0.78, 0.80, 0.81, 0.82, 0.84])

    assert calibrator.is_done() is True
    assert calibrator.is_calibrating() is False
    assert calibrator.open_ear_baseline() == pytest.approx(0.81)
    assert calibrator.get_baseline()["open_ear_sample_count"] == 5
    assert path.exists()


def test_natural_blink_outlier_does_not_pull_down_median(tmp_path):
    calibrator = Calibrator(
        tmp_path / "baseline.json",
        min_valid_samples=7,
        duration_sec=3.0,
    )
    calibrator.start()

    complete_calibration(
        calibrator,
        [0.80, 0.81, 0.79, 0.08, 0.82, 0.80, 0.81],
    )

    assert calibrator.open_ear_baseline() == pytest.approx(0.80)


@pytest.mark.parametrize(
    "overrides",
    [
        {"face_detected": False},
        {"frontal": False},
        {"left_ear": math.nan},
        {"right_ear": math.inf},
        {"combined_ear": None},
    ],
)
def test_invalid_no_face_and_non_frontal_frames_are_excluded(overrides):
    values = {
        "face_detected": True,
        "frontal": True,
        "face_width_px": 200.0,
        "left_ear": 0.80,
        "right_ear": 0.82,
        "combined_ear": 0.81,
    }
    values.update(overrides)

    assert calibration_sample(**values) is None


def test_timer_starts_with_first_valid_sample_not_start_call(tmp_path):
    now = [10.0]
    calibrator = Calibrator(
        tmp_path / "baseline.json",
        clock=lambda: now[0],
        min_valid_samples=2,
        duration_sec=3.0,
    )
    calibrator.start()
    now[0] = 100.0

    assert calibrator.progress() == 0.0
    calibrator.tick()
    assert calibrator.is_calibrating() is True

    calibrator.add_sample(sample(), now=100.0)
    now[0] = 101.5
    assert calibrator.progress() == pytest.approx(0.5)


def test_calibration_diagnostics_do_not_pollute_jsonl_stdout(tmp_path, capsys):
    calibrator = Calibrator(
        tmp_path / "baseline.json",
        min_valid_samples=2,
        duration_sec=1.0,
    )
    calibrator.start()
    calibrator.add_sample(sample(), now=10.0)
    calibrator.add_sample(sample(), now=11.0)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[calib] 완료" in captured.err


def test_insufficient_valid_samples_do_not_succeed_or_save(tmp_path):
    path = tmp_path / "baseline.json"
    calibrator = Calibrator(
        path,
        min_valid_samples=5,
        duration_sec=3.0,
    )
    calibrator.start()
    calibrator.add_sample(sample(), now=100.0)
    calibrator.add_sample(sample(), now=101.0)

    calibrator.tick(now=103.0)

    assert calibrator.is_calibrating() is False
    assert calibrator.is_done() is False
    assert calibrator.open_ear_baseline() is None
    assert path.exists() is False


def test_baseline_json_save_and_load(tmp_path):
    path = tmp_path / "baseline.json"
    calibrator = Calibrator(path, min_valid_samples=3, duration_sec=1.0)
    calibrator.start()
    complete_calibration(calibrator, [0.75, 0.80, 0.85], duration=1.0)

    saved = json.loads(path.read_text(encoding="utf-8"))
    loaded = Calibrator(path)

    assert saved["open_ear_baseline"] == pytest.approx(0.80)
    assert saved["open_ear_sample_count"] == 3
    assert loaded.open_ear_baseline() == pytest.approx(0.80)


def test_legacy_baseline_without_open_ear_remains_usable_for_distance(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "face_width_px": 200.0,
                "blink_rate": 0.0,
                "calib_distance_cm": 60.0,
            }
        ),
        encoding="utf-8",
    )

    calibrator = Calibrator(path)

    assert calibrator.is_done() is True
    assert calibrator.open_ear_baseline() is None
    assert calibrator.distance_cm(240.0) == pytest.approx(50.0)


def test_failed_recalibration_keeps_existing_baseline(tmp_path):
    path = tmp_path / "baseline.json"
    original = {
        "face_width_px": 200.0,
        "blink_rate": 0.0,
        "open_ear_baseline": 0.80,
        "open_ear_sample_count": 30,
        "calib_distance_cm": 60.0,
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    calibrator = Calibrator(path, min_valid_samples=5, duration_sec=1.0)
    calibrator.start()
    calibrator.add_sample(sample(0.40), now=10.0)

    calibrator.tick(now=11.0)

    assert calibrator.is_done() is True
    assert calibrator.open_ear_baseline() == pytest.approx(0.80)
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_distance_calibration_regression(tmp_path):
    calibrator = Calibrator(
        tmp_path / "baseline.json",
        calib_distance_cm=60.0,
        min_valid_samples=3,
        duration_sec=1.0,
    )
    calibrator.start()
    for index, width in enumerate((198.0, 200.0, 202.0)):
        calibrator.add_sample(
            sample(0.80, face_width_px=width),
            now=10.0 + index * 0.5,
        )

    assert calibrator.baseline_distance_cm() == 60.0
    assert calibrator.distance_cm(240.0) == pytest.approx(50.0)
