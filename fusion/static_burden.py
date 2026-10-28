"""Cumulative static burden policy used by the Normal profile.

The v2 policy keeps posture episodes separate from one monotonic eligible-time
cursor.  A confirmed posture change starts a new episode but never recovers an
already accumulated static penalty.  ``cumulative-v2`` is the Normal profile
default; explicit CLI or environment configuration can select ``legacy`` for
comparison.  Demo always keeps the legacy policy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from fusion.config import SomaLoadConfig
from fusion.load import clamp_penalty, piecewise_linear


class StaticPolicyMode(str, Enum):
    LEGACY = "legacy"
    CUMULATIVE_V2 = "cumulative-v2"


@dataclass(frozen=True)
class StaticBurdenConfig:
    episode_grace_sec: float = 300.0
    caution_penalty: float = 10.0
    danger_penalty: float = 20.0

    def __post_init__(self):
        for name in ("episode_grace_sec", "caution_penalty", "danger_penalty"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.danger_penalty <= self.caution_penalty:
            raise ValueError("danger_penalty must be greater than caution_penalty")


@dataclass(frozen=True)
class StaticBurdenState:
    posture_episode_sec: float = 0.0
    static_eligible_total_sec: float = 0.0


NORMAL_STATIC_BURDEN_CONFIG = StaticBurdenConfig()


def update_cumulative_static_burden(
    state: StaticBurdenState,
    *,
    previous_penalty: float,
    legacy_penalty: float,
    posture_tracking: dict | None,
    previous_seated: bool,
    seated: bool,
    load_config: SomaLoadConfig,
    config: StaticBurdenConfig = NORMAL_STATIC_BURDEN_CONFIG,
):
    """Return ``(state, penalty, diagnostic)`` without touching other slots.

    Confirmed absence and a new seated interval deliberately keep the existing
    lifecycle authoritative.  During valid seated observation, only the part
    of each episode beyond ``episode_grace_sec`` advances one global curve
    cursor.  This prevents both seated recovery and repeated use of the early,
    steeper curve segment.
    """
    if not seated:
        cleared = StaticBurdenState()
        return cleared, legacy_penalty, _diagnostic(
            cleared,
            legacy_penalty,
            posture_tracking,
            config,
            reason="confirmed_absence",
            eligible_delta=0.0,
        )
    if not previous_seated:
        cleared = StaticBurdenState()
        return cleared, legacy_penalty, _diagnostic(
            cleared,
            legacy_penalty,
            posture_tracking,
            config,
            reason="new_seated_interval",
            eligible_delta=0.0,
        )
    if (
        posture_tracking is None
        or posture_tracking.get("mode") != "clock"
        or posture_tracking.get("lifecycle_paused")
        or not posture_tracking.get("sample_valid_pressure", False)
        or posture_tracking.get("data_gap")
    ):
        return state, previous_penalty, _diagnostic(
            state,
            previous_penalty,
            posture_tracking,
            config,
            reason="frozen_unavailable",
            eligible_delta=0.0,
        )

    pre_reset = posture_tracking.get("pre_reset_sustained_posture_sec") or {}
    pre_reset_episode = pre_reset.get("continuous_valid_seated")
    if not _non_negative_finite(pre_reset_episode):
        return state, previous_penalty, _diagnostic(
            state,
            previous_penalty,
            posture_tracking,
            config,
            reason="frozen_missing_clock",
            eligible_delta=0.0,
        )
    pre_reset_episode = float(pre_reset_episode)
    previous_episode = float(state.posture_episode_sec)
    # A session/lifecycle reset must never be interpreted as negative exposure.
    if pre_reset_episode + 1e-9 < previous_episode:
        return state, previous_penalty, _diagnostic(
            state,
            previous_penalty,
            posture_tracking,
            config,
            reason="frozen_clock_regression",
            eligible_delta=0.0,
        )

    previous_eligible = max(previous_episode - config.episode_grace_sec, 0.0)
    current_eligible = max(pre_reset_episode - config.episode_grace_sec, 0.0)
    eligible_delta = max(current_eligible - previous_eligible, 0.0)
    eligible_total = state.static_eligible_total_sec + eligible_delta
    mapped_curve_sec = config.episode_grace_sec + eligible_total
    target = piecewise_linear(load_config.static_curve, mapped_curve_sec)
    penalty = clamp_penalty(
        max(float(previous_penalty), target),
        load_config.max_penalty,
    )
    canonical_event = posture_tracking.get("canonical_event")
    clocks = posture_tracking.get("sustained_posture_sec") or {}
    current_episode = clocks.get("continuous_valid_seated", pre_reset_episode)
    if canonical_event is not None:
        current_episode = 0.0
    next_state = StaticBurdenState(
        posture_episode_sec=float(current_episode),
        static_eligible_total_sec=eligible_total,
    )
    return next_state, penalty, _diagnostic(
        next_state,
        penalty,
        posture_tracking,
        config,
        reason=("canonical_reset" if canonical_event is not None else "advanced"),
        eligible_delta=eligible_delta,
    )


def static_burden_level(
    penalty: float,
    config: StaticBurdenConfig = NORMAL_STATIC_BURDEN_CONFIG,
):
    if penalty + 1e-9 >= config.danger_penalty:
        return 2
    if penalty + 1e-9 >= config.caution_penalty:
        return 1
    return 0


def _diagnostic(state, penalty, posture_tracking, config, *, reason, eligible_delta):
    canonical_event = (
        None if posture_tracking is None else posture_tracking.get("canonical_event")
    )
    return {
        "v": 1,
        "policy": StaticPolicyMode.CUMULATIVE_V2.value,
        "candidate_time_policy": "continuous_valid_seated",
        "posture_episode_sec": round(state.posture_episode_sec, 6),
        "static_eligible_total_sec": round(state.static_eligible_total_sec, 6),
        "eligible_delta_sec": round(eligible_delta, 6),
        "mapped_curve_sec": round(
            config.episode_grace_sec + state.static_eligible_total_sec,
            6,
        ),
        "static_penalty": round(float(penalty), 6),
        "static_level": static_burden_level(penalty, config),
        "canonical_event_id": (
            None if canonical_event is None else canonical_event.get("id")
        ),
        "update_reason": reason,
        "episode_grace_sec": config.episode_grace_sec,
        "caution_penalty": config.caution_penalty,
        "danger_penalty": config.danger_penalty,
        "production_behavior_changed": True,
    }


def _non_negative_finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )
