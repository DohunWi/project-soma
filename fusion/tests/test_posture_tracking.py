import pytest

from fusion.posture_tracking import (
    PostureTrackingConfig,
    PostureTrackingCoordinator,
    PostureTrackingMode,
)
from fusion.state import FusionState, step
from tools.posture_shadow import PostureShadowObserver, ShadowConfig, ShadowMode


CENTER = [250, 250, 250, 250]
LEFT = [400, 100, 400, 100]
FORWARD = [252, 252, 248, 248]


def config(mode=PostureTrackingMode.CLOCK, **overrides):
    values = {
        "mode": mode,
        "anchor_samples": 3,
        "anchor_radius": 0.02,
        "max_gap_sec": 5.0,
        "pressure_candidate_distance": 0.2,
        "pressure_candidate_sec": 2.0,
        "pressure_candidate_radius": 0.02,
        "ir_anchor_radius_mm": 10.0,
        "forward_ir_delta_mm": 80.0,
        "forward_front_back_delta": 0.0025,
        "lateral_guard_delta": 0.02,
        "forward_candidate_sec": 2.0,
        "recovery_ir_delta_mm": 20.0,
        "recovery_front_back_delta": 0.0025,
        "recovery_pressure_distance": 0.02,
        "recovery_sec": 2.0,
    }
    values.update(overrides)
    return PostureTrackingConfig(**values)


def observe(tracker, t, pressure=CENTER, ir=50, **kwargs):
    return tracker.observe(
        t,
        pressure,
        ir,
        seated=kwargs.pop("seated", True),
        session_id=kwargs.pop("session_id", "session-a"),
        static_hold_sec=kwargs.pop("static_hold_sec", float(t)),
        **kwargs,
    )


def anchor(tracker):
    rows = [observe(tracker, t) for t in range(3)]
    assert rows[-1]["pressure_shadow"]["state"] == "ANCHOR_READY"
    assert rows[-1]["pressure_ir_shadow"]["state"] == "ANCHOR_READY"
    return rows[-1]


def test_off_mode_is_a_noop_and_does_not_change_fusion_output():
    tracker = PostureTrackingCoordinator()
    assert observe(tracker, 0) is None

    sample = {"pressure": CENTER, "user_name": "guest"}
    expected = step(FusionState(), sample, 1000.0)
    observe(tracker, 1, LEFT)
    assert step(FusionState(), sample, 1000.0) == expected


def test_shadow_mode_detects_but_does_not_advance_clocks():
    tracker = PostureTrackingCoordinator(config(PostureTrackingMode.SHADOW))
    anchor(tracker)
    result = observe(tracker, 3, LEFT, ir=-1)
    assert result["pressure_shadow"]["state"] == "CANDIDATE"
    assert result["sustained_posture_sec"] is None
    assert result["production_behavior_changed"] is False


def test_candidate_cancellation_exposes_both_clock_policies_without_backfill():
    tracker = PostureTrackingCoordinator(config())
    anchor(tracker)
    observe(tracker, 3, LEFT, ir=-1)
    observe(tracker, 4, LEFT, ir=-1)
    cancelled = observe(tracker, 5, CENTER, ir=-1)
    assert cancelled["pressure_shadow"]["state"] == "ANCHOR_READY"
    assert cancelled["canonical_event"] is None
    assert cancelled["sustained_posture_sec"] == {
        "freeze_no_backfill": pytest.approx(3.0),
        "continuous_valid_seated": pytest.approx(5.0),
    }
    assert cancelled["candidate_policy_finalized"] is False


def test_pressure_confirmation_resets_both_clocks_once_and_return_is_new_change():
    tracker = PostureTrackingCoordinator(config())
    anchor(tracker)
    for t in range(3, 6):
        confirmed = observe(tracker, t, LEFT, ir=-1)
    assert confirmed["canonical_event"]["kind"] == "PRESSURE_CHANGE"
    assert confirmed["canonical_event_count"] == 1
    assert confirmed["sustained_posture_sec"] == {
        "freeze_no_backfill": 0.0,
        "continuous_valid_seated": 0.0,
    }
    after = observe(tracker, 6, LEFT, ir=-1)
    assert after["canonical_event"] is None
    assert after["canonical_event_count"] == 1
    assert after["sustained_posture_sec"]["continuous_valid_seated"] == 1.0

    for t in range(7, 10):
        returned = observe(tracker, t, CENTER, ir=-1)
    assert returned["canonical_event"]["target"] == "RIGHT"
    assert returned["canonical_event_count"] == 2


def test_forward_and_neutral_return_are_independent_canonical_events():
    tracker = PostureTrackingCoordinator(
        config(pressure_candidate_distance=0.2)
    )
    anchor(tracker)
    for t in range(3, 6):
        forward = observe(tracker, t, FORWARD, ir=150)
    assert forward["canonical_event"]["kind"] == "FORWARD_CHANGE"
    assert forward["pressure_ir_shadow"]["forward_confirmed"] is True
    for t in range(6, 9):
        returned = observe(tracker, t, CENTER, ir=50)
    assert returned["canonical_event"]["kind"] == "NEUTRAL_RETURN"
    assert returned["canonical_event_count"] == 2


def test_same_tick_pressure_and_ir_confirmations_reset_one_canonical_event():
    tracker = PostureTrackingCoordinator(
        config(
            pressure_candidate_distance=0.02,
            pressure_candidate_radius=0.02,
            lateral_guard_delta=0.2,
        )
    )
    anchor(tracker)
    front_loaded = [400, 400, 100, 100]
    for t in range(3, 6):
        result = observe(tracker, t, front_loaded, ir=150)
    assert result["canonical_event_count"] == 1
    assert result["canonical_event"]["resets_clock_once"] is True
    assert result["canonical_event"]["kind"] == "FORWARD_CHANGE"
    assert result["canonical_event"]["sources"] == ["pressure", "pressure_ir"]
    assert len(result["deduplicated_events"]) == 1


def test_overlapping_late_neutral_confirmation_does_not_reset_clock_twice():
    tracker = PostureTrackingCoordinator(
        config(
            pressure_candidate_distance=0.02,
            pressure_candidate_sec=1.0,
            pressure_candidate_radius=0.02,
            lateral_guard_delta=0.2,
        )
    )
    anchor(tracker)
    front_loaded = [400, 400, 100, 100]
    for t in range(3, 6):
        observe(tracker, t, front_loaded, ir=150)
    first_return = observe(tracker, 6, CENTER, ir=50)
    pressure_return = observe(tracker, 7, CENTER, ir=50)
    ir_return = observe(tracker, 8, CENTER, ir=50)
    assert first_return["canonical_event"] is None
    assert pressure_return["canonical_event"]["kind"] == "PRESSURE_CHANGE"
    assert pressure_return["canonical_event_count"] == 2
    assert ir_return["canonical_event"] is None
    assert ir_return["canonical_event_count"] == 2
    assert ir_return["deduplicated_events"][0]["duplicate_of"] == 2
    assert ir_return["sustained_posture_sec"] == {
        "freeze_no_backfill": 0.0,
        "continuous_valid_seated": 1.0,
    }


def test_invalid_ir_does_not_disable_pressure_change_detection():
    tracker = PostureTrackingCoordinator(config())
    anchor(tracker)
    for t in range(3, 6):
        result = observe(tracker, t, LEFT, ir=-1)
    assert result["sample_valid_ir"] is False
    assert result["pressure_ir_shadow"]["state"] == "INVALID"
    assert result["canonical_event"]["kind"] == "PRESSURE_CHANGE"


def test_gap_absence_reseat_and_session_change_follow_safe_lifecycle():
    tracker = PostureTrackingCoordinator(config())
    anchor(tracker)
    before = observe(tracker, 3)
    gap = observe(tracker, 10)
    assert gap["data_gap"] is True
    assert gap["sustained_posture_sec"] == before["sustained_posture_sec"]

    absent = observe(tracker, 11, pressure=[0, 0, 0, 0], ir=-1, seated=False)
    assert absent["sustained_posture_sec"] == {
        "freeze_no_backfill": 0.0,
        "continuous_valid_seated": 0.0,
    }
    reseated = observe(tracker, 12)
    assert reseated["pressure_shadow"]["state"] == "UNINITIALIZED"

    changed = observe(tracker, 20, session_id="session-b")
    assert changed["session_changed"] is True
    assert changed["canonical_event_count"] == 0
    assert changed["sustained_posture_sec"] == {
        "freeze_no_backfill": 0.0,
        "continuous_valid_seated": 0.0,
    }


def test_pressure_confirmation_timing_matches_r19_shadow_logic():
    tracker = PostureTrackingCoordinator(config())
    r19 = PostureShadowObserver(
        ShadowConfig(
            shadow_mode=ShadowMode.EXPERIMENTAL,
            shadow_anchor_samples=3,
            shadow_anchor_stability_radius=0.02,
            shadow_max_gap_sec=5.0,
            shadow_long_absence_sec=5.0,
            experimental_candidate_distance=0.2,
            experimental_candidate_sec=2.0,
            experimental_candidate_radius=0.02,
        )
    )
    tracker_confirmed = []
    r19_confirmed = []
    for t, pressure in enumerate([CENTER] * 3 + [LEFT] * 3 + [CENTER] * 3):
        tracked = observe(tracker, t, pressure, ir=-1)
        legacy = r19.observe(
            t,
            pressure,
            seated=True,
            static_hold_sec=float(t),
            session_id="session-a",
        )
        if tracked["pressure_shadow"]["event"]:
            tracker_confirmed.append(t)
        if legacy["shadow_event"]:
            r19_confirmed.append(t)
    assert tracker_confirmed == r19_confirmed == [5, 8]
