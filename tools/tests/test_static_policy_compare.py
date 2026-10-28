import pytest

from fusion.posture_tracking import PostureTrackingConfig, PostureTrackingMode
from tools.static_policy_compare import compare_rows


def row(timestamp, pressure=None, *, ir=-1, seated=True):
    return {
        "record_type": "shadow_sample",
        "timestamp": float(timestamp),
        "pressure": list(pressure or [250, 250, 250, 250]),
        "chair_distance_mm": ir,
        "fusion_seated": seated,
        "shadow": {"session_id": "compare-test"},
    }


def short_tracker():
    return PostureTrackingConfig(
        mode=PostureTrackingMode.CLOCK,
        anchor_samples=3,
        pressure_candidate_distance=0.1,
        pressure_candidate_sec=2.0,
        pressure_candidate_radius=0.02,
    )


@pytest.mark.parametrize(
    ("seconds", "expected_penalty", "expected_score"),
    [
        (1200, 10.0, 90.0),
        (2700, 20.0, 80.0),
        (3600, 25.0, 75.0),
    ],
)
def test_constant_pose_matches_all_three_policies_at_long_boundaries(
    seconds,
    expected_penalty,
    expected_score,
):
    summary = compare_rows(row(second) for second in range(seconds + 1))
    policies = summary["policies"]
    assert all(
        policy["final_static_penalty"] == expected_penalty
        for policy in policies.values()
    )
    assert all(
        policy["final_static_only_score"] == expected_score
        for policy in policies.values()
    )


def test_small_pressure_jitter_resets_legacy_but_not_new_clocks():
    rows = []
    for second in range(601):
        value = 900 if second % 120 else 910
        rows.append(row(second, [value, value, value, value]))
    summary = compare_rows(rows)
    assert summary["policies"]["A_legacy"]["final_static_penalty"] == 0.0
    assert summary["policies"]["C_cumulative_burden"]["final_static_penalty"] == 5.0
    assert summary["canonical_event_count"] == 0


def test_confirmed_changes_are_counted_once_and_do_not_recover_c_burden():
    center = [250, 250, 250, 250]
    left = [400, 100, 400, 100]
    rows = [row(second, center) for second in range(601)]
    rows.extend(row(second, left) for second in range(601, 605))
    rows.extend(row(second, left) for second in range(605, 880))
    summary = compare_rows(rows, tracker_config=short_tracker())
    assert summary["canonical_event_count"] == 1
    assert summary["policies"]["C_cumulative_burden"]["final_static_penalty"] >= 5.0
    assert summary["final_clocks"]["posture_episode_sec"] < 300.0


def test_invalid_rows_and_long_gaps_do_not_backfill_time():
    rows = [row(0), {"record_type": "shadow_sample", "timestamp": 1, "pressure": [1]}]
    rows.extend([row(10), row(11)])
    summary = compare_rows(rows)
    assert summary["samples"] == 3
    assert summary["invalid_rows"] == 1
    assert summary["final_clocks"]["static_eligible_total_sec"] == 0.0
