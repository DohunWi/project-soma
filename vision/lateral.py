"""Pure geometry for observational Vision lateral-displacement metrics.

Coordinates follow the unmirrored image passed to MediaPipe: x grows toward
image-right and y grows downward.  These signs do not imply the subject's
anatomical left/right until a real webcam direction check has been completed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

try:
    from vision.geometry import FACE_L, FACE_R
    from vision.config import DEFAULT_LEAN_THRESHOLD_POLICY, LeanThresholdPolicy
except ImportError:  # ``python vision/run.py`` imports modules from vision/.
    from geometry import FACE_L, FACE_R
    from config import DEFAULT_LEAN_THRESHOLD_POLICY, LeanThresholdPolicy

EYE_OUTER_CORNERS = (33, 263)
MIN_NORMALIZED_FACE_WIDTH = 1e-6

LEAN_CENTER = "CENTER"
LEAN_LEFT = "LEFT"
LEAN_RIGHT = "RIGHT"
LEAN_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FaceLateralGeometry:
    """Resolution-independent geometry from one valid frontal face frame."""

    center_x_ratio: float
    width_ratio: float
    head_roll_deg: float


@dataclass(frozen=True)
class LeanClassification:
    """One observable result plus transition context for diagnostics/eval."""

    direction: str
    previous_direction: str
    transitioned: bool


class FaceLeanClassifier:
    """Stateful observational lean hysteresis with unavailable preservation."""

    def __init__(
        self,
        policy: LeanThresholdPolicy = DEFAULT_LEAN_THRESHOLD_POLICY,
    ) -> None:
        self.policy = policy
        self._state = LEAN_CENTER

    @property
    def state(self) -> str:
        """Last valid internal state; unavailable observations do not replace it."""
        return self._state

    def reset(self) -> None:
        self._state = LEAN_CENTER

    def update(self, offset: object) -> LeanClassification:
        previous = self._state
        try:
            value = float(offset)
        except (TypeError, ValueError):
            return LeanClassification(LEAN_UNKNOWN, previous, False)
        if not math.isfinite(value):
            return LeanClassification(LEAN_UNKNOWN, previous, False)

        if self._state == LEAN_CENTER:
            if value >= self.policy.left_entry:
                self._state = LEAN_LEFT
            elif value <= self.policy.right_entry:
                self._state = LEAN_RIGHT
        elif self._state == LEAN_LEFT:
            if value <= self.policy.left_release:
                self._state = LEAN_CENTER
        elif self._state == LEAN_RIGHT:
            if value >= self.policy.right_release:
                self._state = LEAN_CENTER

        return LeanClassification(
            self._state,
            previous,
            self._state != previous,
        )


def face_lateral_geometry(pts, frame_width: object) -> FaceLateralGeometry | None:
    """Return normalized center/width and image-plane roll, or ``None``.

    Roll uses the line between landmarks 33 and 263 after ordering them by
    image x.  Positive roll means that line descends toward image-right
    (clockwise in an image whose y coordinate grows downward).
    """
    try:
        image_width = float(frame_width)
        lx, ly = (float(value) for value in pts[FACE_L])
        rx, ry = (float(value) for value in pts[FACE_R])
        eye_a = tuple(float(value) for value in pts[EYE_OUTER_CORNERS[0]])
        eye_b = tuple(float(value) for value in pts[EYE_OUTER_CORNERS[1]])
    except (KeyError, TypeError, ValueError):
        return None

    values = (image_width, lx, ly, rx, ry, *eye_a, *eye_b)
    if not all(math.isfinite(value) for value in values) or image_width <= 0:
        return None

    face_width = math.hypot(rx - lx, ry - ly)
    width_ratio = face_width / image_width
    if width_ratio <= MIN_NORMALIZED_FACE_WIDTH:
        return None

    image_left_eye, image_right_eye = sorted((eye_a, eye_b), key=lambda point: point[0])
    eye_dx = image_right_eye[0] - image_left_eye[0]
    eye_dy = image_right_eye[1] - image_left_eye[1]
    if eye_dx <= 1e-6:
        return None

    return FaceLateralGeometry(
        center_x_ratio=((lx + rx) / 2.0) / image_width,
        width_ratio=width_ratio,
        head_roll_deg=math.degrees(math.atan2(eye_dy, eye_dx)),
    )


def lateral_offset(
    current: FaceLateralGeometry,
    *,
    neutral_center_x_ratio: object,
) -> float | None:
    """Normalize horizontal displacement by the current observed face width."""
    try:
        neutral = float(neutral_center_x_ratio)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(neutral) or current.width_ratio <= MIN_NORMALIZED_FACE_WIDTH:
        return None
    value = (current.center_x_ratio - neutral) / current.width_ratio
    return value if math.isfinite(value) else None


def angle_delta_deg(current: object, neutral: object) -> float | None:
    """Return the shortest signed image-plane angle difference."""
    try:
        current_value = float(current)
        neutral_value = float(neutral)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (current_value, neutral_value)):
        return None
    return (current_value - neutral_value + 180.0) % 360.0 - 180.0
