"""Pure SOMA Load Model v1 calculations for Chair and relative distance evidence.

The score is an observed load-awareness indicator, not a medical, disease-risk,
or posture-correctness score. Blink remains reserved; calibrated sustained
distance evidence feeds one slot. Missing sensors freeze that slot.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP

from fusion.config import DistanceEvidenceTiming, PenaltyCurve, SomaLoadConfig
from fusion.distance_evidence import DistanceEvidenceClass
from fusion.distance_temporal import DistanceTemporalResult, FORWARD_CLASSES, RECOVERY_CLASSES


@dataclass(frozen=True)
class SomaLoadState:
    """Penalty memory carried by one immutable FusionState."""

    static_penalty: float = 0.0
    balance_penalty: float = 0.0
    blink_penalty: float = 0.0
    distance_penalty: float = 0.0
    absent_sec: float = 0.0


def piecewise_linear(curve: PenaltyCurve, value: float) -> float:
    """Interpolate a sorted engineering curve, clamping outside its endpoints."""
    if not curve:
        raise ValueError("penalty curve must not be empty")
    x = float(value)
    if x <= curve[0][0]:
        return float(curve[0][1])
    if x >= curve[-1][0]:
        return float(curve[-1][1])

    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            if x1 <= x0:
                raise ValueError("penalty curve x values must be strictly increasing")
            ratio = (x - x0) / (x1 - x0)
            return float(y0 + (y1 - y0) * ratio)
    return float(curve[-1][1])


def clamp_penalty(value: float, maximum: float = 25.0) -> float:
    return min(max(float(value), 0.0), float(maximum))


def score_float(load: SomaLoadState) -> float:
    """Return the raw composite score used by future policies."""
    total = (
        load.static_penalty
        + load.balance_penalty
        + load.blink_penalty
        + load.distance_penalty
    )
    return min(max(100.0 - total, 0.0), 100.0)


def round_half_up(value: float) -> int:
    """Round a non-negative score without Python's banker's rounding."""
    bounded = min(max(float(value), 0.0), 100.0)
    return int(Decimal(str(bounded)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def score_integer(load: SomaLoadState) -> int:
    return round_half_up(score_float(load))


def update_distance_load(
    load: SomaLoadState,
    *,
    previous_evidence: DistanceTemporalResult | None,
    evidence: DistanceTemporalResult | None,
    seated: bool,
    dt: float,
    config: SomaLoadConfig,
    timing: DistanceEvidenceTiming,
) -> SomaLoadState:
    """Update ONE residual distance penalty from currently justified intervals.

Call before update_chair_load so both use the same previous absent_sec. Absolute
face distance never enters this numeric slot. A remembered active classification
alone cannot justify accumulation or recovery during sensor unavailability.
"""
    policy = config.distance
    elapsed = max(float(dt), 0.0)
    if not seated:
        penalty = _recover(
            load.distance_penalty,
            _absent_weighted_seconds(load.absent_sec, elapsed, config),
            policy.recovery_sec,
            policy.body_cap,
        )
        return replace(load, distance_penalty=penalty)
    if evidence is None or previous_evidence is None or elapsed == 0:
        return load
    if any(reason in evidence.reasons for reason in ("timestamp_ignored", "gap_not_accumulated")):
        return load
    if "timestamp_ignored" in previous_evidence.reasons:
        return load
    current = evidence.instantaneous_classification
    previous = previous_evidence.instantaneous_classification
    if current is DistanceEvidenceClass.UNKNOWN or previous is DistanceEvidenceClass.UNKNOWN:
        return load

    penalty = load.distance_penalty
    if (
        current in FORWARD_CLASSES
        and previous is current
        and evidence.sustained_classification is current
        and evidence.candidate_classification is current
        and previous_evidence.candidate_classification is current
    ):
        body = current is DistanceEvidenceClass.BODY_FORWARD_CLOSE
        enter = timing.body_forward_enter_sec if body else timing.face_only_enter_sec
        # Integrate only the part AFTER entry; a boundary sample adds zero.
        # Temporal deltas exclude UNKNOWN edges and gaps rather than charging wall time.
        eligible = min(elapsed, max(
            max(evidence.accumulated_sec - enter, 0.0)
            - max(previous_evidence.accumulated_sec - enter, 0.0),
            0.0,
        ))
        rate = policy.body_rate_per_sec if body else policy.face_only_rate_per_sec
        cap = policy.body_cap if body else policy.face_only_cap
        # Weaker evidence must not erase an earlier BODY residual above its cap.
        penalty = max(penalty, min(penalty + rate * eligible, cap))
    elif (
        current in RECOVERY_CLASSES
        and previous in RECOVERY_CLASSES
        and not evidence.active
        and not previous_evidence.active
    ):
        # Both ends must be known and recovered; never infer NORMAL from dropout.
        penalty = _recover(penalty, elapsed, policy.recovery_sec, policy.body_cap)
    return replace(load, distance_penalty=clamp_penalty(penalty, policy.body_cap))


def update_chair_load(
    load: SomaLoadState,
    *,
    previous_static_sec: float,
    static_sec: float,
    previous_imbalance_sec: float,
    imbalance_sec: float,
    balance: str,
    seated: bool,
    dt: float,
    config: SomaLoadConfig,
) -> SomaLoadState:
    """Accumulate Chair penalties or recover them using validated elapsed time."""
    elapsed = max(float(dt), 0.0)
    if not seated:
        weighted_absent = _absent_weighted_seconds(
            load.absent_sec,
            elapsed,
            config,
        )
        return replace(
            load,
            static_penalty=_recover(
                load.static_penalty,
                weighted_absent,
                config.static_recovery_sec,
                config.max_penalty,
            ),
            balance_penalty=_recover(
                load.balance_penalty,
                weighted_absent,
                config.balance_recovery_sec,
                config.max_penalty,
            ),
            absent_sec=load.absent_sec + elapsed,
        )

    static_previous_target = piecewise_linear(
        config.static_curve,
        previous_static_sec,
    )
    static_target = piecewise_linear(config.static_curve, static_sec)
    static_delta = max(static_target - static_previous_target, 0.0)
    if static_delta > 0.0:
        static_penalty = load.static_penalty + static_delta
    elif static_target == 0.0:
        static_penalty = _recover(
            load.static_penalty,
            elapsed,
            config.static_recovery_sec,
            config.max_penalty,
        )
    else:
        static_penalty = load.static_penalty

    balance_previous_target = piecewise_linear(
        config.balance_curve,
        previous_imbalance_sec,
    )
    balance_target = piecewise_linear(config.balance_curve, imbalance_sec)
    balance_delta = max(balance_target - balance_previous_target, 0.0)
    if balance == "CENTER":
        balance_penalty = _recover(
            load.balance_penalty,
            elapsed,
            config.balance_recovery_sec,
            config.max_penalty,
        )
    else:
        balance_penalty = load.balance_penalty + balance_delta

    return replace(
        load,
        static_penalty=clamp_penalty(static_penalty, config.max_penalty),
        balance_penalty=clamp_penalty(balance_penalty, config.max_penalty),
        absent_sec=0.0,
    )


def _recover(
    penalty: float,
    weighted_sec: float,
    recovery_sec: float,
    maximum: float,
) -> float:
    if recovery_sec <= 0:
        raise ValueError("recovery_sec must be positive")
    reduction = maximum / recovery_sec * weighted_sec
    return clamp_penalty(penalty - reduction, maximum)


def _absent_weighted_seconds(
    absent_start: float,
    dt: float,
    config: SomaLoadConfig,
) -> float:
    """Integrate piecewise ABSENT multipliers across every crossed boundary."""
    start = max(float(absent_start), 0.0)
    end = start + max(float(dt), 0.0)
    if end <= start:
        return 0.0

    segments = (
        (0.0, config.absent_medium_sec, config.absent_short_multiplier),
        (
            config.absent_medium_sec,
            config.absent_long_sec,
            config.absent_medium_multiplier,
        ),
        (config.absent_long_sec, float("inf"), config.absent_long_multiplier),
    )
    weighted = 0.0
    for segment_start, segment_end, multiplier in segments:
        overlap_start = max(start, segment_start)
        overlap_end = min(end, segment_end)
        if overlap_end > overlap_start:
            weighted += (overlap_end - overlap_start) * multiplier
    return weighted
