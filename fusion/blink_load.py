"""Normal blink load policy and reliability diagnostics.

``score-v1`` is the Normal profile default; explicit CLI or environment
configuration can select ``legacy`` for comparison.  Demo always keeps the
legacy policy.  This module never infers authentication, camera quality, or
personalization from a blink-rate value alone: a valid personalized OPEN EAR
baseline must accompany the observation.
"""
from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite

from fusion.load import SomaLoadState, clamp_penalty


class BlinkPolicyMode(str, Enum):
    LEGACY = "legacy"
    SCORE_V1 = "score-v1"


@dataclass(frozen=True)
class BlinkPenaltyConfig:
    """Provisional engineering curve; not a physiological threshold."""

    grace_sec: float = 300.0
    full_penalty_sec: float = 900.0
    max_penalty: float = 10.0
    recovery_sec: float = 300.0
    rate_low: float = 8.0
    rate_ok: float = 11.0

    def __post_init__(self):
        values = (
            self.grace_sec,
            self.full_penalty_sec,
            self.max_penalty,
            self.recovery_sec,
            self.rate_low,
            self.rate_ok,
        )
        if any(not isfinite(value) or value <= 0 for value in values):
            raise ValueError("blink policy values must be positive and finite")
        if self.full_penalty_sec <= self.grace_sec:
            raise ValueError("full penalty time must be after the grace period")
        if self.rate_ok <= self.rate_low:
            raise ValueError("blink recovery rate must be above the low threshold")


NORMAL_BLINK_PENALTY_CONFIG = BlinkPenaltyConfig()


@dataclass(frozen=True)
class BlinkReliability:
    personalized_ear: bool
    threshold_mode: str
    calibration_status: str
    open_ear_baseline: float | None
    blink_rate_baseline_usable: bool
    observation_quality: str
    policy_available: bool
    unavailable_reason: str | None

    def as_dict(self):
        result = {
            "personalized_ear": self.personalized_ear,
            "threshold_mode": self.threshold_mode,
            "calibration_status": self.calibration_status,
            "blink_rate_baseline_usable": self.blink_rate_baseline_usable,
            "observation_quality": self.observation_quality,
            "policy_available": self.policy_available,
        }
        if self.open_ear_baseline is not None:
            result["open_ear_baseline"] = self.open_ear_baseline
        if self.unavailable_reason is not None:
            result["unavailable_reason"] = self.unavailable_reason
        return result


def _positive_finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def assess_blink_reliability(sample: dict) -> BlinkReliability:
    """Classify reliability without inventing a partial-detection threshold."""
    threshold_mode = sample.get("ear_threshold_mode")
    if threshold_mode not in ("personalized", "fallback"):
        threshold_mode = "missing"
    baseline = _positive_finite(sample.get("open_ear_baseline"))
    personalized = threshold_mode == "personalized" and baseline is not None

    rate = _finite(sample.get("blink_rate"))
    detect_rate = _finite(sample.get("detect_rate"))
    if rate is None:
        quality = "unavailable"
    elif detect_rate == 0:
        quality = "no_face_in_quality_window"
    elif detect_rate is None:
        quality = "quality_unknown"
    elif detect_rate < 1:
        quality = "partial_detection"
    else:
        quality = "full_detection"

    if threshold_mode == "fallback":
        reason = "fallback_threshold"
    elif threshold_mode == "missing":
        reason = "threshold_mode_missing"
    elif baseline is None:
        reason = "personalized_baseline_invalid"
    elif rate is None:
        reason = "blink_rate_unavailable"
    elif detect_rate == 0:
        reason = "detect_rate_zero"
    else:
        reason = None

    return BlinkReliability(
        personalized_ear=personalized,
        threshold_mode=threshold_mode,
        calibration_status=str(
            sample.get("ear_calibration_status") or "unknown"
        ),
        open_ear_baseline=baseline,
        blink_rate_baseline_usable=(
            _positive_finite(sample.get("blink_rate_baseline")) is not None
        ),
        observation_quality=quality,
        policy_available=personalized and rate is not None and detect_rate != 0,
        unavailable_reason=reason,
    )


def blink_penalty_target(low_blink_sec, config=NORMAL_BLINK_PENALTY_CONFIG):
    """Policy B: zero through grace, then linear to the configured cap."""
    elapsed = max(float(low_blink_sec), 0.0)
    if elapsed <= config.grace_sec:
        return 0.0
    span = config.full_penalty_sec - config.grace_sec
    ratio = min((elapsed - config.grace_sec) / span, 1.0)
    return config.max_penalty * ratio


def update_blink_load(
    load: SomaLoadState,
    *,
    previous_low_blink_sec: float,
    low_blink_sec: float,
    blink_rate,
    detect_rate,
    reliability: BlinkReliability,
    seated: bool,
    lifecycle_paused: bool,
    elapsed_sec: float,
    config=NORMAL_BLINK_PENALTY_CONFIG,
):
    """Update only ``blink_penalty`` and return an observer-safe diagnostic."""
    elapsed = max(float(elapsed_sec), 0.0)
    rate = _finite(blink_rate)
    detect = _finite(detect_rate)
    penalty = load.blink_penalty

    if not seated or lifecycle_paused or elapsed == 0:
        reason = "lifecycle_frozen"
    elif not reliability.personalized_ear:
        reason = reliability.unavailable_reason or "ear_unreliable"
    elif rate is None:
        reason = "blink_rate_unavailable"
    elif detect == 0:
        reason = "detect_rate_zero"
    elif rate < config.rate_low:
        previous_target = blink_penalty_target(previous_low_blink_sec, config)
        current_target = blink_penalty_target(low_blink_sec, config)
        penalty = clamp_penalty(
            penalty + max(current_target - previous_target, 0.0),
            config.max_penalty,
        )
        reason = "low_blink_accumulation"
    elif rate >= config.rate_ok:
        penalty = clamp_penalty(
            penalty - config.max_penalty / config.recovery_sec * elapsed,
            config.max_penalty,
        )
        reason = "healthy_rate_recovery"
    else:
        reason = "hysteresis_hold"

    updated = replace(load, blink_penalty=penalty)
    diagnostic = {
        **reliability.as_dict(),
        "blink_rate": rate,
        "detect_rate": detect,
        "low_blink_sec": round(float(low_blink_sec), 6),
        "blink_penalty": round(penalty, 6),
        "update_reason": reason,
        "experimental": True,
    }
    return updated, diagnostic
