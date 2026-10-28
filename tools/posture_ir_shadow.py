"""Experimental pressure + Chair IR posture-change shadow observer.

This module is diagnostics-only.  It never calls Fusion, changes a production
score, or treats a confirmed event as ergonomic or clinical truth.  The
four-channel pressure observer remains the authority for lateral changes; this
observer adds provisional forward evidence and recovery diagnostics.
"""

from __future__ import annotations

import math
import statistics
import threading
from dataclasses import dataclass
from enum import Enum

from tools.chair_posture_diagnostic import pressure_distribution


PRESSURE_IR_DEFAULT_ANCHOR_SAMPLES = 5
PRESSURE_IR_DEFAULT_ANCHOR_IR_RADIUS_MM = 40.0
PRESSURE_IR_DEFAULT_ANCHOR_PRESSURE_RADIUS = 0.02
PRESSURE_IR_DEFAULT_MAX_GAP_SEC = 5.0
PRESSURE_IR_DEFAULT_FORWARD_IR_DELTA_MM = 80.0
PRESSURE_IR_DEFAULT_FORWARD_FRONT_BACK_DELTA = 0.0025
PRESSURE_IR_DEFAULT_LATERAL_GUARD_DELTA = 0.02
PRESSURE_IR_DEFAULT_CANDIDATE_SEC = 5.0
PRESSURE_IR_DEFAULT_RECOVERY_IR_DELTA_MM = 40.0
PRESSURE_IR_DEFAULT_RECOVERY_FRONT_BACK_DELTA = 0.0025
PRESSURE_IR_DEFAULT_RECOVERY_PRESSURE_DISTANCE = 0.02
PRESSURE_IR_DEFAULT_RECOVERY_SEC = 5.0


class PressureIrShadowMode(str, Enum):
    OBSERVE_ONLY = "observe-only"
    EXPERIMENTAL = "experimental"


class PressureIrShadowState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    ANCHOR_READY = "ANCHOR_READY"
    FORWARD_CANDIDATE = "FORWARD_CANDIDATE"
    FORWARD_CONFIRMED = "FORWARD_CONFIRMED"
    RECOVERY_CANDIDATE = "RECOVERY_CANDIDATE"
    INVALID = "INVALID"
    SUSPENDED = "SUSPENDED"


@dataclass(frozen=True)
class PressureIrShadowConfig:
    """Provisional diagnostic settings that are never production thresholds."""

    mode: PressureIrShadowMode = PressureIrShadowMode.OBSERVE_ONLY
    anchor_samples: int = PRESSURE_IR_DEFAULT_ANCHOR_SAMPLES
    anchor_ir_radius_mm: float = PRESSURE_IR_DEFAULT_ANCHOR_IR_RADIUS_MM
    anchor_pressure_radius: float = PRESSURE_IR_DEFAULT_ANCHOR_PRESSURE_RADIUS
    max_gap_sec: float = PRESSURE_IR_DEFAULT_MAX_GAP_SEC
    forward_ir_delta_mm: float = PRESSURE_IR_DEFAULT_FORWARD_IR_DELTA_MM
    forward_front_back_delta: float = (
        PRESSURE_IR_DEFAULT_FORWARD_FRONT_BACK_DELTA
    )
    lateral_guard_delta: float = PRESSURE_IR_DEFAULT_LATERAL_GUARD_DELTA
    candidate_sec: float = PRESSURE_IR_DEFAULT_CANDIDATE_SEC
    recovery_ir_delta_mm: float = PRESSURE_IR_DEFAULT_RECOVERY_IR_DELTA_MM
    recovery_front_back_delta: float = (
        PRESSURE_IR_DEFAULT_RECOVERY_FRONT_BACK_DELTA
    )
    recovery_pressure_distance: float = (
        PRESSURE_IR_DEFAULT_RECOVERY_PRESSURE_DISTANCE
    )
    recovery_sec: float = PRESSURE_IR_DEFAULT_RECOVERY_SEC

    def __post_init__(self):
        object.__setattr__(self, "mode", PressureIrShadowMode(self.mode))
        if self.anchor_samples < 2:
            raise ValueError("pressure_ir_anchor_samples must be at least 2")
        for name in (
            "anchor_ir_radius_mm",
            "anchor_pressure_radius",
            "max_gap_sec",
            "forward_ir_delta_mm",
            "forward_front_back_delta",
            "lateral_guard_delta",
            "candidate_sec",
            "recovery_ir_delta_mm",
            "recovery_front_back_delta",
            "recovery_pressure_distance",
            "recovery_sec",
        ):
            if not _positive_finite(getattr(self, name)):
                raise ValueError(f"pressure_ir_{name} must be positive and finite")


class PressureIrPostureShadowObserver:
    """Track a neutral anchor and provisional forward evidence independently."""

    def __init__(self, config=None):
        self.config = PressureIrShadowConfig() if config is None else config
        self._lock = threading.RLock()
        self._session_id = None
        self._clear_all()

    @property
    def session_id(self):
        return self._session_id

    def set_session(self, session_id):
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
        chair_ir_mm,
        *,
        seated,
        pressure_shadow=None,
        session_id=None,
    ):
        with self._lock:
            transition = None
            if session_id is not None and str(session_id) != self._session_id:
                self._session_id = str(session_id)
                self._clear_all()
                transition = self._event("anchor_reset_session", timestamp)

            now = _finite_timestamp(timestamp)
            sample = _sample_features(pressure, chair_ir_mm)
            if now is None:
                self._cancel_temporal()
                return self._result(
                    PressureIrShadowState.INVALID,
                    sample,
                    pressure_shadow,
                    classification="UNKNOWN",
                    invalid_reason="invalid_timestamp",
                    transition=transition,
                )

            gap = None if self._last_t is None else now - self._last_t
            if self._last_t is not None and gap <= 0:
                self._cancel_temporal()
                return self._result(
                    PressureIrShadowState.SUSPENDED,
                    sample,
                    pressure_shadow,
                    classification="UNKNOWN",
                    invalid_reason="non_increasing_timestamp",
                    transition=transition,
                )
            self._last_t = now

            if not seated:
                if not self._absent:
                    self._clear_anchor()
                    transition = self._event("anchor_reset_absence", now)
                self._absent = True
                return self._result(
                    PressureIrShadowState.SUSPENDED,
                    sample,
                    pressure_shadow,
                    classification="ABSENT",
                    invalid_reason="absent",
                    transition=transition,
                )

            if self._absent:
                self._clear_anchor()
                self._absent = False
                transition = self._event("anchor_rebuild_after_reseat", now)

            if gap is not None and gap > self.config.max_gap_sec:
                self._clear_anchor()
                transition = self._event(
                    "anchor_reset_data_gap",
                    now,
                    gap_sec=gap,
                )
                return self._result(
                    PressureIrShadowState.SUSPENDED,
                    sample,
                    pressure_shadow,
                    classification="UNKNOWN",
                    invalid_reason="data_gap",
                    transition=transition,
                )

            if not sample["valid"]:
                self._cancel_temporal()
                return self._result(
                    PressureIrShadowState.INVALID,
                    sample,
                    pressure_shadow,
                    classification="UNKNOWN",
                    invalid_reason=sample["reason"],
                    transition=transition,
                )

            if self._anchor is None:
                self._anchor_buffer.append(sample)
                if len(self._anchor_buffer) > self.config.anchor_samples:
                    self._anchor_buffer.pop(0)
                if len(self._anchor_buffer) == self.config.anchor_samples:
                    proposed = _median_sample(self._anchor_buffer)
                    if self._anchor_is_stable(self._anchor_buffer, proposed):
                        self._anchor = proposed
                        self._anchor_buffer.clear()
                        transition = self._event(
                            "neutral_anchor_created",
                            now,
                            ir_baseline_mm=proposed["ir_mm"],
                        )
                        return self._result(
                            PressureIrShadowState.ANCHOR_READY,
                            sample,
                            pressure_shadow,
                            classification="NEUTRAL",
                            transition=transition,
                        )
                return self._result(
                    PressureIrShadowState.UNINITIALIZED,
                    sample,
                    pressure_shadow,
                    classification="BASELINE_COLLECTING",
                    transition=transition,
                )

            evidence = self._evidence(sample, pressure_shadow)
            classification = evidence["classification"]
            pressure_event = (pressure_shadow or {}).get("shadow_event")
            if pressure_event is not None:
                transition = self._event(
                    "lateral_pressure_change_confirmed",
                    now,
                    direction=evidence["lateral_direction"],
                    pressure_event_timestamp=pressure_event.get("timestamp"),
                    ir_delta_mm=evidence["ir_delta_mm"],
                )

            if self.config.mode is PressureIrShadowMode.OBSERVE_ONLY:
                self._cancel_temporal()
                return self._result(
                    PressureIrShadowState.ANCHOR_READY,
                    sample,
                    pressure_shadow,
                    evidence=evidence,
                    classification=classification,
                    transition=transition,
                )

            if self._forward_confirmed:
                return self._observe_confirmed(
                    now,
                    gap,
                    sample,
                    pressure_shadow,
                    evidence,
                    transition,
                )

            if classification != "FORWARD_EVIDENCE":
                if self._candidate_started_at is not None:
                    transition = transition or self._event(
                        "forward_candidate_cancelled",
                        now,
                        reason=classification.lower(),
                    )
                self._cancel_candidate()
                return self._result(
                    PressureIrShadowState.ANCHOR_READY,
                    sample,
                    pressure_shadow,
                    evidence=evidence,
                    classification=classification,
                    transition=transition,
                )

            if self._candidate_started_at is None:
                self._candidate_started_at = now
                self._candidate_elapsed_sec = 0.0
                transition = transition or self._event(
                    "forward_candidate_started",
                    now,
                    ir_delta_mm=evidence["ir_delta_mm"],
                    front_back_delta=evidence["front_back_delta"],
                )
            else:
                self._candidate_elapsed_sec += 0.0 if gap is None else gap

            if self._candidate_elapsed_sec + 1e-9 < self.config.candidate_sec:
                return self._result(
                    PressureIrShadowState.FORWARD_CANDIDATE,
                    sample,
                    pressure_shadow,
                    evidence=evidence,
                    classification=classification,
                    transition=transition,
                )

            self._forward_confirmed = True
            self._confirmed_count += 1
            transition = self._event(
                "forward_change_confirmed",
                now,
                candidate_started_at=self._candidate_started_at,
                candidate_elapsed_sec=self._candidate_elapsed_sec,
                ir_delta_mm=evidence["ir_delta_mm"],
                front_back_delta=evidence["front_back_delta"],
            )
            self._cancel_candidate()
            return self._result(
                PressureIrShadowState.FORWARD_CONFIRMED,
                sample,
                pressure_shadow,
                evidence=evidence,
                classification=classification,
                transition=transition,
            )

    def _observe_confirmed(
        self,
        now,
        gap,
        sample,
        pressure_shadow,
        evidence,
        transition,
    ):
        recovery = self._is_recovery(evidence)
        if not recovery:
            if self._recovery_started_at is not None:
                transition = transition or self._event(
                    "neutral_recovery_cancelled",
                    now,
                )
            self._clear_recovery()
            return self._result(
                PressureIrShadowState.FORWARD_CONFIRMED,
                sample,
                pressure_shadow,
                evidence=evidence,
                classification=evidence["classification"],
                transition=transition,
            )

        self._recovery_samples.append(sample)
        if self._recovery_started_at is None:
            self._recovery_started_at = now
            self._recovery_elapsed_sec = 0.0
            transition = transition or self._event(
                "neutral_recovery_started",
                now,
            )
        else:
            self._recovery_elapsed_sec += 0.0 if gap is None else gap

        if self._recovery_elapsed_sec + 1e-9 < self.config.recovery_sec:
            return self._result(
                PressureIrShadowState.RECOVERY_CANDIDATE,
                sample,
                pressure_shadow,
                evidence=evidence,
                classification="NEUTRAL_RECOVERY",
                transition=transition,
            )

        old_ir = self._anchor["ir_mm"]
        self._anchor = _median_sample(self._recovery_samples)
        self._anchor_buffer.clear()
        self._forward_confirmed = False
        transition = self._event(
            "neutral_return_confirmed",
            now,
            recovery_started_at=self._recovery_started_at,
            recovery_elapsed_sec=self._recovery_elapsed_sec,
            old_ir_baseline_mm=old_ir,
            new_ir_baseline_mm=self._anchor["ir_mm"],
        )
        self._clear_recovery()
        evidence = self._evidence(sample, pressure_shadow)
        return self._result(
            PressureIrShadowState.ANCHOR_READY,
            sample,
            pressure_shadow,
            evidence=evidence,
            classification="NEUTRAL",
            transition=transition,
        )

    def _anchor_is_stable(self, samples, proposed):
        ir_radius = max(abs(row["ir_mm"] - proposed["ir_mm"]) for row in samples)
        pressure_radius = max(
            _distance(row["shares"], proposed["shares"]) for row in samples
        )
        return (
            ir_radius <= self.config.anchor_ir_radius_mm
            and pressure_radius <= self.config.anchor_pressure_radius
        )

    def _evidence(self, sample, pressure_shadow):
        ir_delta = sample["ir_mm"] - self._anchor["ir_mm"]
        front_back_delta = sample["front_back"] - self._anchor["front_back"]
        left_right_delta = sample["left_right"] - self._anchor["left_right"]
        neutral_pressure_distance = _distance(
            sample["shares"],
            self._anchor["shares"],
        )
        pressure_anchor_distance = (pressure_shadow or {}).get(
            "distance_from_anchor"
        )
        lateral_direction = "CENTER"
        if left_right_delta >= self.config.lateral_guard_delta:
            lateral_direction = "LEFT"
        elif left_right_delta <= -self.config.lateral_guard_delta:
            lateral_direction = "RIGHT"

        forward_axes = (
            ir_delta >= self.config.forward_ir_delta_mm
            and front_back_delta >= self.config.forward_front_back_delta
        )
        if forward_axes and lateral_direction != "CENTER":
            classification = "COMBINED_LATERAL_IR"
        elif forward_axes:
            classification = "FORWARD_EVIDENCE"
        elif lateral_direction != "CENTER":
            classification = f"LATERAL_{lateral_direction}"
        else:
            classification = "NEUTRAL"
        return {
            "classification": classification,
            "ir_delta_mm": ir_delta,
            "front_back_delta": front_back_delta,
            "left_right_delta": left_right_delta,
            "lateral_direction": lateral_direction,
            "neutral_pressure_distance": neutral_pressure_distance,
            "pressure_anchor_distance": pressure_anchor_distance,
        }

    def _is_recovery(self, evidence):
        pressure_distance = evidence["neutral_pressure_distance"]
        return (
            abs(evidence["ir_delta_mm"]) <= self.config.recovery_ir_delta_mm
            and abs(evidence["front_back_delta"])
            <= self.config.recovery_front_back_delta
            and pressure_distance <= self.config.recovery_pressure_distance
        )

    def _result(
        self,
        state,
        sample,
        pressure_shadow,
        *,
        classification,
        evidence=None,
        invalid_reason=None,
        transition=None,
    ):
        evidence = evidence or {}
        return {
            "mode": self.config.mode.value,
            "state": state.value,
            "session_id": self._session_id,
            "anchor_initialized": self._anchor is not None,
            "ir_baseline_mm": (
                None if self._anchor is None else self._anchor["ir_mm"]
            ),
            "front_back_baseline": (
                None if self._anchor is None else self._anchor["front_back"]
            ),
            "left_right_baseline": (
                None if self._anchor is None else self._anchor["left_right"]
            ),
            "classification": classification,
            "ir_delta_mm": evidence.get("ir_delta_mm"),
            "front_back_delta": evidence.get("front_back_delta"),
            "left_right_delta": evidence.get("left_right_delta"),
            "lateral_direction": evidence.get("lateral_direction"),
            "neutral_pressure_distance": evidence.get(
                "neutral_pressure_distance"
            ),
            "pressure_anchor_distance": evidence.get("pressure_anchor_distance"),
            "candidate_started_at": self._candidate_started_at,
            "candidate_elapsed_sec": self._candidate_elapsed_sec,
            "forward_confirmed": self._forward_confirmed,
            "confirmed_count": self._confirmed_count,
            "recovery_started_at": self._recovery_started_at,
            "recovery_elapsed_sec": self._recovery_elapsed_sec,
            "sample_valid": sample.get("valid", False),
            "invalid_reason": invalid_reason,
            "transition": transition,
            "provisional_thresholds": {
                "forward_ir_delta_mm": self.config.forward_ir_delta_mm,
                "forward_front_back_delta": self.config.forward_front_back_delta,
                "lateral_guard_delta": self.config.lateral_guard_delta,
                "candidate_sec": self.config.candidate_sec,
                "recovery_ir_delta_mm": self.config.recovery_ir_delta_mm,
                "recovery_front_back_delta": (
                    self.config.recovery_front_back_delta
                ),
                "recovery_pressure_distance": (
                    self.config.recovery_pressure_distance
                ),
                "recovery_sec": self.config.recovery_sec,
            },
            "production_behavior_changed": False,
        }

    def _event(self, event_type, timestamp, **details):
        return {"type": event_type, "timestamp": timestamp, **details}

    def _cancel_candidate(self):
        self._candidate_started_at = None
        self._candidate_elapsed_sec = 0.0

    def _clear_recovery(self):
        self._recovery_started_at = None
        self._recovery_elapsed_sec = 0.0
        self._recovery_samples = []

    def _cancel_temporal(self):
        self._cancel_candidate()
        self._clear_recovery()

    def _clear_anchor(self):
        self._anchor = None
        self._anchor_buffer = []
        self._forward_confirmed = False
        self._cancel_temporal()

    def _clear_all(self):
        self._anchor = None
        self._anchor_buffer = []
        self._last_t = None
        self._absent = False
        self._forward_confirmed = False
        self._confirmed_count = 0
        self._cancel_temporal()


def _sample_features(pressure, chair_ir_mm):
    if pressure is None:
        return {"valid": False, "reason": "invalid_pressure"}
    distribution = pressure_distribution(pressure)
    if not distribution["valid"]:
        return {"valid": False, "reason": distribution["reason"]}
    if not _positive_finite(chair_ir_mm) or chair_ir_mm >= 8191:
        return {"valid": False, "reason": "invalid_chair_ir"}
    shares = tuple(distribution["channel_share"][name] for name in ("fl", "fr", "bl", "br"))
    return {
        "valid": True,
        "reason": None,
        "ir_mm": float(chair_ir_mm),
        "shares": shares,
        "front_back": distribution["front_back_ratio"],
        "left_right": distribution["left_right_ratio"],
    }


def _median_sample(samples):
    return {
        "ir_mm": statistics.median(row["ir_mm"] for row in samples),
        "shares": tuple(
            statistics.median(row["shares"][index] for row in samples)
            for index in range(4)
        ),
        "front_back": statistics.median(row["front_back"] for row in samples),
        "left_right": statistics.median(row["left_right"] for row in samples),
    }


def _distance(first, second):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _finite_timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _positive_finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )
