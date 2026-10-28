from dataclasses import replace

import pytest

from fusion.blink_load import (
    NORMAL_BLINK_PENALTY_CONFIG,
    assess_blink_reliability,
    blink_penalty_target,
    update_blink_load,
)
from fusion.load import SomaLoadState, score_integer


def reliable_sample(**overrides):
    sample = {
        "blink_rate": 7.0,
        "detect_rate": 1.0,
        "ear_threshold_mode": "personalized",
        "open_ear_baseline": 0.244899,
        "ear_calibration_status": "loaded_personalized",
        "blink_rate_baseline": 0.0,
    }
    sample.update(overrides)
    return sample


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, 0), (300, 0), (600, 5), (900, 10), (1200, 10)],
)
def test_policy_b_curve(seconds, expected):
    assert blink_penalty_target(seconds) == pytest.approx(expected)


def test_low_blink_adds_only_new_curve_interval_and_preserves_other_slots():
    reliability = assess_blink_reliability(reliable_sample())
    initial = SomaLoadState(
        static_penalty=4,
        balance_penalty=3,
        blink_penalty=blink_penalty_target(599),
        distance_penalty=2,
    )
    updated, diagnostic = update_blink_load(
        initial,
        previous_low_blink_sec=599,
        low_blink_sec=600,
        blink_rate=7,
        detect_rate=1,
        reliability=reliability,
        seated=True,
        lifecycle_paused=False,
        elapsed_sec=1,
    )

    assert updated.static_penalty == 4
    assert updated.balance_penalty == 3
    assert updated.distance_penalty == 2
    assert updated.blink_penalty == pytest.approx(5)
    assert score_integer(updated) == 86
    assert diagnostic["update_reason"] == "low_blink_accumulation"
    assert diagnostic["blink_rate_baseline_usable"] is False


def test_healthy_rate_recovers_linearly_over_300_seconds():
    reliability = assess_blink_reliability(reliable_sample(blink_rate=11))
    load = SomaLoadState(blink_penalty=10)
    for _ in range(300):
        load, diagnostic = update_blink_load(
            load,
            previous_low_blink_sec=0,
            low_blink_sec=0,
            blink_rate=11,
            detect_rate=1,
            reliability=reliability,
            seated=True,
            lifecycle_paused=False,
            elapsed_sec=1,
        )

    assert load.blink_penalty == pytest.approx(0)
    assert diagnostic["update_reason"] == "healthy_rate_recovery"


@pytest.mark.parametrize(
    ("sample_overrides", "reason"),
    [
        ({"blink_rate": None}, "blink_rate_unavailable"),
        ({"detect_rate": 0}, "detect_rate_zero"),
        (
            {
                "ear_threshold_mode": "fallback",
                "open_ear_baseline": None,
            },
            "fallback_threshold",
        ),
        (
            {"ear_threshold_mode": None, "open_ear_baseline": None},
            "threshold_mode_missing",
        ),
    ],
)
def test_unavailable_and_fallback_observations_freeze_penalty(
    sample_overrides,
    reason,
):
    sample = reliable_sample(**sample_overrides)
    reliability = assess_blink_reliability(sample)
    load, diagnostic = update_blink_load(
        SomaLoadState(blink_penalty=6),
        previous_low_blink_sec=600,
        low_blink_sec=700,
        blink_rate=sample.get("blink_rate"),
        detect_rate=sample.get("detect_rate"),
        reliability=reliability,
        seated=True,
        lifecycle_paused=False,
        elapsed_sec=100,
    )

    assert load.blink_penalty == 6
    assert diagnostic["update_reason"] == reason


def test_hysteresis_and_partial_detection_are_diagnostic_without_new_threshold():
    sample = reliable_sample(blink_rate=9, detect_rate=0.2)
    reliability = assess_blink_reliability(sample)
    load, diagnostic = update_blink_load(
        SomaLoadState(blink_penalty=5),
        previous_low_blink_sec=600,
        low_blink_sec=600,
        blink_rate=9,
        detect_rate=0.2,
        reliability=reliability,
        seated=True,
        lifecycle_paused=False,
        elapsed_sec=30,
    )

    assert load.blink_penalty == 5
    assert diagnostic["observation_quality"] == "partial_detection"
    assert diagnostic["update_reason"] == "hysteresis_hold"


def test_zero_rate_baseline_never_makes_ear_personalization_unusable():
    reliability = assess_blink_reliability(reliable_sample())

    assert reliability.personalized_ear is True
    assert reliability.blink_rate_baseline_usable is False
    assert reliability.policy_available is True


def test_invalid_personalized_baseline_is_not_trusted():
    reliability = assess_blink_reliability(
        reliable_sample(open_ear_baseline=0)
    )

    assert reliability.personalized_ear is False
    assert reliability.unavailable_reason == "personalized_baseline_invalid"


def test_configuration_is_immutable_and_recovery_does_not_touch_other_slots():
    load = SomaLoadState(static_penalty=2, distance_penalty=3, blink_penalty=5)
    reliability = assess_blink_reliability(reliable_sample(blink_rate=11))
    updated, _ = update_blink_load(
        load,
        previous_low_blink_sec=0,
        low_blink_sec=0,
        blink_rate=11,
        detect_rate=1,
        reliability=reliability,
        seated=True,
        lifecycle_paused=False,
        elapsed_sec=1,
    )

    assert updated.static_penalty == 2
    assert updated.distance_penalty == 3
    assert updated.blink_penalty < 5
    assert replace(NORMAL_BLINK_PENALTY_CONFIG) == NORMAL_BLINK_PENALTY_CONFIG
