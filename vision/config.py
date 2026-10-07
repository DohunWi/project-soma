"""Vision engineering configuration and personalized EAR threshold selection."""

from __future__ import annotations

import math
from dataclasses import dataclass


EAR_FALLBACK_CLOSED = 0.21
EAR_FALLBACK_OPEN = 0.25

# Provisional values from one subject's two guided recordings. They are
# engineering candidates, not universal physiological or medical thresholds.
EAR_CLOSED_RATIO = 0.225
EAR_OPEN_RATIO = 0.50


@dataclass(frozen=True)
class EarThresholdPolicy:
    """Configurable mapping from an OPEN EAR baseline to blink thresholds."""

    closed_ratio: float = EAR_CLOSED_RATIO
    open_ratio: float = EAR_OPEN_RATIO
    fallback_closed: float = EAR_FALLBACK_CLOSED
    fallback_open: float = EAR_FALLBACK_OPEN

    def __post_init__(self) -> None:
        values = (
            self.closed_ratio,
            self.open_ratio,
            self.fallback_closed,
            self.fallback_open,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("EAR threshold policy values must be finite and positive")
        if self.open_ratio <= self.closed_ratio:
            raise ValueError("open_ratio must be greater than closed_ratio")
        if self.fallback_open <= self.fallback_closed:
            raise ValueError("fallback_open must be greater than fallback_closed")


@dataclass(frozen=True)
class EarThresholdSelection:
    """Resolved thresholds plus enough context for startup diagnostics."""

    closed_threshold: float
    open_threshold: float
    open_ear_baseline: float | None
    personalized: bool


DEFAULT_EAR_THRESHOLD_POLICY = EarThresholdPolicy()

# Phase B.5 observational lean classifier. These provisional engineering
# thresholds come from one subject and one webcam installation. In that setup,
# positive offset mapped to the user's anatomical LEFT and negative to RIGHT;
# mirrored/different camera setups must verify that mapping independently.
LEAN_LEFT_ENTRY = 0.20
LEAN_LEFT_RELEASE = 0.10
LEAN_RIGHT_ENTRY = -0.15
LEAN_RIGHT_RELEASE = -0.08


@dataclass(frozen=True)
class LeanThresholdPolicy:
    """Hysteresis thresholds for observational face lateral direction."""

    left_entry: float = LEAN_LEFT_ENTRY
    left_release: float = LEAN_LEFT_RELEASE
    right_entry: float = LEAN_RIGHT_ENTRY
    right_release: float = LEAN_RIGHT_RELEASE

    def __post_init__(self) -> None:
        values = (
            self.left_entry,
            self.left_release,
            self.right_entry,
            self.right_release,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("lean thresholds must be finite")
        if not self.right_entry < self.right_release < self.left_release < self.left_entry:
            raise ValueError("lean thresholds must define ordered entry/release bands")


DEFAULT_LEAN_THRESHOLD_POLICY = LeanThresholdPolicy()


def resolve_ear_thresholds(
    open_ear_baseline: object,
    policy: EarThresholdPolicy = DEFAULT_EAR_THRESHOLD_POLICY,
) -> EarThresholdSelection:
    """Return personalized thresholds, or the established absolute fallback."""
    try:
        baseline = float(open_ear_baseline)
    except (TypeError, ValueError):
        baseline = math.nan

    if math.isfinite(baseline) and baseline > 0:
        closed = baseline * policy.closed_ratio
        opened = baseline * policy.open_ratio
        if 0 < closed < opened < baseline and all(
            math.isfinite(value) for value in (closed, opened)
        ):
            return EarThresholdSelection(closed, opened, baseline, True)

    return EarThresholdSelection(
        policy.fallback_closed,
        policy.fallback_open,
        None,
        False,
    )
