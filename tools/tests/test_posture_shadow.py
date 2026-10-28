import pytest

from fusion.state import FusionState, step
from tools.posture_shadow import (
    PostureShadowObserver,
    ShadowConfig,
    ShadowMode,
)


CENTER = [250, 250, 250, 250]
CENTER_NOISE = [252, 248, 251, 249]
LEFT = [400, 100, 400, 100]
RIGHT = [100, 400, 100, 400]


def experimental(**overrides):
    values = {
        "shadow_mode": ShadowMode.EXPERIMENTAL,
        "shadow_anchor_samples": 3,
        "shadow_anchor_stability_radius": 0.02,
        "shadow_max_gap_sec": 5.0,
        "shadow_long_absence_sec": 5.0,
        "experimental_candidate_distance": 0.2,
        "experimental_candidate_sec": 2.0,
        "experimental_candidate_radius": 0.02,
    }
    values.update(overrides)
    return ShadowConfig(**values)


def observe(observer, t, pressure=CENTER, *, seated=True, session="session-a"):
    return observer.observe(
        t,
        pressure,
        seated=seated,
        static_hold_sec=float(t),
        session_id=session,
    )


def initialize(observer, *, session="session-a"):
    results = [
        observe(
            observer,
            t,
            CENTER_NOISE if t == 1 else CENTER,
            session=session,
        )
        for t in range(3)
    ]
    assert results[-1]["shadow_state"] == "ANCHOR_READY"
    return results[-1]


def confirm(observer, start, pressure):
    results = [observe(observer, start + offset, pressure) for offset in range(3)]
    assert results[-1]["shadow_state"] == "CONFIRMED"
    return results


def test_initial_anchor_requires_stable_valid_window():
    observer = PostureShadowObserver(experimental())
    first = observe(observer, 0)
    second = observe(observer, 1, CENTER_NOISE)
    third = observe(observer, 2)
    assert first["shadow_state"] == "UNINITIALIZED"
    assert second["anchor_initialized"] is False
    assert third["anchor_initialized"] is True
    assert third["anchor_update_reason"] == "initial_stable_window"


@pytest.mark.parametrize(
    ("pressure", "reason"),
    [
        ([1, 2, 3], "pressure_must_have_four_finite_numbers"),
        ([0, 0, 0, 0], "pressure_sum_must_be_positive"),
    ],
)
def test_invalid_pressure_and_zero_sum_are_safe(pressure, reason):
    observer = PostureShadowObserver(experimental())
    result = observe(observer, 0, pressure)
    assert result["shadow_state"] == "INVALID"
    assert result["invalid_reason"] == reason
    assert result["anchor_initialized"] is False


def test_normal_micro_variation_does_not_start_candidate():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    result = observe(observer, 3, CENTER_NOISE)
    assert result["shadow_state"] == "ANCHOR_READY"
    assert result["candidate_started_at"] is None
    assert result["confirmed_change_count"] == 0


def test_transient_change_then_return_cancels_candidate():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    candidate = observe(observer, 3, LEFT)
    returned = observe(observer, 4, CENTER)
    assert candidate["shadow_state"] == "CANDIDATE"
    assert returned["shadow_state"] == "ANCHOR_READY"
    assert returned["candidate_started_at"] is None
    assert returned["confirmed_change_count"] == 0


def test_sustained_change_confirms_and_updates_anchor():
    observer = PostureShadowObserver(experimental())
    initial = initialize(observer)
    old_anchor = initial["anchor_channel_share"]
    results = confirm(observer, 3, LEFT)
    confirmed = results[-1]
    assert confirmed["confirmed_change_count"] == 1
    assert confirmed["anchor_update_reason"] == "confirmed_change"
    assert confirmed["anchor_channel_share"] != old_anchor
    assert confirmed["shadow_event"]["clinical_or_recovery_meaning"] is False


def test_gradual_change_reseeds_candidate_then_confirms_only_after_stability():
    observer = PostureShadowObserver(
        experimental(
            experimental_candidate_distance=0.05,
            experimental_candidate_radius=0.04,
        )
    )
    initialize(observer)
    first = observe(observer, 3, [280, 220, 280, 220])
    reseeded = observe(observer, 4, [310, 190, 310, 190])
    held_once = observe(observer, 5, [310, 190, 310, 190])
    confirmed = observe(observer, 6, [310, 190, 310, 190])
    assert first["shadow_state"] == "CANDIDATE"
    assert reseeded["shadow_state"] == "CANDIDATE"
    assert reseeded["candidate_elapsed_sec"] == 0
    assert held_once["candidate_elapsed_sec"] == 1
    assert confirmed["shadow_state"] == "CONFIRMED"
    assert confirmed["confirmed_change_count"] == 1


def test_same_pose_after_confirmation_does_not_duplicate_event():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    confirm(observer, 3, LEFT)
    results = [observe(observer, t, LEFT) for t in range(6, 12)]
    assert all(result["shadow_state"] == "ANCHOR_READY" for result in results)
    assert results[-1]["confirmed_change_count"] == 1


def test_return_to_original_pose_can_confirm_as_independent_change():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    confirm(observer, 3, LEFT)
    returned = confirm(observer, 6, CENTER)[-1]
    assert returned["shadow_state"] == "CONFIRMED"
    assert returned["confirmed_change_count"] == 2


def test_short_gap_accumulates_only_observed_valid_interval():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    started = observe(observer, 3, LEFT)
    confirmed = observe(observer, 5, LEFT)
    assert started["candidate_elapsed_sec"] == 0
    assert confirmed["shadow_state"] == "CONFIRMED"


def test_long_gap_suspends_and_cancels_candidate_but_keeps_anchor():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    observe(observer, 3, LEFT)
    gap = observe(observer, 10, LEFT)
    assert gap["shadow_state"] == "SUSPENDED"
    assert gap["invalid_reason"] == "data_gap"
    assert gap["anchor_initialized"] is True
    assert gap["candidate_started_at"] is None


def test_short_absence_preserves_anchor_and_long_absence_clears_it():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    short = observe(observer, 3, CENTER, seated=False)
    reseated = observe(observer, 4, CENTER, seated=True)
    assert short["shadow_state"] == "SUSPENDED"
    assert reseated["anchor_initialized"] is True

    observe(observer, 5, CENTER, seated=False)
    long_absence = observe(observer, 11, CENTER, seated=False)
    assert long_absence["anchor_initialized"] is False
    assert long_absence["anchor_update_reason"] == "long_absence_reset"


def test_session_change_clears_anchor_candidate_and_counts():
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    confirm(observer, 3, LEFT)
    changed = observe(observer, 10, CENTER, session="session-b")
    assert changed["session_id"] == "session-b"
    assert changed["shadow_state"] == "UNINITIALIZED"
    assert changed["confirmed_change_count"] == 0
    assert changed["anchor_update_reason"] == "session_changed"


@pytest.mark.parametrize("timestamp", [2, 1])
def test_duplicate_or_regressed_timestamp_is_ignored(timestamp):
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    result = observe(observer, timestamp, LEFT)
    assert result["shadow_state"] == "SUSPENDED"
    assert result["invalid_reason"] == "timestamp_not_increasing"
    assert result["confirmed_change_count"] == 0


def test_observe_only_never_creates_candidate_or_confirmed_event():
    observer = PostureShadowObserver(
        ShadowConfig(
            shadow_mode=ShadowMode.OBSERVE_ONLY,
            shadow_anchor_samples=3,
            shadow_anchor_stability_radius=0.02,
        )
    )
    initialize(observer)
    results = [observe(observer, t, LEFT) for t in range(3, 20)]
    assert all(result["shadow_state"] == "ANCHOR_READY" for result in results)
    assert all(result["shadow_event"] is None for result in results)
    assert results[-1]["confirmed_change_count"] == 0
    assert results[-1]["distance_from_anchor"] > 0


def test_experimental_settings_are_required_and_not_allowed_in_observe_only():
    with pytest.raises(ValueError, match="requires positive"):
        ShadowConfig(shadow_mode=ShadowMode.EXPERIMENTAL)
    with pytest.raises(ValueError, match="require shadow_mode"):
        ShadowConfig(experimental_candidate_distance=0.2)


def test_shadow_observation_does_not_change_fusion_result():
    sample = {"pressure": CENTER, "user_name": "guest"}
    before_state, before_decision = step(FusionState(), sample, 1000.0)
    observer = PostureShadowObserver(experimental())
    initialize(observer)
    observe(observer, 3, LEFT)
    after_state, after_decision = step(FusionState(), sample, 1000.0)
    assert after_state == before_state
    assert after_decision == before_decision
