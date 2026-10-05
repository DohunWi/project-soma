"""Boundary and transition tests for diagnostic-only Phase C temporal evidence."""
from dataclasses import FrozenInstanceError
import math

import pytest

from fusion.config import (
    DEMO_DISTANCE_EVIDENCE_TIMING as DEMO,
    NORMAL_DISTANCE_EVIDENCE_TIMING as NORMAL,
    DistanceEvidenceTiming,
)
from fusion.distance_evidence import DistanceEvidenceClass as Kind
from fusion.distance_evidence import classify_distance_evidence
from fusion.distance_temporal import DistanceTemporalState, step


def observation(kind, *, seated=True):
    return classify_distance_evidence(
        face_distance_cm=42.0 if kind in (Kind.BODY_FORWARD_CLOSE, Kind.FACE_ONLY_CLOSE) else 60.0,
        face_approach_delta_cm=(
            18.0 if kind in (Kind.BODY_FORWARD_CLOSE, Kind.FACE_ONLY_CLOSE) else 0.0
        ),
        backrest_departure_delta_mm=(
            130.0 if kind in (Kind.BODY_FORWARD_CLOSE, Kind.BACKREST_AWAY) else 0.0
        ),
        vision_available=kind is not Kind.UNKNOWN,
        chair_ir_available=True,
        seated=seated,
    )


def tick(state, kind, now, timing=DEMO, **kwargs):
    return step(state, observation(kind, **kwargs), now, timing=timing)


def hold(kind, seconds, *, timing=DEMO, cadence=1.0, state=None, start=0.0):
    state = state or DistanceTemporalState()
    for index in range(int(seconds / cadence) + 1):
        state, result = tick(state, kind, start + index * cadence, timing)
    return state, result


@pytest.mark.parametrize("timing", [DEMO, NORMAL])
@pytest.mark.parametrize("kind,field", [
    (Kind.BODY_FORWARD_CLOSE, "body_forward_enter_sec"),
    (Kind.FACE_ONLY_CLOSE, "face_only_enter_sec"),
])
@pytest.mark.parametrize("cadence", [0.25, 1.0, 5.0])
def test_own_entry_boundary_uses_elapsed_time_not_sample_count(timing, kind, field, cadence):
    threshold = getattr(timing, field)
    state, _ = tick(DistanceTemporalState(), Kind.NORMAL, -1, timing)
    state, result = hold(kind, threshold - cadence, timing=timing, cadence=cadence, state=state)
    assert result.active is False
    state, result = tick(state, kind, threshold - 0.001, timing)
    assert result.active is False
    state, result = tick(state, kind, threshold, timing)
    assert result.accumulated_sec == pytest.approx(threshold)
    assert result.sustained_classification is kind
    assert result.active is True


def test_transient_forward_then_normal_clears_pending_evidence_without_activation():
    state, _ = tick(DistanceTemporalState(), Kind.NORMAL, -1)
    state, result = hold(Kind.BODY_FORWARD_CLOSE, 9, state=state)
    assert result.active is False
    state, result = tick(state, Kind.NORMAL, 10)
    assert result.accumulated_sec == 0
    assert result.candidate_classification is None
    assert result.active is False
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 11)
    assert result.accumulated_sec == 0
    assert result.active is False


def test_face_only_is_still_pending_at_body_threshold():
    _, result = hold(Kind.FACE_ONLY_CLOSE, DEMO.body_forward_enter_sec)
    assert result.active is False


@pytest.mark.parametrize("missing_duration", [2, 60, 600])
def test_unknown_freezes_pending_and_does_not_credit_either_missing_edge(missing_duration):
    state, _ = hold(Kind.BODY_FORWARD_CLOSE, 8)
    state, result = tick(state, Kind.UNKNOWN, 9)
    assert result.accumulated_sec == 8
    state, result = tick(state, Kind.UNKNOWN, 9 + missing_duration)
    assert result.accumulated_sec == 8
    resume = 10 + missing_duration
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, resume)
    assert result.accumulated_sec == 8
    assert result.active is False
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, resume + 1)
    assert result.active is False
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, resume + 2)
    assert result.active is True


@pytest.mark.parametrize("timing", [DEMO, NORMAL])
@pytest.mark.parametrize("recovery_kind", [Kind.NORMAL, Kind.BACKREST_AWAY])
def test_recovery_boundary_and_no_close_backrest_semantics(timing, recovery_kind):
    state, _ = hold(Kind.BODY_FORWARD_CLOSE, timing.body_forward_enter_sec, timing=timing)
    start = timing.body_forward_enter_sec + 1
    state, result = hold(
        recovery_kind, timing.recovery_sec - 1, state=state, start=start, timing=timing,
    )
    assert result.active is True
    assert result.accumulated_sec == 0
    state, result = tick(state, recovery_kind, start + timing.recovery_sec - 0.001, timing)
    assert result.active is True
    state, result = tick(state, recovery_kind, start + timing.recovery_sec, timing)
    assert result.active is False
    assert result.recovery_sec == 0
    assert "recovered" in result.reasons


def test_unknown_freezes_latched_evidence_and_partial_recovery():
    state, _ = hold(Kind.BODY_FORWARD_CLOSE, 10)
    state, _ = hold(Kind.NORMAL, 2, state=state, start=11)
    for t in (14, 15, 100):
        state, result = tick(state, Kind.UNKNOWN, t)
        assert result.active is True
        assert result.recovery_sec == 2
        assert result.reasons == ("unavailable_freeze",)
    state, result = tick(state, Kind.NORMAL, 101)
    assert result.recovery_sec == 2
    state, result = hold(Kind.NORMAL, 3, state=state, start=101)
    assert result.active is False


def test_close_interrupts_recovery_and_requires_fresh_full_recovery():
    state, _ = hold(Kind.BODY_FORWARD_CLOSE, 10)
    state, _ = hold(Kind.NORMAL, 4, state=state, start=11)
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 16)
    assert result.active is True
    assert result.recovery_sec == 0
    state, result = hold(Kind.NORMAL, 4, state=state, start=17)
    assert result.active is True
    state, result = tick(state, Kind.NORMAL, 22)
    assert result.active is False


def test_backrest_alone_never_activates_forward_evidence():
    _, result = hold(Kind.BACKREST_AWAY, 1000)
    assert result.active is False
    assert result.accumulated_sec == 0
    assert result.sustained_classification is None
    assert "backrest_observation_only" in result.reasons


def test_forward_types_do_not_pool_time_and_qualified_type_replaces_latched_type():
    state, _ = hold(Kind.FACE_ONLY_CLOSE, 19)
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 20)
    assert result.accumulated_sec == 0
    assert result.active is False
    state, result = hold(Kind.BODY_FORWARD_CLOSE, 10, state=state, start=20)
    assert result.sustained_classification is Kind.BODY_FORWARD_CLOSE
    state, result = hold(Kind.FACE_ONLY_CLOSE, 19, state=state, start=31)
    assert result.sustained_classification is Kind.BODY_FORWARD_CLOSE
    state, result = tick(state, Kind.FACE_ONLY_CLOSE, 51)
    assert result.sustained_classification is Kind.FACE_ONLY_CLOSE


@pytest.mark.parametrize("timing", [DEMO, NORMAL])
def test_absent_immediately_resets_every_timer_and_return_starts_clean(timing):
    state, _ = hold(Kind.BODY_FORWARD_CLOSE, timing.body_forward_enter_sec, timing=timing)
    now = timing.body_forward_enter_sec + 1
    state, result = tick(state, Kind.UNKNOWN, now, timing, seated=False)
    assert state == DistanceTemporalState(last_t=now)
    assert result.active is False
    assert result.reasons == ("absent_reset",)
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, now + 1, timing)
    assert result.accumulated_sec == 0


def test_gap_at_limit_counts_but_long_gap_does_not_infer_sustained_time():
    state, _ = tick(DistanceTemporalState(), Kind.BODY_FORWARD_CLOSE, 0)
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 5)
    assert result.accumulated_sec == 5
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 10.001)
    assert result.accumulated_sec == 5
    assert "gap_not_accumulated" in result.reasons
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 15.001)
    assert result.active is True


def test_duplicate_or_backwards_timestamp_does_not_move_high_water_mark():
    state, _ = hold(Kind.BODY_FORWARD_CLOSE, 8)
    for t in (8, 7, -100):
        unchanged, result = tick(state, Kind.NORMAL, t)
        assert unchanged is state
        assert result.reasons == ("timestamp_ignored",)
    state, result = tick(state, Kind.BODY_FORWARD_CLOSE, 9)
    assert result.accumulated_sec == 9


@pytest.mark.parametrize("now", [math.nan, math.inf, -math.inf])
def test_nonfinite_timestamp_is_rejected(now):
    with pytest.raises(ValueError, match="timestamp"):
        tick(DistanceTemporalState(), Kind.NORMAL, now)


@pytest.mark.parametrize("field", [
    "body_forward_enter_sec", "face_only_enter_sec", "recovery_sec", "max_gap_sec",
])
@pytest.mark.parametrize("invalid", [0, -1, math.nan, math.inf])
def test_invalid_configuration_rejected(field, invalid):
    values = dict(body_forward_enter_sec=10, face_only_enter_sec=20, recovery_sec=5, max_gap_sec=5)
    values[field] = invalid
    with pytest.raises(ValueError):
        DistanceEvidenceTiming(**values)


def test_config_ordering_immutability_and_result_serialization():
    with pytest.raises(ValueError, match="weaker"):
        DistanceEvidenceTiming(20, 10, 5)
    with pytest.raises(FrozenInstanceError):
        DEMO.recovery_sec = 1
    original = DistanceTemporalState()
    updated, result = tick(original, Kind.BODY_FORWARD_CLOSE, 0)
    assert original.last_t is None
    assert updated.last_t == 0
    value = result.as_dict()
    assert value["instantaneous_classification"] == "BODY_FORWARD_CLOSE"
    assert value["candidate_classification"] == "BODY_FORWARD_CLOSE"
    assert "sustained_classification" not in value
    assert "score" not in value
    assert "state" not in value
    assert None not in value.values()
