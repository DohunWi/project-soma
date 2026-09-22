"""Fusion timing profiles shared by every state-transition path."""
from dataclasses import dataclass


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
