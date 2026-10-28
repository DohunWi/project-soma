"""Normal-only integration for the score-v1 Blink Score policy."""
from dataclasses import replace

import pytest

from fusion.blink_load import BlinkPolicyMode
from fusion.load import score_integer
from fusion.posture_tracking import PostureTrackingConfig, PostureTrackingMode
from fusion.static_burden import StaticPolicyMode
from server.app import ChairPipeline
from server.config import NORMAL_PROFILE


def pipeline(*, blink_policy=BlinkPolicyMode.SCORE_V1, static_v2=False):
    result = ChairPipeline(
        NORMAL_PROFILE.fusion,
        NORMAL_PROFILE.load,
        distance_timing=NORMAL_PROFILE.distance_evidence_timing,
        blink_policy_mode=blink_policy,
        posture_tracking_config=PostureTrackingConfig(
            mode=(
                PostureTrackingMode.CLOCK
                if static_v2
                else PostureTrackingMode.OFF
            )
        ),
        static_policy_mode=(
            StaticPolicyMode.CUMULATIVE_V2
            if static_v2
            else StaticPolicyMode.LEGACY
        ),
    )
    result.reset("blink-policy-session")
    return result


def tick(
    subject,
    timestamp,
    *,
    rate=7.0,
    detect_rate=1.0,
    threshold_mode="personalized",
    baseline=0.244899,
    include_vision=True,
    pressure_value=None,
):
    if pressure_value is None:
        pressure_value = 900 if int(timestamp) % 2 == 0 else 910
    payload = {
        "v": 1,
        "t": float(timestamp),
        "source": "chair",
        "device_id": "blink-policy-test",
        "user_name": "test",
        "chair": {"pressure": [pressure_value] * 4, "ir": [50]},
    }
    sample = {
        "pressure": [pressure_value] * 4,
        "ir": [50],
        "user_name": "test",
    }
    if include_vision:
        if rate is not None:
            sample["blink_rate"] = rate
        if detect_rate is not None:
            sample["detect_rate"] = detect_rate
        if threshold_mode is not None:
            sample["ear_threshold_mode"] = threshold_mode
        if baseline is not None:
            sample["open_ear_baseline"] = baseline
        sample["ear_calibration_status"] = (
            "loaded_personalized"
            if threshold_mode == "personalized"
            else "calibration_failed_fallback"
        )
    return subject.process_prepared(payload, sample)


def hold(subject, end, **kwargs):
    decision = None
    for timestamp in range(end + 1):
        decision = tick(subject, timestamp, **kwargs)
    return decision


@pytest.mark.parametrize(
    ("seconds", "expected_penalty", "expected_score", "expected_state"),
    [
        (300, 0.0, 100, "CAUTION"),
        (600, 5.0, 95, "CAUTION"),
        (900, 10.0, 90, "DANGER"),
    ],
)
def test_policy_b_five_ten_fifteen_minute_boundaries(
    seconds,
    expected_penalty,
    expected_score,
    expected_state,
):
    subject = pipeline()
    decision = hold(subject, seconds)

    state = subject.debug_state()
    assert state.load.blink_penalty == pytest.approx(expected_penalty)
    assert decision["score"] == expected_score
    assert decision["state"] == expected_state
    assert decision["reasons"] == ["low_blink"]


def test_recovery_changes_state_immediately_but_score_recovers_linearly():
    subject = pipeline()
    hold(subject, 900)

    first = tick(subject, 901, rate=11)
    assert first["state"] == "NORMAL"
    assert first["reasons"] == []
    assert subject.debug_state().load.blink_penalty == pytest.approx(
        10 - 10 / 300
    )

    decision = first
    for timestamp in range(902, 1201):
        decision = tick(subject, timestamp, rate=11)
    assert subject.debug_state().load.blink_penalty == pytest.approx(0)
    assert decision["score"] == 100


def test_fallback_protects_timer_and_score_but_legacy_mode_is_unchanged():
    protected = pipeline()
    protected_decision = hold(
        protected,
        900,
        threshold_mode="fallback",
        baseline=None,
    )
    assert protected.debug_state().low_blink_sec == 0
    assert protected.debug_state().load.blink_penalty == 0
    assert protected_decision["state"] == "NORMAL"
    assert protected.latest_blink_diagnostic["update_reason"] == "fallback_threshold"

    legacy = pipeline(blink_policy=BlinkPolicyMode.LEGACY)
    legacy_decision = hold(
        legacy,
        300,
        threshold_mode="fallback",
        baseline=None,
    )
    assert legacy.debug_state().low_blink_sec == 300
    assert legacy.debug_state().load.blink_penalty == 0
    assert legacy_decision["state"] == "CAUTION"
    assert legacy.latest_blink_diagnostic is None


def test_missing_stale_zero_detect_and_hysteresis_freeze_blink_load():
    subject = pipeline()
    hold(subject, 600)
    expected_timer = subject.debug_state().low_blink_sec
    expected_penalty = subject.debug_state().load.blink_penalty

    tick(subject, 601, include_vision=False)
    assert subject.debug_state().low_blink_sec == expected_timer
    assert subject.debug_state().load.blink_penalty == expected_penalty

    tick(subject, 602, detect_rate=0)
    assert subject.debug_state().low_blink_sec == expected_timer
    assert subject.debug_state().load.blink_penalty == expected_penalty

    tick(subject, 603, rate=9, detect_rate=0.2)
    assert subject.debug_state().low_blink_sec == expected_timer
    assert subject.debug_state().load.blink_penalty == expected_penalty
    assert subject.latest_blink_diagnostic["observation_quality"] == "partial_detection"
    assert subject.latest_blink_diagnostic["update_reason"] == "hysteresis_hold"


def test_chair_only_never_creates_vision_penalty():
    subject = pipeline()
    decision = hold(subject, 900, include_vision=False)

    assert subject.debug_state().low_blink_sec == 0
    assert subject.debug_state().load.blink_penalty == 0
    assert decision["score"] == 100
    assert decision["state"] == "NORMAL"


def test_cumulative_static_blink_and_phase_c_slots_remain_additive():
    subject = pipeline(static_v2=True)
    decision = hold(subject, 600, pressure_value=900)
    assert subject.debug_state().load.static_penalty == pytest.approx(5)
    assert subject.debug_state().load.blink_penalty == pytest.approx(5)
    assert decision["score"] == 90

    state = subject.debug_state()
    subject._state = replace(  # Controlled integration fixture.
        state,
        load=replace(state.load, distance_penalty=3),
    )
    decision = tick(subject, 601, rate=9, pressure_value=900)
    state = subject.debug_state()

    assert state.load.distance_penalty == 3
    assert state.load.blink_penalty == pytest.approx(5)
    assert decision["score"] == score_integer(state.load)
    assert decision["reasons"] == ["low_blink"]


def test_flag_off_preserves_exact_legacy_decisions():
    legacy = pipeline(blink_policy=BlinkPolicyMode.LEGACY)
    default = ChairPipeline(
        NORMAL_PROFILE.fusion,
        NORMAL_PROFILE.load,
        distance_timing=NORMAL_PROFILE.distance_evidence_timing,
    )
    default.reset("default-session")

    for timestamp in range(0, 310):
        assert tick(legacy, timestamp) == tick(default, timestamp)
    assert legacy.debug_state().load == default.debug_state().load
