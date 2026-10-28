import pytest

from tools.posture_ir_shadow import (
    PressureIrPostureShadowObserver,
    PressureIrShadowConfig,
    PressureIrShadowMode,
)


NEUTRAL = [250, 250, 250, 250]
FORWARD = [252, 252, 248, 248]
COMBINED_LEFT = [310, 210, 290, 190]


def config(mode=PressureIrShadowMode.EXPERIMENTAL):
    return PressureIrShadowConfig(
        mode=mode,
        anchor_samples=3,
        anchor_ir_radius_mm=10,
        anchor_pressure_radius=0.02,
        forward_ir_delta_mm=80,
        forward_front_back_delta=0.0025,
        lateral_guard_delta=0.02,
        candidate_sec=2,
        recovery_ir_delta_mm=20,
        recovery_front_back_delta=0.0025,
        recovery_pressure_distance=0.02,
        recovery_sec=2,
    )


def observe(observer, timestamp, pressure=NEUTRAL, ir=50, **kwargs):
    return observer.observe(
        timestamp,
        pressure,
        ir,
        seated=kwargs.pop("seated", True),
        pressure_shadow=kwargs.pop("pressure_shadow", None),
        session_id=kwargs.pop("session_id", "session-a"),
        **kwargs,
    )


def anchor(observer, start=0):
    results = [observe(observer, timestamp) for timestamp in range(start, start + 3)]
    assert results[-1]["state"] == "ANCHOR_READY"
    assert results[-1]["transition"]["type"] == "neutral_anchor_created"
    return results[-1]


def test_default_observe_only_builds_anchor_but_never_detects():
    observer = PressureIrPostureShadowObserver(
        config(PressureIrShadowMode.OBSERVE_ONLY)
    )
    anchor(observer)
    results = [observe(observer, timestamp, FORWARD, 150) for timestamp in range(3, 7)]
    assert all(result["state"] == "ANCHOR_READY" for result in results)
    assert all(result["candidate_started_at"] is None for result in results)
    assert results[-1]["classification"] == "FORWARD_EVIDENCE"
    assert results[-1]["confirmed_count"] == 0


def test_forward_candidate_confirms_after_continuous_evidence_without_anchor_drift():
    observer = PressureIrPostureShadowObserver(config())
    initial = anchor(observer)
    baseline = initial["ir_baseline_mm"]
    started = observe(observer, 3, FORWARD, 150)
    held = observe(observer, 4, FORWARD, 151)
    confirmed = observe(observer, 5, FORWARD, 149)
    assert started["state"] == "FORWARD_CANDIDATE"
    assert started["transition"]["type"] == "forward_candidate_started"
    assert held["candidate_elapsed_sec"] == pytest.approx(1)
    assert confirmed["state"] == "FORWARD_CONFIRMED"
    assert confirmed["transition"]["type"] == "forward_change_confirmed"
    assert confirmed["ir_baseline_mm"] == baseline
    assert confirmed["confirmed_count"] == 1


def test_single_ir_jump_does_not_confirm_and_candidate_is_cancelled():
    observer = PressureIrPostureShadowObserver(config())
    anchor(observer)
    jump = observe(observer, 3, FORWARD, 180)
    returned = observe(observer, 4, NEUTRAL, 50)
    assert jump["state"] == "FORWARD_CANDIDATE"
    assert returned["state"] == "ANCHOR_READY"
    assert returned["transition"]["type"] == "forward_candidate_cancelled"
    assert returned["confirmed_count"] == 0


def test_lateral_pressure_and_ir_combination_is_not_called_forward():
    observer = PressureIrPostureShadowObserver(config())
    anchor(observer)
    result = observe(observer, 3, COMBINED_LEFT, 160)
    assert result["classification"] == "COMBINED_LATERAL_IR"
    assert result["lateral_direction"] == "LEFT"
    assert result["state"] == "ANCHOR_READY"
    assert result["candidate_started_at"] is None


def test_existing_pressure_confirmation_is_recorded_as_lateral_evidence():
    observer = PressureIrPostureShadowObserver(config())
    anchor(observer)
    pressure_shadow = {
        "distance_from_anchor": 0.0,
        "shadow_event": {
            "type": "posture_change_confirmed",
            "timestamp": 3,
        },
    }
    result = observe(
        observer,
        3,
        [300, 200, 300, 200],
        90,
        pressure_shadow=pressure_shadow,
    )
    assert result["classification"] == "LATERAL_LEFT"
    assert result["transition"]["type"] == "lateral_pressure_change_confirmed"
    assert result["transition"]["direction"] == "LEFT"


def test_neutral_recovery_confirms_and_reseeds_anchor_after_hold():
    observer = PressureIrPostureShadowObserver(config())
    anchor(observer)
    for timestamp in range(3, 6):
        confirmed = observe(observer, timestamp, FORWARD, 150)
    assert confirmed["forward_confirmed"] is True
    recovering = observe(observer, 6, NEUTRAL, 52)
    observe(observer, 7, NEUTRAL, 51)
    recovered = observe(observer, 8, NEUTRAL, 50)
    assert recovering["state"] == "RECOVERY_CANDIDATE"
    assert recovered["state"] == "ANCHOR_READY"
    assert recovered["transition"]["type"] == "neutral_return_confirmed"
    assert recovered["forward_confirmed"] is False
    assert recovered["ir_baseline_mm"] == 51


def test_invalid_ir_absence_and_long_gap_do_not_continue_candidate():
    observer = PressureIrPostureShadowObserver(config())
    anchor(observer)
    observe(observer, 3, FORWARD, 150)
    invalid = observe(observer, 4, FORWARD, -1)
    assert invalid["state"] == "INVALID"
    assert invalid["candidate_started_at"] is None

    absent = observe(observer, 5, NEUTRAL, 50, seated=False)
    assert absent["state"] == "SUSPENDED"
    assert absent["transition"]["type"] == "anchor_reset_absence"
    assert absent["anchor_initialized"] is False

    rebuilding = observe(observer, 6, NEUTRAL, 50)
    assert rebuilding["state"] == "UNINITIALIZED"
    assert rebuilding["transition"]["type"] == "anchor_rebuild_after_reseat"
    assert observe(observer, 7, NEUTRAL, 50)["state"] == "UNINITIALIZED"
    assert observe(observer, 8, NEUTRAL, 50)["state"] == "ANCHOR_READY"
    gap = observe(observer, 20, NEUTRAL, 50)
    assert gap["state"] == "SUSPENDED"
    assert gap["transition"]["type"] == "anchor_reset_data_gap"
    assert gap["anchor_initialized"] is False


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError, match="anchor_samples"):
        PressureIrShadowConfig(anchor_samples=1)
    with pytest.raises(ValueError, match="candidate_sec"):
        PressureIrShadowConfig(candidate_sec=0)
