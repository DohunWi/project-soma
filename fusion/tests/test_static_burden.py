from dataclasses import replace

import pytest

from fusion.config import NORMAL_FUSION_TIMING, NORMAL_SOMA_LOAD_CONFIG
from fusion.load import SomaLoadState
from fusion.state import FusionState, decision_with_static_burden
from fusion.static_burden import (
    NORMAL_STATIC_BURDEN_CONFIG,
    StaticBurdenState,
    static_burden_level,
    update_cumulative_static_burden,
)


def tracking(episode, *, pre_reset=None, event=None, **overrides):
    values = {
        "mode": "clock",
        "lifecycle_paused": False,
        "sample_valid_pressure": True,
        "data_gap": False,
        "sustained_posture_sec": {
            "freeze_no_backfill": episode,
            "continuous_valid_seated": episode,
        },
        "pre_reset_sustained_posture_sec": {
            "freeze_no_backfill": episode if pre_reset is None else pre_reset,
            "continuous_valid_seated": (
                episode if pre_reset is None else pre_reset
            ),
        },
        "canonical_event": event,
    }
    values.update(overrides)
    return values


def advance(state, penalty, episode, *, pre_reset=None, event=None, **overrides):
    return update_cumulative_static_burden(
        state,
        previous_penalty=penalty,
        legacy_penalty=penalty,
        posture_tracking=tracking(
            episode,
            pre_reset=pre_reset,
            event=event,
            **overrides,
        ),
        previous_seated=True,
        seated=True,
        load_config=NORMAL_SOMA_LOAD_CONFIG,
    )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(300, 0), (600, 5), (1200, 10), (1800, 15), (2700, 20), (3600, 25)],
)
def test_continuous_pose_matches_existing_normal_curve(seconds, expected):
    state, penalty, diagnostic = advance(StaticBurdenState(), 0.0, seconds)
    assert penalty == pytest.approx(expected)
    assert state.posture_episode_sec == seconds
    assert diagnostic["static_penalty"] == pytest.approx(expected)


def test_confirmed_change_resets_episode_once_without_recovering_penalty():
    event = {"id": 1, "kind": "PRESSURE_CHANGE"}
    state, penalty, diagnostic = advance(
        StaticBurdenState(
            posture_episode_sec=599,
            static_eligible_total_sec=299,
        ),
        4.9833333333,
        0,
        pre_reset=600,
        event=event,
    )
    assert state.posture_episode_sec == 0
    assert state.static_eligible_total_sec == pytest.approx(300)
    assert penalty == pytest.approx(5)
    assert diagnostic["update_reason"] == "canonical_reset"

    # A duplicate confirmation carries no new canonical event and cannot reset
    # or charge the same interval again.
    duplicate_state, duplicate_penalty, _ = advance(state, penalty, 0)
    assert duplicate_state == state
    assert duplicate_penalty == penalty


def test_global_curve_cursor_does_not_repeat_early_episode_slope():
    event = {"id": 1}
    state, penalty, _ = advance(
        StaticBurdenState(), 0.0, 0, pre_reset=600, event=event
    )
    assert penalty == pytest.approx(5)
    assert state.static_eligible_total_sec == 300

    state, penalty, _ = advance(
        state, penalty, 0, pre_reset=600, event={"id": 2}
    )
    # Two ten-minute episodes map to global curve second 900, not 5+5.
    assert state.static_eligible_total_sec == 600
    assert penalty == pytest.approx(7.5)


def test_frequent_confirmed_changes_below_grace_add_no_penalty():
    state = StaticBurdenState()
    penalty = 0.0
    for event_id in range(1, 16):
        state, penalty, _ = advance(
            state,
            penalty,
            0,
            pre_reset=240,
            event={"id": event_id},
        )
    assert state.static_eligible_total_sec == 0
    assert penalty == 0


def test_candidate_cancellation_continuous_time_is_eligible():
    state, penalty, _ = advance(StaticBurdenState(), 0.0, 599)
    state, penalty, _ = advance(state, penalty, 601)
    assert state.posture_episode_sec == 601
    assert state.static_eligible_total_sec == 301
    assert penalty > 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"sample_valid_pressure": False},
        {"data_gap": True},
        {"lifecycle_paused": True},
    ],
)
def test_invalid_gap_and_lifecycle_pause_freeze_burden(overrides):
    initial = StaticBurdenState(700, 400)
    state, penalty, diagnostic = advance(
        initial,
        6.0,
        800,
        **overrides,
    )
    assert state == initial
    assert penalty == 6.0
    assert diagnostic["update_reason"] == "frozen_unavailable"


def test_confirmed_absence_uses_legacy_recovery_and_reseat_starts_clean():
    initial = StaticBurdenState(1200, 900)
    absent, penalty, diagnostic = update_cumulative_static_burden(
        initial,
        previous_penalty=10.0,
        legacy_penalty=9.9583333333,
        posture_tracking=None,
        previous_seated=True,
        seated=False,
        load_config=NORMAL_SOMA_LOAD_CONFIG,
    )
    assert absent == StaticBurdenState()
    assert penalty == pytest.approx(9.9583333333)
    assert diagnostic["update_reason"] == "confirmed_absence"

    reseated, penalty, diagnostic = update_cumulative_static_burden(
        absent,
        previous_penalty=penalty,
        legacy_penalty=0.0,
        posture_tracking=tracking(0),
        previous_seated=False,
        seated=True,
        load_config=NORMAL_SOMA_LOAD_CONFIG,
    )
    assert reseated == StaticBurdenState()
    assert penalty == 0
    assert diagnostic["update_reason"] == "new_seated_interval"


def test_penalty_caps_at_twenty_five_and_levels_are_ten_and_twenty():
    state, penalty, _ = advance(StaticBurdenState(), 0.0, 10000)
    assert penalty == 25
    assert static_burden_level(9.999) == 0
    assert static_burden_level(10) == 1
    assert static_burden_level(19.999) == 1
    assert static_burden_level(20) == 2


def test_v2_decision_uses_penalty_for_static_reason_and_preserves_other_reasons():
    base_decision = {
        "v": 1,
        "t": 1.0,
        "user_name": "test",
        "state": "NORMAL",
        "confidence": 1.0,
        "score": 100,
        "reasons": [],
        "metrics": {"balance": "CENTER", "seated": True},
    }
    caution = FusionState(
        seated=True,
        static_hold_sec=0.0,
        load=SomaLoadState(static_penalty=10.0),
    )
    decision = decision_with_static_burden(
        caution,
        base_decision,
        NORMAL_FUSION_TIMING,
        NORMAL_STATIC_BURDEN_CONFIG,
    )
    assert decision["state"] == "CAUTION"
    assert decision["score"] == 90
    assert decision["reasons"] == ["static_hold"]

    danger_with_vision = replace(
        caution,
        low_blink_sec=NORMAL_FUSION_TIMING.low_blink_danger_sec,
        load=SomaLoadState(static_penalty=20.0, distance_penalty=5.0),
    )
    decision = decision_with_static_burden(
        danger_with_vision,
        base_decision,
        NORMAL_FUSION_TIMING,
        NORMAL_STATIC_BURDEN_CONFIG,
    )
    assert decision["state"] == "DANGER"
    assert decision["score"] == 75
    assert decision["reasons"] == ["low_blink", "static_hold"]


def test_v2_decision_never_overrides_confirmed_absence():
    absent = FusionState(
        seated=False,
        load=SomaLoadState(static_penalty=10.0),
    )
    base_decision = {
        "v": 1,
        "t": 1.0,
        "user_name": "test",
        "state": "ABSENT",
        "confidence": 1.0,
        "score": 90,
        "reasons": [],
        "metrics": {"balance": "CENTER", "seated": False},
    }
    decision = decision_with_static_burden(
        absent,
        base_decision,
        NORMAL_FUSION_TIMING,
        NORMAL_STATIC_BURDEN_CONFIG,
    )
    assert decision["state"] == "ABSENT"
    assert decision["score"] == 90
    assert decision["reasons"] == []
