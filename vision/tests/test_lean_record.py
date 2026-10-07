"""Pure guided lateral-recording helpers, without camera hardware."""

import importlib.util
import sys
from pathlib import Path

import pytest

from vision.lateral import FaceLateralGeometry
from vision.payload import vision_payload

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "vision" / "blink"))

SPEC = importlib.util.spec_from_file_location(
    "vision_eval_lean_record",
    ROOT / "vision" / "eval" / "lean_record.py",
)
lean_record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lean_record
SPEC.loader.exec_module(lean_record)


def workflow(
    record_sec=3.0,
    protocol_name=lean_record.PROTOCOL_DEFAULT,
    movement_sec=5.0,
):
    return lean_record.GuidedLeanRecorder(
        lean_record.LeanProtocol(
            record_sec=record_sec,
            name=protocol_name,
            movement_sec=movement_sec,
        )
    )


def row(recorder, now):
    return lean_record.recording_frame_record(
        recorder,
        now=now,
        face_detected=True,
        frontal=True,
        calibration_available=True,
        geometry=FaceLateralGeometry(0.55, 0.25, -3.25),
        offset=-0.2,
        roll_delta=-1.5,
    )


def test_guided_sequence_keeps_all_seven_stages_in_exact_order():
    protocol = lean_record.LeanProtocol()

    assert protocol.phases == (
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_USER_LEFT,
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_USER_RIGHT,
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_HEAD_TILT,
        lean_record.LABEL_TORSO_LEAN,
    )


def test_cli_defaults_to_existing_protocol_and_selects_lean_intensity():
    parser = lean_record.build_parser()

    default = parser.parse_args(["--subject", "S01"])
    intensity = parser.parse_args([
        "--subject", "S01", "--protocol", "lean-intensity",
    ])

    assert default.protocol == lean_record.PROTOCOL_DEFAULT
    assert intensity.protocol == lean_record.PROTOCOL_LEAN_INTENSITY


def test_cli_selects_lean_threshold_with_five_second_movement_default():
    args = lean_record.build_parser().parse_args([
        "--subject", "S01", "--protocol", "lean-threshold",
    ])

    assert args.protocol == lean_record.PROTOCOL_LEAN_THRESHOLD
    assert args.movement_sec == 5.0


def test_lean_intensity_protocol_has_exact_nine_stage_order():
    protocol = lean_record.LeanProtocol(name=lean_record.PROTOCOL_LEAN_INTENSITY)

    assert protocol.phases == (
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_USER_LEFT_MILD,
        lean_record.LABEL_USER_LEFT_MEDIUM,
        lean_record.LABEL_USER_LEFT_LARGE,
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_USER_RIGHT_MILD,
        lean_record.LABEL_USER_RIGHT_MEDIUM,
        lean_record.LABEL_USER_RIGHT_LARGE,
        lean_record.LABEL_NEUTRAL,
    )


def test_lean_threshold_protocol_has_exact_seven_stage_order():
    protocol = lean_record.LeanProtocol(name=lean_record.PROTOCOL_LEAN_THRESHOLD)

    assert protocol.phases == (
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_LEFT_BOUNDARY_OUT,
        lean_record.LABEL_LEFT_BOUNDARY_RETURN,
        lean_record.LABEL_NEUTRAL,
        lean_record.LABEL_RIGHT_BOUNDARY_OUT,
        lean_record.LABEL_RIGHT_BOUNDARY_RETURN,
        lean_record.LABEL_NEUTRAL,
    )
    assert protocol.duration_for(lean_record.LABEL_NEUTRAL) == 3.0
    assert protocol.duration_for(lean_record.LABEL_LEFT_BOUNDARY_OUT) == 5.0


def test_invalid_recording_duration_is_rejected():
    with pytest.raises(ValueError):
        lean_record.LeanProtocol(record_sec=0)


def test_prepare_frames_are_not_rows_and_time_does_not_auto_start_pose():
    recorder = workflow()

    assert row(recorder, 10.0) is None
    assert row(recorder, 1000.0) is None
    assert recorder.state == lean_record.STATE_PREPARE
    assert recorder.label == lean_record.LABEL_NEUTRAL


def test_space_confirms_and_only_current_label_is_written_during_window():
    recorder = workflow()

    assert lean_record.handle_key(recorder, 32, 10.0) == "confirm"
    first = row(recorder, 10.0)
    middle = row(recorder, 12.9)

    assert recorder.state == lean_record.STATE_RECORDING
    assert first["guided_label"] == lean_record.LABEL_NEUTRAL
    assert middle["guided_label"] == lean_record.LABEL_NEUTRAL
    assert first["workflow_state"] == lean_record.STATE_RECORDING


def test_recording_ends_at_duration_and_next_pose_returns_to_prepare():
    recorder = workflow(record_sec=3.0)
    recorder.confirm(10.0)

    assert row(recorder, 12.999) is not None
    assert row(recorder, 13.0) is None
    assert recorder.state == lean_record.STATE_PREPARE
    assert recorder.label == lean_record.LABEL_USER_LEFT
    assert row(recorder, 100.0) is None


def test_all_seven_confirmed_stages_complete_without_prepare_samples():
    recorder = workflow(record_sec=1.0)
    written_labels = []

    for stage_index, expected in enumerate(recorder.protocol.phases):
        start = stage_index * 10.0
        assert recorder.state == lean_record.STATE_PREPARE
        assert recorder.label == expected
        assert row(recorder, start) is None
        assert recorder.confirm(start)
        written_labels.append(row(recorder, start)["guided_label"])
        assert row(recorder, start + 1.0) is None

    assert tuple(written_labels) == recorder.protocol.phases
    assert recorder.state == lean_record.STATE_COMPLETE
    assert recorder.label is None


def test_all_nine_intensity_stages_retain_labels_and_complete():
    recorder = workflow(
        record_sec=1.0,
        protocol_name=lean_record.PROTOCOL_LEAN_INTENSITY,
    )
    written_labels = []

    for stage_index, expected in enumerate(recorder.protocol.phases):
        start = stage_index * 10.0
        assert recorder.state == lean_record.STATE_PREPARE
        assert recorder.label == expected
        assert row(recorder, start) is None
        assert lean_record.handle_key(recorder, 32, start) == "confirm"
        recorded = row(recorder, start + 0.5)
        assert recorded["guided_label"] == expected
        written_labels.append(recorded["guided_label"])
        assert row(recorder, start + 1.0) is None

    assert tuple(written_labels) == lean_record.LEAN_INTENSITY_PHASES
    assert recorder.state == lean_record.STATE_COMPLETE


def test_threshold_movement_window_keeps_every_transition_frame():
    recorder = workflow(
        record_sec=1.0,
        protocol_name=lean_record.PROTOCOL_LEAN_THRESHOLD,
        movement_sec=5.0,
    )
    evaluator = lean_record.FaceLeanClassifier()

    recorder.confirm(0.0)
    assert row(recorder, 1.0) is None  # First neutral stage ends.
    assert recorder.label == lean_record.LABEL_LEFT_BOUNDARY_OUT
    recorder.confirm(2.0)

    offsets = (0.05, 0.19, 0.20, 0.25, 0.12)
    rows = []
    for index, offset in enumerate(offsets):
        rows.append(lean_record.recording_frame_record(
            recorder,
            now=2.0 + index,
            evaluator=evaluator,
            face_detected=True,
            frontal=True,
            calibration_available=True,
            geometry=FaceLateralGeometry(0.55, 0.25, 0.0),
            offset=offset,
            roll_delta=0.0,
        ))

    assert [sample["face_lateral_offset"] for sample in rows] == list(offsets)
    assert {sample["guided_label"] for sample in rows} == {
        lean_record.LABEL_LEFT_BOUNDARY_OUT
    }
    assert [sample["candidate_lean_state"] for sample in rows] == [
        "CENTER", "CENTER", "LEFT", "LEFT", "LEFT"
    ]


def test_all_seven_threshold_stages_can_complete_and_abort_stays_safe():
    recorder = workflow(
        record_sec=0.5,
        protocol_name=lean_record.PROTOCOL_LEAN_THRESHOLD,
        movement_sec=0.5,
    )

    for stage_index, expected in enumerate(recorder.protocol.phases):
        start = stage_index * 10.0
        assert recorder.label == expected
        assert recorder.confirm(start)
        assert row(recorder, start + 0.25)["guided_label"] == expected
        assert row(recorder, start + 0.5) is None

    assert recorder.state == lean_record.STATE_COMPLETE

    aborted = workflow(protocol_name=lean_record.PROTOCOL_LEAN_THRESHOLD)
    assert lean_record.handle_key(aborted, ord("q"), 0.0) == "abort"
    assert row(aborted, 1.0) is None


def test_candidate_hysteresis_exact_boundaries_and_no_direct_side_jump():
    evaluator = lean_record.FaceLeanClassifier()

    assert evaluator.update(0.199).direction == "CENTER"
    entered_left = evaluator.update(0.20)
    assert entered_left.direction == "LEFT"
    assert entered_left.transitioned is True
    assert evaluator.update(0.101).direction == "LEFT"
    released_left = evaluator.update(0.10)
    assert released_left.direction == "CENTER"
    assert released_left.transitioned is True

    entered_right = evaluator.update(-0.15)
    assert entered_right.direction == "RIGHT"
    assert evaluator.update(-0.081).direction == "RIGHT"
    released_right = evaluator.update(-0.08)
    assert released_right.direction == "CENTER"

    evaluator.update(0.20)
    crossed = evaluator.update(-0.50)
    assert crossed.direction == "CENTER"
    assert crossed.previous_direction == "LEFT"


@pytest.mark.parametrize("offset", [None, float("nan"), float("inf")])
def test_candidate_unavailable_is_unknown_without_erasing_valid_state(offset):
    evaluator = lean_record.FaceLeanClassifier()
    evaluator.update(0.20)

    unavailable = evaluator.update(offset)

    assert unavailable.direction == "UNKNOWN"
    assert unavailable.transitioned is False
    assert evaluator.state == "LEFT"


def test_evaluation_fields_never_enter_production_vision_payload():
    payload = vision_payload(
        t=1000.0,
        user_name="S01",
        face_detected=True,
        face_lateral_offset=0.21,
    )

    assert not any(
        key.startswith("candidate_") for key in payload["vision"]
    )


@pytest.mark.parametrize("key", [10, 13])
def test_enter_can_also_confirm(key):
    recorder = workflow()

    assert lean_record.handle_key(recorder, key, 10.0) == "confirm"
    assert recorder.is_recording is True


@pytest.mark.parametrize("key", [27, ord("q"), ord("Q")])
def test_safe_early_abort_stops_future_rows(key):
    recorder = workflow()
    recorder.confirm(10.0)

    assert lean_record.handle_key(recorder, key, 10.5) == "abort"
    assert recorder.state == lean_record.STATE_ABORTED
    assert row(recorder, 10.6) is None


def test_frame_record_exposes_validity_calibration_and_continuous_metrics():
    geometry = FaceLateralGeometry(0.55, 0.25, -3.25)

    row = lean_record.frame_record(
        now=1725345600.1234,
        label=lean_record.LABEL_USER_LEFT,
        face_detected=True,
        frontal=True,
        calibration_available=True,
        geometry=geometry,
        offset=-0.2,
        roll_delta=-1.5,
    )

    assert row["timestamp"] == "2024-09-03T06:40:00.123Z"
    assert row["workflow_state"] == "RECORDING"
    assert row["guided_label"] == "USER_LEFT_LEAN"
    assert row["face_valid"] is True
    assert row["calibration_available"] is True
    assert row["face_lateral_offset"] == -0.2
    assert row["head_roll_deg"] == -3.25
    assert row["head_roll_delta_deg"] == -1.5


def test_no_face_is_unavailable_not_center():
    row = lean_record.frame_record(
        now=1000,
        label=lean_record.LABEL_NEUTRAL,
        face_detected=False,
        frontal=None,
        calibration_available=False,
    )

    assert row["face_valid"] is False
    assert row["calibration_available"] is False
    assert row["face_lateral_offset"] is None
    assert row["head_roll_deg"] is None


def test_output_path_separates_conditions_and_sanitizes_metadata():
    path = lean_record.output_path("../S 01", "light/off", 1725345600.0)

    assert path.parent == lean_record.OUT_DIR
    assert path.name == "S-01_lean_light-off_20240903T064000Z.jsonl"
