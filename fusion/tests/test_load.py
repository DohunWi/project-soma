"""Pure SOMA Load Model v1 tests, independent of Fusion state decisions."""
from dataclasses import replace

import pytest

from fusion.config import (
    DEMO_SOMA_LOAD_CONFIG,
    NORMAL_SOMA_LOAD_CONFIG,
)
from fusion.load import (
    SomaLoadState,
    clamp_penalty,
    piecewise_linear,
    round_half_up,
    score_float,
    score_integer,
    update_chair_load,
)


def update(load, *, config=DEMO_SOMA_LOAD_CONFIG, **overrides):
    values = {
        "previous_static_sec": 0.0,
        "static_sec": 0.0,
        "previous_imbalance_sec": 0.0,
        "imbalance_sec": 0.0,
        "balance": "CENTER",
        "seated": True,
        "dt": 0.0,
        "config": config,
    }
    values.update(overrides)
    return update_chair_load(load, **values)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (300, 0),
        (450, 2.5),
        (600, 5),
        (900, 7.5),
        (1200, 10),
        (1800, 15),
        (2700, 20),
        (3600, 25),
        (9999, 25),
    ],
)
def test_normal_static_curve_interpolates_and_clamps(value, expected):
    assert piecewise_linear(NORMAL_SOMA_LOAD_CONFIG.static_curve, value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (5, 0), (10, 5), (20, 10), (60, 25), (999, 25)],
)
def test_demo_static_curve_boundaries(value, expected):
    assert piecewise_linear(DEMO_SOMA_LOAD_CONFIG.static_curve, value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (5, 0),
        (10, 2.5),
        (15, 5),
        (30, 10),
        (60, 15),
        (120, 20),
        (180, 25),
    ],
)
def test_normal_balance_curve_interpolates(value, expected):
    assert piecewise_linear(NORMAL_SOMA_LOAD_CONFIG.balance_curve, value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (2, 0), (3.5, 2.5), (5, 5), (10, 10), (20, 15), (30, 20), (45, 25)],
)
def test_demo_balance_curve_endpoints_and_midpoint(value, expected):
    assert piecewise_linear(DEMO_SOMA_LOAD_CONFIG.balance_curve, value) == expected


def test_penalty_clamp_enforces_zero_and_twenty_five():
    assert clamp_penalty(-1) == 0
    assert clamp_penalty(12.5) == 12.5
    assert clamp_penalty(30) == 25


def test_score_is_composite_clamped_and_half_up_rounded():
    load = SomaLoadState(10.0, 12.5, 20.0, 5.0)
    assert score_float(load) == 52.5
    assert score_integer(load) == 53
    assert round_half_up(98.5) == 99
    assert score_integer(SomaLoadState(25, 25, 25, 25)) == 0
    assert score_integer(SomaLoadState()) == 100


def test_positive_target_delta_accumulates_without_erasing_residual():
    load = update(
        SomaLoadState(static_penalty=3.0),
        previous_static_sec=5,
        static_sec=10,
        dt=5,
    )
    assert load.static_penalty == 8.0


def test_static_recovery_then_recurrence_adds_to_residual_and_clamps():
    first = update(
        SomaLoadState(),
        previous_static_sec=0,
        static_sec=20,
        dt=20,
    )
    assert first.static_penalty == 10.0

    recovered = update(first, previous_static_sec=20, static_sec=0, dt=1)
    assert recovered.static_penalty == 7.5

    recurrence = update(
        recovered,
        previous_static_sec=0,
        static_sec=10,
        dt=10,
    )
    assert recurrence.static_penalty == 12.5

    saturated = update(
        recurrence,
        previous_static_sec=0,
        static_sec=60,
        dt=60,
    )
    assert saturated.static_penalty == 25.0


def test_balance_recovery_then_recurrence_adds_to_residual_and_clamps():
    first = update(
        SomaLoadState(),
        previous_imbalance_sec=0,
        imbalance_sec=10,
        balance="LEFT",
        dt=10,
    )
    assert first.balance_penalty == 10.0

    recovered = update(first, balance="CENTER", dt=1)
    assert recovered.balance_penalty == pytest.approx(10 - 25 / 6)

    recurrence = update(
        recovered,
        previous_imbalance_sec=0,
        imbalance_sec=10,
        balance="RIGHT",
        dt=10,
    )
    assert recurrence.balance_penalty == pytest.approx(20 - 25 / 6)

    saturated = update(
        recurrence,
        previous_imbalance_sec=0,
        imbalance_sec=45,
        balance="RIGHT",
        dt=45,
    )
    assert saturated.balance_penalty == 25.0


def test_movement_and_center_recover_penalties_at_independent_rates():
    load = SomaLoadState(static_penalty=25.0, balance_penalty=25.0)
    recovered = update(load, dt=1.0)
    assert recovered.static_penalty == 22.5
    assert recovered.balance_penalty == pytest.approx(25 - 25 / 6)


def test_absent_recovery_integrates_multiplier_boundaries_exactly():
    config = replace(
        DEMO_SOMA_LOAD_CONFIG,
        static_recovery_sec=250.0,
        balance_recovery_sec=250.0,
    )
    load = SomaLoadState(
        static_penalty=25.0,
        balance_penalty=25.0,
        absent_sec=59.0,
    )
    recovered = update(load, config=config, seated=False, dt=122.0)
    # 59→60: 1s, 60→180: 120s×1.5, 180→181: 1s×2 = 183 weighted sec.
    assert recovered.absent_sec == 181.0
    assert recovered.static_penalty == pytest.approx(6.7)
    assert recovered.balance_penalty == pytest.approx(6.7)


@pytest.mark.parametrize(
    ("absent_sec", "expected_static"),
    [
        (30.0, 23.75),
        (120.0, 18.75),
        (300.0, 5.0),
    ],
)
def test_normal_absent_recovery_accelerates_by_duration(absent_sec, expected_static):
    recovered = update(
        SomaLoadState(static_penalty=25.0),
        config=NORMAL_SOMA_LOAD_CONFIG,
        seated=False,
        dt=absent_sec,
    )
    assert recovered.static_penalty == pytest.approx(expected_static)
    assert recovered.absent_sec == absent_sec


def test_reseating_preserves_residual_absent_recovery_memory():
    absent = update(
        SomaLoadState(static_penalty=25.0),
        config=NORMAL_SOMA_LOAD_CONFIG,
        seated=False,
        dt=30,
    )
    reseated = update(
        absent,
        config=NORMAL_SOMA_LOAD_CONFIG,
        seated=True,
        dt=1,
    )
    assert 0 < reseated.static_penalty < 25
    assert reseated.absent_sec == 0.0


def test_missing_vision_never_changes_reserved_vision_penalties():
    load = SomaLoadState(blink_penalty=4.0, distance_penalty=7.0)
    updated = update(load, previous_static_sec=5, static_sec=10, dt=5)
    assert updated.blink_penalty == 4.0
    assert updated.distance_penalty == 7.0
