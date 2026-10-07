"""Pure, session-scoped Feedback and Break Recommendation policy.

The policy consumes validated Fusion decisions. It has no socket, serial, DB,
clock, or hardware dependency; callers provide both the timestamp and session.
"""
from dataclasses import dataclass, replace
from typing import Optional
from uuid import UUID

from feedback.config import DEMO_FEEDBACK_CONFIG, FeedbackPolicyConfig


LEVEL_BY_STATE = {
    "NORMAL": "NORMAL",
    "CAUTION": "NOTICE",
    "DANGER": "WARNING",
    "ABSENT": "NORMAL",
}

BREAK_REASON_PRIORITY = (
    "SUSTAINED_DANGER",
    "LOW_SCORE",
    "CONTINUOUS_WORK",
)


@dataclass(frozen=True)
class FeedbackPolicyState:
    """Immutable state belonging to exactly one measurement session."""

    last_t: Optional[float] = None
    current_level: str = "NORMAL"
    work_period_sec: float = 0.0
    danger_hold_sec: float = 0.0
    low_score_hold_sec: float = 0.0
    absence_sec: float = 0.0
    break_active: bool = False
    break_reason: Optional[str] = None
    cooldown_remaining_sec: float = 0.0
    recommended_break_reached: bool = False


def step(
    previous: FeedbackPolicyState,
    fusion_decision: dict,
    now: float,
    session_id,
    *,
    config: FeedbackPolicyConfig = DEMO_FEEDBACK_CONFIG,
):
    """Advance the policy once and return ``(state, logical decision)``."""
    timestamp = float(now)
    session = str(UUID(str(session_id)))
    dt, last_t = _elapsed(previous.last_t, timestamp, config.max_gap_sec)
    fusion_state = fusion_decision.get("state")
    if fusion_state not in LEVEL_BY_STATE:
        raise ValueError(f"unsupported Fusion state: {fusion_state!r}")

    seated = fusion_state != "ABSENT"
    cooldown = max(previous.cooldown_remaining_sec - dt, 0.0)
    break_active = previous.break_active
    break_reason = previous.break_reason
    work_period = previous.work_period_sec
    recommended_reached = previous.recommended_break_reached

    if seated:
        absence = 0.0
        recommended_reached = False
        work_period += dt
        danger_hold = (
            previous.danger_hold_sec + dt if fusion_state == "DANGER" else 0.0
        )
        score = fusion_decision.get("score")
        low_score_hold = (
            previous.low_score_hold_sec + dt
            if isinstance(score, (int, float))
            and not isinstance(score, bool)
            and score <= config.low_score_threshold
            else 0.0
        )
    else:
        absence = previous.absence_sec + dt
        danger_hold = 0.0
        low_score_hold = 0.0
        recommended_reached = (
            recommended_reached or absence >= config.recommended_break_sec
        )

        effective_just_reached = (
            previous.absence_sec < config.effective_break_sec <= absence
        )
        if effective_just_reached:
            had_work = work_period > 0.0 or break_active
            work_period = 0.0
            break_active = False
            break_reason = None
            if had_work:
                cooldown = config.break_cooldown_sec

    if seated and not break_active and cooldown <= 0.0:
        triggered = {
            "SUSTAINED_DANGER": danger_hold >= config.danger_hold_sec,
            "LOW_SCORE": low_score_hold >= config.low_score_hold_sec,
            "CONTINUOUS_WORK": work_period >= config.continuous_work_sec,
        }
        break_reason = next(
            (reason for reason in BREAK_REASON_PRIORITY if triggered[reason]),
            None,
        )
        break_active = break_reason is not None

    level = "BREAK" if break_active else LEVEL_BY_STATE[fusion_state]
    transition = level != previous.current_level
    state = replace(
        previous,
        last_t=last_t,
        current_level=level,
        work_period_sec=work_period,
        danger_hold_sec=danger_hold,
        low_score_hold_sec=low_score_hold,
        absence_sec=absence,
        break_active=break_active,
        break_reason=break_reason,
        cooldown_remaining_sec=cooldown,
        recommended_break_reached=recommended_reached,
    )

    decision = {
        "v": 1,
        "t": round(timestamp, 3),
        "session_id": session,
        "level": level,
        "transition": transition,
    }
    reason = _decision_reason(fusion_state, level, break_reason)
    if reason is not None:
        decision["reason"] = reason
    if level == "BREAK":
        decision["recommended_break_sec"] = config.recommended_break_sec
    return state, decision


def _elapsed(last_t: Optional[float], now: float, max_gap_sec: float):
    if last_t is None:
        return 0.0, now
    if now <= last_t:
        return 0.0, last_t
    elapsed = now - last_t
    if elapsed > max_gap_sec:
        return 0.0, now
    return elapsed, now


def _decision_reason(fusion_state, level, break_reason):
    if level == "BREAK":
        return break_reason
    if fusion_state == "CAUTION":
        return "FUSION_CAUTION"
    if fusion_state == "DANGER":
        return "FUSION_DANGER"
    if fusion_state == "ABSENT":
        return "ABSENT"
    return None
