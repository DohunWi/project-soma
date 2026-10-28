"""Independent posture-change shadow observer.

This module never calls Fusion and never changes a production decision.  A
``CONFIRMED`` result means only that normalized Chair pressure distribution met
explicit experimental settings; it does not mean ergonomic benefit or fatigue
recovery.

State lifecycle:

``UNINITIALIZED`` -> ``ANCHOR_READY`` after a stable initial window.
``ANCHOR_READY`` -> ``CANDIDATE`` only in experimental mode.
``CANDIDATE`` -> ``CONFIRMED`` after an observed stable duration, or back to
``ANCHOR_READY`` when the distribution returns/reseeds. ``INVALID`` is emitted
for unusable pressure. ``SUSPENDED`` is emitted for absence, time gaps, or
non-increasing timestamps. Session changes and long absence clear all anchors.
"""

from __future__ import annotations

import math
import statistics
import threading
from dataclasses import dataclass
from enum import Enum

from tools.chair_activity_diagnostic import pressure_values
from tools.chair_posture_diagnostic import pressure_distribution


SHADOW_DEFAULT_ANCHOR_SAMPLES = 5
SHADOW_DEFAULT_ANCHOR_STABILITY_RADIUS = 0.02
SHADOW_DEFAULT_MAX_GAP_SEC = 5.0
SHADOW_DEFAULT_LONG_ABSENCE_SEC = 5.0


class ShadowMode(str, Enum):
    OBSERVE_ONLY = "observe-only"
    EXPERIMENTAL = "experimental"


class ShadowState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    ANCHOR_READY = "ANCHOR_READY"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALID = "INVALID"
    SUSPENDED = "SUSPENDED"


@dataclass(frozen=True)
class ShadowConfig:
    """Unvalidated diagnostic settings; none are production thresholds."""

    shadow_mode: ShadowMode = ShadowMode.OBSERVE_ONLY
    shadow_anchor_samples: int = SHADOW_DEFAULT_ANCHOR_SAMPLES
    shadow_anchor_stability_radius: float = SHADOW_DEFAULT_ANCHOR_STABILITY_RADIUS
    shadow_max_gap_sec: float = SHADOW_DEFAULT_MAX_GAP_SEC
    shadow_long_absence_sec: float = SHADOW_DEFAULT_LONG_ABSENCE_SEC
    experimental_candidate_distance: float | None = None
    experimental_candidate_sec: float | None = None
    experimental_candidate_radius: float | None = None

    def __post_init__(self):
        mode = ShadowMode(self.shadow_mode)
        object.__setattr__(self, "shadow_mode", mode)
        if self.shadow_anchor_samples < 2:
            raise ValueError("shadow_anchor_samples must be at least 2")
        for name in (
            "shadow_anchor_stability_radius",
            "shadow_max_gap_sec",
            "shadow_long_absence_sec",
        ):
            value = getattr(self, name)
            if not _positive_finite(value):
                raise ValueError(f"{name} must be positive and finite")
        experimental = (
            self.experimental_candidate_distance,
            self.experimental_candidate_sec,
            self.experimental_candidate_radius,
        )
        if mode is ShadowMode.EXPERIMENTAL:
            if any(not _positive_finite(value) for value in experimental):
                raise ValueError(
                    "experimental mode requires positive candidate distance, "
                    "duration, and radius"
                )
        elif any(value is not None for value in experimental):
            raise ValueError(
                "experimental detection settings require shadow_mode=experimental"
            )


class PostureShadowObserver:
    """Session-scoped normalized pressure-distribution observer."""

    def __init__(self, config=None):
        self.config = ShadowConfig() if config is None else config
        self._lock = threading.RLock()
        self._session_id = None
        self._clear_all()

    @property
    def session_id(self):
        return self._session_id

    def set_session(self, session_id):
        """Clear all state whenever the measurement session identity changes."""
        normalized = None if session_id is None else str(session_id)
        with self._lock:
            if normalized == self._session_id:
                return False
            self._session_id = normalized
            self._clear_all()
            return True

    def observe(
        self,
        timestamp,
        pressure,
        *,
        seated,
        static_hold_sec=None,
        session_id=None,
    ):
        with self._lock:
            anchor_reason = None
            if session_id is not None and str(session_id) != self._session_id:
                self._session_id = str(session_id)
                self._clear_all()
                anchor_reason = "session_changed"

            if not _finite_number(timestamp):
                return self._result(
                    ShadowState.SUSPENDED,
                    None,
                    static_hold_sec,
                    invalid_reason="invalid_timestamp",
                    anchor_update_reason=anchor_reason,
                )
            now = float(timestamp)
            if self._last_t is not None and now <= self._last_t:
                return self._result(
                    ShadowState.SUSPENDED,
                    None,
                    static_hold_sec,
                    invalid_reason="timestamp_not_increasing",
                    anchor_update_reason=anchor_reason,
                )
            gap = None if self._last_t is None else now - self._last_t

            if not seated:
                if self._absence_started_at is None:
                    self._absence_started_at = (
                        self._last_seated_t
                        if self._last_seated_t is not None
                        else now
                    )
                absence_sec = max(now - self._absence_started_at, 0.0)
                self._cancel_candidate()
                if absence_sec >= self.config.shadow_long_absence_sec:
                    self._clear_anchor()
                    anchor_reason = "long_absence_reset"
                self._last_t = now
                return self._result(
                    ShadowState.SUSPENDED,
                    None,
                    static_hold_sec,
                    invalid_reason="absent",
                    anchor_update_reason=anchor_reason,
                )

            if self._absence_started_at is not None:
                absence_sec = max(now - self._absence_started_at, 0.0)
                self._absence_started_at = None
                self._cancel_candidate()
                if absence_sec >= self.config.shadow_long_absence_sec:
                    self._clear_anchor()
                    anchor_reason = "long_absence_reset"

            if gap is not None and gap > self.config.shadow_max_gap_sec:
                self._cancel_candidate()
                self._last_t = now
                self._last_seated_t = now
                return self._result(
                    ShadowState.SUSPENDED,
                    None,
                    static_hold_sec,
                    invalid_reason="data_gap",
                    anchor_update_reason=anchor_reason,
                )

            validated = pressure_values({"pressure": pressure})
            if validated is None:
                self._cancel_candidate()
                self._last_t = now
                self._last_seated_t = now
                return self._result(
                    ShadowState.INVALID,
                    None,
                    static_hold_sec,
                    invalid_reason="pressure_must_have_four_finite_numbers",
                    anchor_update_reason=anchor_reason,
                )
            distribution = pressure_distribution(validated)
            if not distribution["valid"]:
                self._cancel_candidate()
                self._last_t = now
                self._last_seated_t = now
                return self._result(
                    ShadowState.INVALID,
                    None,
                    static_hold_sec,
                    invalid_reason=distribution["reason"],
                    anchor_update_reason=anchor_reason,
                )

            current = tuple(
                distribution["channel_share"][name]
                for name in ("fl", "fr", "bl", "br")
            )
            elapsed = 0.0 if gap is None else gap
            self._last_t = now
            self._last_seated_t = now

            if self._anchor is None:
                self._anchor_buffer.append(current)
                maximum = self.config.shadow_anchor_samples
                if len(self._anchor_buffer) > maximum:
                    self._anchor_buffer.pop(0)
                if len(self._anchor_buffer) == maximum:
                    proposed = _median_vector(self._anchor_buffer)
                    radius = max(
                        _distance(vector, proposed)
                        for vector in self._anchor_buffer
                    )
                    if radius <= self.config.shadow_anchor_stability_radius:
                        self._anchor = proposed
                        self._anchor_buffer.clear()
                        anchor_reason = "initial_stable_window"
                        return self._result(
                            ShadowState.ANCHOR_READY,
                            current,
                            static_hold_sec,
                            anchor_update_reason=anchor_reason,
                        )
                return self._result(
                    ShadowState.UNINITIALIZED,
                    current,
                    static_hold_sec,
                    anchor_update_reason=anchor_reason,
                )

            distance = _distance(current, self._anchor)
            if self.config.shadow_mode is ShadowMode.OBSERVE_ONLY:
                self._cancel_candidate()
                return self._result(
                    ShadowState.ANCHOR_READY,
                    current,
                    static_hold_sec,
                    distance=distance,
                    anchor_update_reason=anchor_reason,
                )

            threshold = self.config.experimental_candidate_distance
            radius = self.config.experimental_candidate_radius
            required_sec = self.config.experimental_candidate_sec
            if self._candidate_center is None:
                if distance >= threshold:
                    self._start_candidate(current, now, distance)
                    return self._result(
                        ShadowState.CANDIDATE,
                        current,
                        static_hold_sec,
                        distance=distance,
                        anchor_update_reason=anchor_reason,
                    )
                return self._result(
                    ShadowState.ANCHOR_READY,
                    current,
                    static_hold_sec,
                    distance=distance,
                    anchor_update_reason=anchor_reason,
                )

            if distance < threshold:
                self._cancel_candidate()
                return self._result(
                    ShadowState.ANCHOR_READY,
                    current,
                    static_hold_sec,
                    distance=distance,
                    anchor_update_reason=anchor_reason,
                )

            if _distance(current, self._candidate_center) > radius:
                self._start_candidate(current, now, distance)
                return self._result(
                    ShadowState.CANDIDATE,
                    current,
                    static_hold_sec,
                    distance=distance,
                    anchor_update_reason=anchor_reason,
                )

            self._candidate_samples.append(current)
            self._candidate_center = _median_vector(self._candidate_samples)
            self._candidate_elapsed_sec += elapsed
            if self._candidate_elapsed_sec + 1e-9 < required_sec:
                return self._result(
                    ShadowState.CANDIDATE,
                    current,
                    static_hold_sec,
                    distance=distance,
                    anchor_update_reason=anchor_reason,
                )

            old_anchor = self._anchor
            new_anchor = self._candidate_center
            confirmed_distance = _distance(old_anchor, new_anchor)
            started_at = self._candidate_started_at
            elapsed_sec = self._candidate_elapsed_sec
            self._anchor = new_anchor
            self._confirmed_change_count += 1
            self._last_confirmed_change_at = now
            self._last_confirmed_distance = confirmed_distance
            self._cancel_candidate()
            anchor_reason = "confirmed_change"
            event = {
                "type": "posture_change_confirmed",
                "timestamp": now,
                "candidate_started_at": started_at,
                "candidate_elapsed_sec": elapsed_sec,
                "distance": confirmed_distance,
                "old_anchor_channel_share": _share_dict(old_anchor),
                "new_anchor_channel_share": _share_dict(new_anchor),
                "clinical_or_recovery_meaning": False,
            }
            return self._result(
                ShadowState.CONFIRMED,
                current,
                static_hold_sec,
                # The result exposes the new anchor, so keep its distance field
                # consistent with that anchor.  The old-to-new distance remains
                # available as last_confirmed_distance and in shadow_event.
                distance=_distance(current, new_anchor),
                anchor_update_reason=anchor_reason,
                shadow_event=event,
            )

    def _start_candidate(self, current, now, distance):
        self._candidate_center = current
        self._candidate_samples = [current]
        self._candidate_started_at = now
        self._candidate_elapsed_sec = 0.0
        self._candidate_distance = distance

    def _cancel_candidate(self):
        self._candidate_center = None
        self._candidate_samples = []
        self._candidate_started_at = None
        self._candidate_elapsed_sec = 0.0
        self._candidate_distance = None

    def _clear_anchor(self):
        self._anchor = None
        self._anchor_buffer = []
        self._cancel_candidate()

    def _clear_all(self):
        self._anchor = None
        self._anchor_buffer = []
        self._cancel_candidate()
        self._last_t = None
        self._last_seated_t = None
        self._absence_started_at = None
        self._confirmed_change_count = 0
        self._last_confirmed_change_at = None
        self._last_confirmed_distance = None

    def _result(
        self,
        state,
        current,
        static_hold_sec,
        *,
        distance=None,
        anchor_update_reason=None,
        invalid_reason=None,
        shadow_event=None,
    ):
        candidate_distance = (
            None
            if self._anchor is None or self._candidate_center is None
            else _distance(self._anchor, self._candidate_center)
        )
        return {
            "shadow_mode": self.config.shadow_mode.value,
            "shadow_state": state.value,
            "session_id": self._session_id,
            "anchor_initialized": self._anchor is not None,
            "anchor_channel_share": _share_dict(self._anchor),
            "current_channel_share": _share_dict(current),
            "distance_from_anchor": distance,
            "candidate_started_at": self._candidate_started_at,
            "candidate_elapsed_sec": self._candidate_elapsed_sec,
            "candidate_distance": candidate_distance,
            "confirmed_change_count": self._confirmed_change_count,
            "last_confirmed_change_at": self._last_confirmed_change_at,
            "last_confirmed_distance": self._last_confirmed_distance,
            "anchor_update_reason": anchor_update_reason,
            "invalid_reason": invalid_reason,
            "static_hold_sec": static_hold_sec,
            "shadow_event": shadow_event,
        }


def _positive_finite(value):
    return _finite_number(value) and float(value) > 0


def _finite_number(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _median_vector(vectors):
    return tuple(
        statistics.median(vector[index] for vector in vectors)
        for index in range(4)
    )


def _distance(first, second):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _share_dict(vector):
    if vector is None:
        return None
    return dict(zip(("fl", "fr", "bl", "br"), vector))
