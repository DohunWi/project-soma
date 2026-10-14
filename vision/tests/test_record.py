"""Guided EAR dataset recording helpers, without camera hardware."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))

SPEC = importlib.util.spec_from_file_location(
    "vision_eval_record",
    ROOT / "vision" / "eval" / "record.py",
)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def protocol():
    return record.GuidedProtocol(
        open_sec=2.0,
        blink_count=2,
        cue_interval_sec=3.0,
        blink_window_sec=0.5,
        closed_hold_sec=2.0,
        recovery_open_sec=2.0,
    )


def test_guided_sequence_labels_open_blinks_hold_and_recovery():
    guided = protocol()

    assert guided.label_at(0.0) == record.LABEL_OPEN
    assert guided.label_at(2.1) == record.LABEL_BLINK
    assert guided.label_at(2.6) == record.LABEL_OPEN
    assert guided.label_at(5.1) == record.LABEL_BLINK
    assert guided.label_at(8.1) == record.LABEL_CLOSED_HOLD
    assert guided.label_at(10.1) == record.LABEL_OPEN
    assert guided.total_sec == 12.0


def test_guided_cues_are_not_lost_when_one_frame_crosses_multiple_cues():
    guided = protocol()

    assert guided.due_cues(1.9, 5.1) == ((1, 2.0), (2, 5.0))
    assert guided.due_cues(5.1, 7.0) == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"blink_count": 0},
        {"open_sec": 0},
        {"cue_interval_sec": 0.5, "blink_window_sec": 0.5},
    ],
)
def test_invalid_guided_protocol_is_rejected(kwargs):
    with pytest.raises(ValueError):
        record.GuidedProtocol(**kwargs)


def test_frame_record_contains_labeled_left_right_and_combined_ear():
    row = record.frame_record(
        now=1725345600.1234,
        label=record.LABEL_BLINK,
        left_ear=0.123456,
        right_ear=0.234567,
        combined_ear=0.1790115,
        detected=True,
        frontal=True,
        width_px=182.345,
    )

    assert row == {
        "type": "frame",
        "t": 1725345600.1234,
        "timestamp": "2024-09-03T06:40:00.123Z",
        "left_ear": 0.12346,
        "right_ear": 0.23457,
        "combined_ear": 0.17901,
        "ear": 0.17901,
        "label": "BLINK",
        "face_detected": True,
        "frontal": True,
        "face_width_px": 182.34,
    }
    assert json.loads(json.dumps(row)) == row


def test_missing_face_keeps_metrics_null_and_marks_detection_false():
    row = record.frame_record(now=1000.0, label=record.LABEL_OPEN)

    assert row["face_detected"] is False
    assert row["frontal"] is None
    assert row["left_ear"] is None
    assert row["right_ear"] is None
    assert row["combined_ear"] is None
    assert row["ear"] is None


def test_output_filename_separates_lighting_conditions():
    off = record.output_path("S01", "light-off", 1725345600.0)
    on = record.output_path("S01", "light-on", 1725345600.0)

    assert off.name == "S01_light-off_20240903T064000Z.jsonl"
    assert on.name == "S01_light-on_20240903T064000Z.jsonl"
    assert off != on
    assert off.parent == record.OUT_DIR


def test_output_filename_sanitizes_external_metadata():
    path = record.output_path("../S 01", "../../dark room", 1725345600.0)

    assert path.parent == record.OUT_DIR
    assert path.name == "S-01_dark-room_20240903T064000Z.jsonl"
