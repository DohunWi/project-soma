"""Project logical Feedback onto the optional Nano's physical presentation.

Logical BREAK remains latched for the Front and policy lifecycle. The Nano
shows the initial BREAK intervention for a bounded minimum interval, then uses
the current Fusion state as its steady LED without creating another alert.
"""
from dataclasses import dataclass
from math import isfinite


MIN_BREAK_DISPLAY_SEC = 3.0

_LEVEL_BY_FUSION_STATE = {
    "NORMAL": ("NORMAL", None),
    "CAUTION": ("NOTICE", "FUSION_CAUTION"),
    "DANGER": ("WARNING", "FUSION_DANGER"),
}


@dataclass
class FeedbackDeviceProjector:
    """Own the small, session-reset physical BREAK display window."""

    minimum_break_display_sec: float = MIN_BREAK_DISPLAY_SEC
    _break_visible_until: float | None = None

    def __post_init__(self):
        duration = float(self.minimum_break_display_sec)
        if not isfinite(duration) or duration <= 0:
            raise ValueError(
                "minimum BREAK display duration must be positive and finite"
            )
        self.minimum_break_display_sec = duration

    def reset(self):
        self._break_visible_until = None

    def project(self, feedback_decision, fusion_decision, now):
        """Return a device decision without changing the logical decision."""
        projected = dict(feedback_decision)
        if projected.get("level") != "BREAK":
            self.reset()
            return projected

        monotonic_now = float(now)
        if not isfinite(monotonic_now):
            raise ValueError("device projection time must be finite")

        if projected.get("transition") is True:
            self._break_visible_until = (
                monotonic_now + self.minimum_break_display_sec
            )
            return projected

        if (
            self._break_visible_until is not None
            and monotonic_now < self._break_visible_until
        ):
            return projected

        self._break_visible_until = None
        fusion_state = fusion_decision.get("state")
        try:
            level, reason = _LEVEL_BY_FUSION_STATE[fusion_state]
        except KeyError as error:
            raise ValueError(
                f"unsupported seated Fusion state: {fusion_state!r}"
            ) from error

        device_decision = {
            "v": projected["v"],
            "t": projected["t"],
            "session_id": projected["session_id"],
            "level": level,
            # A steady LED projection must never create another vibration.
            "transition": False,
        }
        if reason is not None:
            device_decision["reason"] = reason
        return device_decision
