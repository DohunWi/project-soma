"""Immutable State timing and SOMA Load Model profiles."""
from dataclasses import dataclass
from math import isfinite


PenaltyCurve = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class FusionTiming:
    """Time-based thresholds used by the profile-independent Fusion algorithm."""

    static_caution_sec: float
    static_danger_sec: float
    imbalance_caution_sec: float
    low_blink_caution_sec: float
    low_blink_danger_sec: float
    close_distance_caution_sec: float
    max_gap_sec: float


@dataclass(frozen=True)
class DistanceEvidenceTiming:
    """Provisional observation timings; not score or medical thresholds."""

    body_forward_enter_sec: float
    face_only_enter_sec: float
    recovery_sec: float
    max_gap_sec: float = 5.0

    def __post_init__(self):
        for value in (
            self.body_forward_enter_sec,
            self.face_only_enter_sec,
            self.recovery_sec,
            self.max_gap_sec,
        ):
            if not isfinite(value) or value <= 0:
                raise ValueError("distance evidence timings must be positive and finite")
        if self.face_only_enter_sec < self.body_forward_enter_sec:
            raise ValueError("weaker face-only evidence must not enter faster than body evidence")


# Engineering candidates only: BODY uses the existing absolute-close time scale;
# weaker FACE_ONLY needs twice as long. Recovery is deliberately shorter than entry.
# Demo compresses the observation for a short demonstration, not physiological time.
DEMO_DISTANCE_EVIDENCE_TIMING = DistanceEvidenceTiming(
    body_forward_enter_sec=10.0,
    face_only_enter_sec=20.0,
    recovery_sec=5.0,
)
NORMAL_DISTANCE_EVIDENCE_TIMING = DistanceEvidenceTiming(
    body_forward_enter_sec=300.0,
    face_only_enter_sec=600.0,
    recovery_sec=30.0,
)


@dataclass(frozen=True)
class SomaLoadConfig:
    """Chair-based SOMA Load Model v1 engineering parameters."""

    static_curve: PenaltyCurve
    balance_curve: PenaltyCurve
    static_recovery_sec: float
    balance_recovery_sec: float
    max_penalty: float = 25.0
    absent_medium_sec: float = 60.0
    absent_long_sec: float = 180.0
    absent_short_multiplier: float = 1.0
    absent_medium_multiplier: float = 1.5
    absent_long_multiplier: float = 2.0


SOMA_LOAD_MODEL_VERSION = "soma_load_v1"

NORMAL_STATIC_CURVE = (
    (0.0, 0.0),
    (300.0, 0.0),
    (600.0, 5.0),
    (1200.0, 10.0),
    (1800.0, 15.0),
    (2700.0, 20.0),
    (3600.0, 25.0),
)

DEMO_STATIC_CURVE = (
    (0.0, 0.0),
    (5.0, 0.0),
    (10.0, 5.0),
    (20.0, 10.0),
    (30.0, 15.0),
    (45.0, 20.0),
    (60.0, 25.0),
)

NORMAL_BALANCE_CURVE = (
    (0.0, 0.0),
    (5.0, 0.0),
    (15.0, 5.0),
    (30.0, 10.0),
    (60.0, 15.0),
    (120.0, 20.0),
    (180.0, 25.0),
)

# Demo 전용 시간축 축소값입니다. 의학적 또는 실사용 기준이 아닙니다.
DEMO_BALANCE_CURVE = (
    (0.0, 0.0),
    (2.0, 0.0),
    (5.0, 5.0),
    (10.0, 10.0),
    (20.0, 15.0),
    (30.0, 20.0),
    (45.0, 25.0),
)

DEMO_SOMA_LOAD_CONFIG = SomaLoadConfig(
    static_curve=DEMO_STATIC_CURVE,
    balance_curve=DEMO_BALANCE_CURVE,
    static_recovery_sec=10.0,
    balance_recovery_sec=6.0,
)

NORMAL_SOMA_LOAD_CONFIG = SomaLoadConfig(
    static_curve=NORMAL_STATIC_CURVE,
    balance_curve=NORMAL_BALANCE_CURVE,
    static_recovery_sec=600.0,
    balance_recovery_sec=180.0,
)


DEMO_FUSION_TIMING = FusionTiming(
    static_caution_sec=10.0,
    static_danger_sec=20.0,
    imbalance_caution_sec=10.0,
    low_blink_caution_sec=10.0,
    low_blink_danger_sec=20.0,
    close_distance_caution_sec=10.0,
    max_gap_sec=5.0,
)

NORMAL_FUSION_TIMING = FusionTiming(
    static_caution_sec=1200.0,
    static_danger_sec=2700.0,
    imbalance_caution_sec=300.0,
    low_blink_caution_sec=300.0,
    low_blink_danger_sec=900.0,
    close_distance_caution_sec=300.0,
    max_gap_sec=5.0,
)
