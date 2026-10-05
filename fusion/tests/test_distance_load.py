"""Single-slot Phase C load tests through the real classifier/temporal/Fusion path."""
from dataclasses import replace
import math

import pytest

from fusion.config import DistancePenaltyConfig
from fusion.distance_evidence import DistanceEvidenceClass as Kind
from fusion.distance_evidence import classify_distance_evidence
from fusion.distance_temporal import DistanceTemporalState, step as temporal_step
from fusion.load import SomaLoadState, score_integer, update_distance_load
from fusion.state import FusionState, step
from server.config import DEMO_PROFILE as DEMO, NORMAL_PROFILE as NORMAL


class DistanceRun:
    def __init__(self, profile=DEMO):
        self.profile = profile
        self.fusion = FusionState()
        self.temporal = DistanceTemporalState()
        self.evidence = None

    def tick(self, kind, t, *, pressure=None, face_distance=60.0, inject=True):
        close = kind in (Kind.BODY_FORWARD_CLOSE, Kind.FACE_ONLY_CLOSE)
        away = kind in (Kind.BODY_FORWARD_CLOSE, Kind.BACKREST_AWAY)
        seated = kind != "ABSENT"
        instantaneous = classify_distance_evidence(
            face_distance_cm=face_distance,
            face_approach_delta_cm=18.0 if close else 0.0,
            backrest_departure_delta_mm=130.0 if away else 0.0,
            vision_available=kind is not Kind.UNKNOWN,
            chair_ir_available=True,
            seated=seated,
        )
        self.temporal, self.evidence = temporal_step(
            self.temporal, instantaneous, t, timing=self.profile.distance_evidence_timing,
        )
        # Balanced movement isolates distance from static/balance score contributions.
        sample = {
            "pressure": pressure if pressure is not None else (
                [300 + (int(t) % 2) * 20] * 4 if seated else [1] * 4
            ),
            "ir": [380 if away else 250],
        }
        if kind is not Kind.UNKNOWN:
            sample.update(face_detected=True, face_distance_cm=face_distance)
        self.fusion, decision = step(
            self.fusion, sample, t,
            timing=self.profile.fusion,
            load_config=self.profile.load,
            distance_evidence=self.evidence if inject else None,
            distance_timing=self.profile.distance_evidence_timing,
        )
        return decision

    def hold(self, kind, start, end, **kwargs):
        for t in range(start, end + 1):
            decision = self.tick(kind, t, **kwargs)
        return decision


@pytest.mark.parametrize("profile", [DEMO, NORMAL])
@pytest.mark.parametrize("kind,field,rate_field", [
    (Kind.BODY_FORWARD_CLOSE, "body_forward_enter_sec", "body_rate_per_sec"),
    (Kind.FACE_ONLY_CLOSE, "face_only_enter_sec", "face_only_rate_per_sec"),
])
def test_entry_boundary_has_no_early_charge_and_first_eligible_second_accumulates(
    profile, kind, field, rate_field,
):
    run = DistanceRun(profile)
    enter = int(getattr(profile.distance_evidence_timing, field))
    run.hold(kind, 0, enter - 1)
    assert run.fusion.load.distance_penalty == 0
    run.tick(kind, enter)
    assert run.evidence.active is True
    assert run.fusion.load.distance_penalty == 0
    run.tick(kind, enter + 1)
    assert run.fusion.load.distance_penalty == pytest.approx(
        getattr(profile.load.distance, rate_field)
    )


@pytest.mark.parametrize("kind,enter", [
    (Kind.BODY_FORWARD_CLOSE, 10), (Kind.FACE_ONLY_CLOSE, 20),
])
def test_transient_episode_never_penalizes(kind, enter):
    run = DistanceRun()
    run.hold(kind, 0, enter - 1)
    run.hold(Kind.NORMAL, enter, enter + 20)
    assert run.fusion.load.distance_penalty == 0


@pytest.mark.parametrize("profile,active_sec", [(DEMO, 10), (NORMAL, 100)])
def test_body_stronger_for_equal_eligible_time_and_only_one_slot(profile, active_sec):
    penalties = []
    for kind, enter in (
        (Kind.BODY_FORWARD_CLOSE, profile.distance_evidence_timing.body_forward_enter_sec),
        (Kind.FACE_ONLY_CLOSE, profile.distance_evidence_timing.face_only_enter_sec),
    ):
        run = DistanceRun(profile)
        decision = run.hold(kind, 0, int(enter) + active_sec)
        penalties.append(run.fusion.load.distance_penalty)
        rate = (profile.load.distance.body_rate_per_sec
                if kind is Kind.BODY_FORWARD_CLOSE else profile.load.distance.face_only_rate_per_sec)
        assert run.fusion.load.distance_penalty == pytest.approx(rate * active_sec)
        assert run.fusion.load.static_penalty == 0
        assert run.fusion.load.balance_penalty == 0
        assert run.fusion.load.blink_penalty == 0
        assert decision["score"] == score_integer(run.fusion.load)
    assert penalties[0] > penalties[1] > 0


@pytest.mark.parametrize("profile", [DEMO, NORMAL])
@pytest.mark.parametrize("kind,cap_field", [
    (Kind.BODY_FORWARD_CLOSE, "body_cap"), (Kind.FACE_ONLY_CLOSE, "face_only_cap"),
])
def test_long_episodes_are_capped_and_score_bounded(profile, kind, cap_field):
    run = DistanceRun(profile)
    for t in range(0, 4001):
        decision = run.tick(kind, t)
        assert 0 <= decision["score"] <= 100
        cap = getattr(profile.load.distance, cap_field)
        assert 0 <= run.fusion.load.distance_penalty <= cap
    assert run.fusion.load.distance_penalty == pytest.approx(
        getattr(profile.load.distance, cap_field)
    )


def test_backrest_alone_never_accumulates():
    run = DistanceRun()
    decision = run.hold(Kind.BACKREST_AWAY, 0, 200)
    assert run.fusion.load.distance_penalty == 0
    assert decision["score"] == 100


@pytest.mark.parametrize("profile", [DEMO, NORMAL])
@pytest.mark.parametrize("kind", [Kind.NORMAL, Kind.BACKREST_AWAY])
def test_known_recovery_waits_for_temporal_release_then_recovers_linearly(profile, kind):
    run = DistanceRun(profile)
    entry = int(profile.distance_evidence_timing.body_forward_enter_sec)
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, entry + 10)
    residual = run.fusion.load.distance_penalty
    start = entry + 11
    release = start + int(profile.distance_evidence_timing.recovery_sec)
    run.hold(kind, start, release)
    assert run.evidence.active is False
    assert run.fusion.load.distance_penalty == pytest.approx(residual)
    run.tick(kind, release + 1)
    recovery_rate = profile.load.distance.body_cap / profile.load.distance.recovery_sec
    assert run.fusion.load.distance_penalty == pytest.approx(max(0, residual - recovery_rate))
    run.hold(kind, release + 2, release + int(profile.load.distance.recovery_sec) + 1)
    assert run.fusion.load.distance_penalty == 0


@pytest.mark.parametrize("kind,enter", [
    (Kind.BODY_FORWARD_CLOSE, 10), (Kind.FACE_ONLY_CLOSE, 20),
])
def test_unknown_after_active_freezes_residual_and_both_edges(kind, enter):
    run = DistanceRun()
    run.hold(kind, 0, enter + 10)
    residual = run.fusion.load.distance_penalty
    run.hold(Kind.UNKNOWN, enter + 11, enter + 40)
    assert run.evidence.active is True
    assert run.fusion.load.distance_penalty == residual
    run.tick(kind, enter + 41)
    assert run.fusion.load.distance_penalty == residual
    run.tick(kind, enter + 42)
    assert run.fusion.load.distance_penalty > residual


def test_unknown_does_not_fabricate_normal_recovery_or_charge_long_missing_gap():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 20)
    run.hold(Kind.NORMAL, 21, 26)
    assert run.evidence.active is False
    residual = run.fusion.load.distance_penalty
    run.tick(Kind.UNKNOWN, 27)
    run.tick(Kind.UNKNOWN, 1000)
    run.tick(Kind.NORMAL, 1001)
    assert run.fusion.load.distance_penalty == residual
    run.tick(Kind.NORMAL, 1002)
    assert run.fusion.load.distance_penalty == pytest.approx(residual - 0.75)


def test_unknown_after_just_entered_active_never_creates_penalty():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 10)
    assert run.evidence.active is True
    assert run.fusion.load.distance_penalty == 0
    run.hold(Kind.UNKNOWN, 11, 100)
    assert run.evidence.active is True
    assert run.fusion.load.distance_penalty == 0


def test_body_to_face_transition_does_not_charge_latched_body_or_erase_body_residual():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 30)
    assert run.fusion.load.distance_penalty == 10
    run.hold(Kind.FACE_ONLY_CLOSE, 31, 80)
    assert run.evidence.sustained_classification is Kind.FACE_ONLY_CLOSE
    assert run.fusion.load.distance_penalty == 10  # Above weaker cap; held, not added or erased.
    run.tick(Kind.BODY_FORWARD_CLOSE, 81)
    assert run.fusion.load.distance_penalty == 10
    run.hold(Kind.BODY_FORWARD_CLOSE, 82, 91)
    assert run.fusion.load.distance_penalty == 10
    run.tick(Kind.BODY_FORWARD_CLOSE, 92)
    assert run.fusion.load.distance_penalty == 10.5


def test_sample_crossing_entry_charges_only_post_boundary_fraction():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 9)
    run.tick(Kind.BODY_FORWARD_CLOSE, 11)
    assert run.fusion.load.distance_penalty == 0.5  # 9->11: only 10->11 is eligible.


def test_gap_over_five_seconds_never_charges_unobserved_time():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 20)
    residual = run.fusion.load.distance_penalty
    run.tick(Kind.BODY_FORWARD_CLOSE, 26)
    assert run.fusion.load.distance_penalty == residual
    run.tick(Kind.BODY_FORWARD_CLOSE, 27)
    assert run.fusion.load.distance_penalty == residual + 0.5


def test_backwards_recovery_sample_does_not_justify_recovery_on_either_edge():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 20)
    run.hold(Kind.NORMAL, 21, 26)
    residual = run.fusion.load.distance_penalty
    run.tick(Kind.NORMAL, 25)
    assert run.fusion.load.distance_penalty == residual
    run.tick(Kind.NORMAL, 27)
    assert run.fusion.load.distance_penalty == residual
    run.tick(Kind.NORMAL, 28)
    assert run.fusion.load.distance_penalty == pytest.approx(residual - 0.75)


def test_absent_resets_temporal_but_recovers_residual_like_existing_load():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 20)
    run.tick("ABSENT", 21)
    assert run.evidence.active is False
    assert run.fusion._last_distance_evidence is None
    assert run.fusion.load.distance_penalty == pytest.approx(5 - 0.75)
    run.tick(Kind.BODY_FORWARD_CLOSE, 22)
    assert run.fusion.load.distance_penalty == pytest.approx(4.25)
    assert run.evidence.accumulated_sec == 0


def test_absent_weighted_recovery_uses_previous_absence_and_crosses_both_boundaries():
    load = SomaLoadState(distance_penalty=15, absent_sec=59)
    result = update_distance_load(
        load, previous_evidence=None, evidence=None, seated=False, dt=122,
        config=NORMAL.load, timing=NORMAL.distance_evidence_timing,
    )
    assert result.distance_penalty == pytest.approx(15 - 0.05 * 183)
    assert result.absent_sec == 59  # Chair update advances it once, after this calculation.


def test_missing_temporal_evidence_never_accumulates_or_recovers():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 20)
    residual = run.fusion.load.distance_penalty
    run.hold(Kind.NORMAL, 21, 50, inject=False)
    assert run.fusion.load.distance_penalty == residual


def test_static_balance_and_distance_use_one_composite_score():
    run = DistanceRun()
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 100, pressure=[700, 300, 700, 300])
    assert run.fusion.load.static_penalty == 25
    assert run.fusion.load.balance_penalty == 25
    assert run.fusion.load.distance_penalty == 15
    assert score_integer(run.fusion.load) == 35


def test_demo_visible_change_and_normal_long_duration_scale():
    demo = DistanceRun()
    assert demo.hold(Kind.BODY_FORWARD_CLOSE, 0, 12)["score"] == 99
    normal = DistanceRun(NORMAL)
    assert normal.hold(Kind.BODY_FORWARD_CLOSE, 0, 299)["score"] == 100
    assert normal.hold(Kind.BODY_FORWARD_CLOSE, 300, 400)["score"] == 99


def test_absolute_close_state_is_preserved_and_never_adds_a_second_numeric_penalty():
    relative = DistanceRun()
    decision = relative.hold(Kind.BODY_FORWARD_CLOSE, 0, 20, face_distance=42)
    assert "close_distance" in decision["reasons"]
    assert decision["state"] == "CAUTION"
    assert relative.fusion.load.distance_penalty == 5
    legacy_only = DistanceRun()
    decision = legacy_only.hold(Kind.NORMAL, 0, 20, face_distance=42)
    assert "close_distance" in decision["reasons"]
    assert decision["state"] == "CAUTION"
    assert decision["score"] == 100
    assert legacy_only.fusion.load.distance_penalty == 0


@pytest.mark.parametrize("values", [
    (0, 0.2, 15, 8, 20), (0.5, math.nan, 15, 8, 20),
    (0.5, 0.2, 26, 8, 20), (0.5, 0.2, 8, 8, 20),
    (0.5, 0.6, 15, 8, 20), (0.5, 0.2, 15, 8, 0),
])
def test_invalid_policy_is_rejected(values):
    with pytest.raises(ValueError):
        DistancePenaltyConfig(*values)


def test_policy_rates_and_caps_are_injectable():
    profile = replace(DEMO, load=replace(DEMO.load, distance=DistancePenaltyConfig(
        body_rate_per_sec=0.1, face_only_rate_per_sec=0.05,
        body_cap=3, face_only_cap=2, recovery_sec=60,
    )))
    run = DistanceRun(profile)
    run.hold(Kind.BODY_FORWARD_CLOSE, 0, 100)
    assert run.fusion.load.distance_penalty == 3
