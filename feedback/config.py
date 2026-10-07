"""Immutable engineering profiles for the pure Feedback Policy."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedbackPolicyConfig:
    """Timing and score parameters; these are not medical thresholds."""

    low_score_threshold: int
    low_score_hold_sec: float
    danger_hold_sec: float
    continuous_work_sec: float
    brief_absence_sec: float
    effective_break_sec: float
    recommended_break_sec: int
    break_cooldown_sec: float
    max_gap_sec: float

    def __post_init__(self):
        if not 0 <= self.low_score_threshold <= 100:
            raise ValueError("low score threshold must be between 0 and 100")
        positive = (
            self.low_score_hold_sec,
            self.danger_hold_sec,
            self.continuous_work_sec,
            self.brief_absence_sec,
            self.effective_break_sec,
            self.recommended_break_sec,
            self.break_cooldown_sec,
            self.max_gap_sec,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("feedback timing values must be positive")
        if self.brief_absence_sec >= self.effective_break_sec:
            raise ValueError("brief absence must end before an effective break")
        if self.effective_break_sec > self.recommended_break_sec:
            raise ValueError("recommended break cannot be shorter than effective break")


# Demo values compress only the time axis for the graduation-project demo.
DEMO_FEEDBACK_CONFIG = FeedbackPolicyConfig(
    low_score_threshold=60,
    low_score_hold_sec=10.0,
    danger_hold_sec=10.0,
    continuous_work_sec=60.0,
    brief_absence_sec=5.0,
    effective_break_sec=10.0,
    recommended_break_sec=20,
    break_cooldown_sec=30.0,
    max_gap_sec=5.0,
)

NORMAL_FEEDBACK_CONFIG = FeedbackPolicyConfig(
    low_score_threshold=60,
    low_score_hold_sec=120.0,
    danger_hold_sec=60.0,
    continuous_work_sec=3000.0,
    brief_absence_sec=60.0,
    effective_break_sec=180.0,
    recommended_break_sec=300,
    break_cooldown_sec=900.0,
    max_gap_sec=5.0,
)

FEEDBACK_CONFIGS = {
    "demo": DEMO_FEEDBACK_CONFIG,
    "normal": NORMAL_FEEDBACK_CONFIG,
}


def feedback_config_for_mode(mode: str) -> FeedbackPolicyConfig:
    """Select by the same profile name as SOMA_MODE without reading the environment."""
    try:
        return FEEDBACK_CONFIGS[mode]
    except KeyError as error:
        allowed = ", ".join(FEEDBACK_CONFIGS)
        raise ValueError(
            f"Invalid feedback mode={mode!r}. Expected one of: {allowed}."
        ) from error
