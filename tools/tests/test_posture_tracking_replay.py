import json

from fusion.posture_tracking import PostureTrackingConfig, PostureTrackingMode
from tools.posture_tracking_replay import read_jsonl, replay_rows, sample_from_row


def short_config():
    return PostureTrackingConfig(
        mode=PostureTrackingMode.CLOCK,
        anchor_samples=3,
        anchor_radius=0.02,
        pressure_candidate_distance=0.2,
        pressure_candidate_sec=2.0,
        pressure_candidate_radius=0.02,
        ir_anchor_radius_mm=10.0,
        forward_ir_delta_mm=80.0,
        forward_front_back_delta=0.0025,
        lateral_guard_delta=0.02,
        forward_candidate_sec=2.0,
        recovery_ir_delta_mm=20.0,
        recovery_front_back_delta=0.0025,
        recovery_pressure_distance=0.02,
        recovery_sec=2.0,
    )


def row(t, pressure, *, ir=-1, seated=True, static=None):
    return {
        "record_type": "shadow_sample",
        "timestamp": t,
        "pressure": pressure,
        "chair_distance_mm": ir,
        "fusion_seated": seated,
        "static_hold_sec": float(t) if static is None else static,
        "shadow": {"session_id": "session-a"},
    }


def test_sample_from_row_supports_r18_flat_pressure_and_skips_events():
    parsed = sample_from_row({
        "record_type": "sample",
        "timestamp": 1.0,
        "fl": 1,
        "fr": 2,
        "bl": 3,
        "br": 4,
        "fusion_seated": True,
    })
    assert parsed["pressure"] == [1, 2, 3, 4]
    assert parsed["chair_ir_mm"] is None
    assert sample_from_row({"record_type": "transition", "timestamp": 1}) is None

    nested = sample_from_row({
        "record_type": "shadow_sample",
        "timestamp": 2.0,
        "pressure": [1, 2, 3, 4],
        "shadow": {"session_id": "s", "static_hold_sec": 12.5},
    })
    assert nested["static_hold_sec"] == 12.5


def test_replay_compares_cancelled_candidate_policies_and_legacy_resets():
    center = [250, 250, 250, 250]
    left = [400, 100, 400, 100]
    rows = [
        row(0, center, static=0),
        row(1, center, static=1),
        row(2, center, static=2),
        row(3, left, static=0),
        row(4, left, static=1),
        row(5, center, static=0),
    ]
    summary = replay_rows(rows, config=short_config())
    assert summary["samples"] == 6
    assert summary["canonical_event_count"] == 0
    assert summary["candidate_samples"] == 2
    assert summary["candidate_cancellations"] == [
        {"timestamp": 5.0, "policy_delta_sec": 2.0}
    ]
    assert summary["max_policy_difference_sec"] == 2.0
    assert summary["legacy_static_hold"]["observed_reset_count"] == 2
    assert summary["final_sustained_posture_sec"] == {
        "freeze_no_backfill": 3.0,
        "continuous_valid_seated": 5.0,
    }


def test_replay_records_one_canonical_confirmation_and_invalid_ir():
    center = [250, 250, 250, 250]
    left = [400, 100, 400, 100]
    rows = [row(t, center if t < 3 else left) for t in range(6)]
    summary = replay_rows(rows, config=short_config())
    assert summary["invalid_ir_samples"] == 6
    assert summary["canonical_event_count"] == 1
    assert summary["canonical_event_kinds"] == {"PRESSURE_CHANGE": 1}
    assert summary["final_sustained_posture_sec"] == {
        "freeze_no_backfill": 0.0,
        "continuous_valid_seated": 0.0,
    }


def test_read_jsonl_preserves_valid_rows(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = [row(0, [1, 1, 1, 1]), row(1, [2, 2, 2, 2])]
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in rows),
        encoding="utf-8",
    )
    assert list(read_jsonl(path)) == rows
