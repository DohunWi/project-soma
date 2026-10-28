"""Diagnostic-only posture-change tracking for R20.

The tracker mirrors the validated R19 pressure and pressure+IR Shadow rules,
but lives in :mod:`fusion` so runtime code never imports ``tools``.  It is
strictly observational: it does not mutate ``FusionState`` and its output is
not part of the public ``state`` contract.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import Enum


class PostureTrackingMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    CLOCK = "clock"


@dataclass(frozen=True)
class PostureTrackingConfig:
    """Provisional R19 settings, available only behind an opt-in flag."""

    mode: PostureTrackingMode = PostureTrackingMode.OFF
    anchor_samples: int = 5
    anchor_radius: float = 0.02
    max_gap_sec: float = 5.0
    pressure_candidate_distance: float = 0.02
    pressure_candidate_sec: float = 5.0
    pressure_candidate_radius: float = 0.02
    ir_anchor_radius_mm: float = 40.0
    forward_ir_delta_mm: float = 80.0
    forward_front_back_delta: float = 0.0025
    lateral_guard_delta: float = 0.02
    forward_candidate_sec: float = 5.0
    recovery_ir_delta_mm: float = 40.0
    recovery_front_back_delta: float = 0.0025
    recovery_pressure_distance: float = 0.02
    recovery_sec: float = 5.0

    def __post_init__(self):
        object.__setattr__(self, "mode", PostureTrackingMode(self.mode))
        if self.anchor_samples < 2:
            raise ValueError("anchor_samples must be at least 2")
        for name in (
            "anchor_radius",
            "max_gap_sec",
            "pressure_candidate_distance",
            "pressure_candidate_sec",
            "pressure_candidate_radius",
            "ir_anchor_radius_mm",
            "forward_ir_delta_mm",
            "forward_front_back_delta",
            "lateral_guard_delta",
            "forward_candidate_sec",
            "recovery_ir_delta_mm",
            "recovery_front_back_delta",
            "recovery_pressure_distance",
            "recovery_sec",
        ):
            value = getattr(self, name)
            if not _positive_finite(value):
                raise ValueError(f"{name} must be positive and finite")


class PostureTrackingCoordinator:
    """Merge R19 Shadow evidence into canonical diagnostic events and clocks."""

    def __init__(self, config=None):
        self.config = PostureTrackingConfig() if config is None else config
        self._pressure = _PressureDetector(self.config)
        self._pressure_ir = _PressureIrDetector(self.config)
        self.reset()

    def reset(self, session_id=None):
        self._session_id = None if session_id is None else str(session_id)
        self._pressure.reset()
        self._pressure_ir.reset()
        self._last_t = None
        self._last_seated = False
        self._candidate_active = False
        self._clock_conservative = 0.0
        self._clock_continuous = 0.0
        self._canonical_count = 0
        self._last_canonical = None
        self._last_result = None

    @property
    def last_result(self):
        return self._last_result

    def observe(
        self,
        timestamp,
        pressure,
        chair_ir_mm,
        *,
        seated,
        session_id=None,
        static_hold_sec=None,
        lifecycle_paused=False,
    ):
        if self.config.mode is PostureTrackingMode.OFF:
            self._last_result = None
            return None

        normalized_session = None if session_id is None else str(session_id)
        session_changed = normalized_session != self._session_id
        if session_changed:
            self.reset(normalized_session)

        now = _timestamp(timestamp)
        gap = None if now is None or self._last_t is None else now - self._last_t
        long_gap = gap is not None and gap > self.config.max_gap_sec
        non_increasing = gap is not None and gap <= 0
        previous_candidate = self._candidate_active

        distribution = _distribution(pressure)
        if lifecycle_paused:
            self._pressure.pause()
            self._pressure_ir.pause()
            self._candidate_active = False
            if now is not None and not non_increasing:
                self._last_t = now
            self._last_seated = bool(seated)
            clocks = None
            if self.config.mode is PostureTrackingMode.CLOCK:
                clocks = {
                    "freeze_no_backfill": round(self._clock_conservative, 6),
                    "continuous_valid_seated": round(self._clock_continuous, 6),
                }
            result = {
                "v": 1,
                "mode": self.config.mode.value,
                "session_id": self._session_id,
                "session_changed": session_changed,
                "sample_timestamp": now,
                "sample_valid_pressure": distribution["valid"],
                "sample_valid_ir": _valid_ir(chair_ir_mm),
                "seated": bool(seated),
                "data_gap": long_gap,
                "lifecycle_paused": True,
                "valid_interval_sec": 0.0,
                "candidate_active": False,
                "candidate_policy_finalized": False,
                "sustained_posture_sec": clocks,
                "pre_reset_sustained_posture_sec": clocks,
                "pressure_shadow": {
                    "state": "SUSPENDED",
                    "invalid_reason": "lifecycle_paused",
                    "event": None,
                },
                "pressure_ir_shadow": {
                    "state": "SUSPENDED",
                    "invalid_reason": "lifecycle_paused",
                    "event": None,
                },
                "canonical_event": None,
                "deduplicated_events": [],
                "canonical_event_count": self._canonical_count,
                "last_canonical_event": self._last_canonical,
                "legacy_static_hold_sec": static_hold_sec,
                "production_behavior_changed": False,
            }
            self._last_result = result
            return result

        pressure_result = self._pressure.observe(
            now,
            distribution,
            seated=bool(seated),
            gap=gap,
        )
        pressure_ir_result = self._pressure_ir.observe(
            now,
            distribution,
            chair_ir_mm,
            seated=bool(seated),
            gap=gap,
            pressure_result=pressure_result,
        )

        candidate_active = (
            pressure_result["state"] == "CANDIDATE"
            or pressure_ir_result["state"]
            in {"FORWARD_CANDIDATE", "RECOVERY_CANDIDATE"}
        )
        canonical_event, duplicates = self._canonical_event(
            now,
            pressure_result,
            pressure_ir_result,
        )

        valid_interval = (
            now is not None
            and gap is not None
            and 0 < gap <= self.config.max_gap_sec
            and bool(seated)
            and distribution["valid"]
            and self._last_seated
        )
        if not seated:
            self._clock_conservative = 0.0
            self._clock_continuous = 0.0
        elif self.config.mode is PostureTrackingMode.CLOCK and valid_interval:
            self._clock_continuous += gap
            if not previous_candidate:
                self._clock_conservative += gap

        pre_reset_clocks = None
        if self.config.mode is PostureTrackingMode.CLOCK:
            pre_reset_clocks = {
                "freeze_no_backfill": round(self._clock_conservative, 6),
                "continuous_valid_seated": round(self._clock_continuous, 6),
            }

        if canonical_event is not None:
            # All evidence for the same change is merged before this single reset.
            self._clock_conservative = 0.0
            self._clock_continuous = 0.0

        if now is not None and not non_increasing:
            self._last_t = now
        self._last_seated = bool(seated)
        self._candidate_active = candidate_active

        clocks = None
        if self.config.mode is PostureTrackingMode.CLOCK:
            clocks = {
                "freeze_no_backfill": round(self._clock_conservative, 6),
                "continuous_valid_seated": round(self._clock_continuous, 6),
            }
        result = {
            "v": 1,
            "mode": self.config.mode.value,
            "session_id": self._session_id,
            "session_changed": session_changed,
            "sample_timestamp": now,
            "sample_valid_pressure": distribution["valid"],
            "sample_valid_ir": pressure_ir_result["sample_valid_ir"],
            "seated": bool(seated),
            "data_gap": long_gap,
            "lifecycle_paused": False,
            "valid_interval_sec": (
                round(gap, 6) if valid_interval else 0.0
            ),
            "candidate_active": candidate_active,
            "candidate_policy_finalized": False,
            "sustained_posture_sec": clocks,
            "pre_reset_sustained_posture_sec": pre_reset_clocks,
            "pressure_shadow": pressure_result,
            "pressure_ir_shadow": pressure_ir_result,
            "canonical_event": canonical_event,
            "deduplicated_events": duplicates,
            "canonical_event_count": self._canonical_count,
            "last_canonical_event": self._last_canonical,
            "legacy_static_hold_sec": static_hold_sec,
            "production_behavior_changed": False,
        }
        self._last_result = result
        return result

    def _canonical_event(self, now, pressure_result, pressure_ir_result):
        if now is None:
            return None, []
        raw = []
        pressure_event = pressure_result.get("event")
        if pressure_event is not None:
            raw.append({
                "source": "pressure",
                "kind": "PRESSURE_CHANGE",
                "target": pressure_event["direction"],
                "started_at": pressure_event["candidate_started_at"],
                "confirmed_at": pressure_event["timestamp"],
            })
        ir_event = pressure_ir_result.get("event")
        if ir_event is not None:
            event_type = ir_event["type"]
            if event_type == "forward_change_confirmed":
                raw.append({
                    "source": "pressure_ir",
                    "kind": "FORWARD_CHANGE",
                    "target": "FORWARD",
                    "started_at": ir_event["candidate_started_at"],
                    "confirmed_at": ir_event["timestamp"],
                })
            elif event_type == "neutral_return_confirmed":
                raw.append({
                    "source": "pressure_ir",
                    "kind": "NEUTRAL_RETURN",
                    "target": "CENTER",
                    "started_at": ir_event["recovery_started_at"],
                    "confirmed_at": ir_event["timestamp"],
                })

        if not raw:
            return None, []
        # When both detectors confirm on the same Chair tick, the IR-specific
        # FORWARD/RETURN meaning is more informative than the generic pressure
        # change while still causing only one canonical reset.
        primary = next(
            (item for item in raw if item["kind"] != "PRESSURE_CHANGE"),
            raw[0],
        )
        sources = [primary["source"]]
        duplicates = []
        for item in raw:
            if item is primary:
                continue
            if _same_change(primary, item):
                sources.append(item["source"])
                duplicates.append(item)
            else:
                # Two genuinely distinct confirmations on one Chair tick are
                # represented by one canonical reset with both evidence sources.
                sources.append(item["source"])
                duplicates.append({**item, "merged_same_tick": True})

        if (
            self._last_canonical is not None
            and _same_change(self._last_canonical, primary)
        ):
            duplicate_of = self._last_canonical["id"]
            if (
                self._last_canonical["kind"] == "PRESSURE_CHANGE"
                and primary["kind"] != "PRESSURE_CHANGE"
            ):
                self._last_canonical = {
                    **self._last_canonical,
                    "kind": primary["kind"],
                    "target": primary["target"],
                    "started_at": min(
                        self._last_canonical["started_at"],
                        primary["started_at"],
                    ),
                    "confirmed_at": max(
                        self._last_canonical["confirmed_at"],
                        primary["confirmed_at"],
                    ),
                    "sources": sorted(set(
                        self._last_canonical["sources"] + [primary["source"]]
                    )),
                }
            return None, [
                {**primary, "duplicate_of": duplicate_of},
                *duplicates,
            ]

        self._canonical_count += 1
        event = {
            "id": self._canonical_count,
            "kind": primary["kind"],
            "target": primary["target"],
            "started_at": primary["started_at"],
            "confirmed_at": primary["confirmed_at"],
            "sources": sorted(set(sources)),
            "resets_clock_once": True,
        }
        self._last_canonical = event
        return event, duplicates


class _PressureDetector:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        self.anchor = None
        self.buffer = []
        self.candidate = None
        self.candidate_samples = []
        self.candidate_started = None
        self.candidate_elapsed = 0.0

    def pause(self):
        self._cancel()

    def observe(self, now, distribution, *, seated, gap):
        if now is None:
            self._cancel()
            return self._result("SUSPENDED", distribution, "invalid_timestamp")
        if not seated:
            self.reset()
            return self._result("SUSPENDED", distribution, "absent")
        if gap is not None and gap <= 0:
            self._cancel()
            return self._result("SUSPENDED", distribution, "timestamp_not_increasing")
        if gap is not None and gap > self.config.max_gap_sec:
            self._cancel()
            return self._result("SUSPENDED", distribution, "data_gap")
        if not distribution["valid"]:
            self._cancel()
            return self._result("INVALID", distribution, distribution["reason"])

        current = distribution["shares"]
        if self.anchor is None:
            self.buffer.append(current)
            if len(self.buffer) > self.config.anchor_samples:
                self.buffer.pop(0)
            if len(self.buffer) == self.config.anchor_samples:
                proposed = _median_vector(self.buffer)
                if max(_distance(row, proposed) for row in self.buffer) <= self.config.anchor_radius:
                    self.anchor = proposed
                    self.buffer.clear()
                    return self._result("ANCHOR_READY", distribution, None)
            return self._result("UNINITIALIZED", distribution, None)

        distance = _distance(current, self.anchor)
        if self.candidate is None:
            if distance >= self.config.pressure_candidate_distance:
                self._start(current, now)
                return self._result("CANDIDATE", distribution, None, distance)
            return self._result("ANCHOR_READY", distribution, None, distance)
        if distance < self.config.pressure_candidate_distance:
            self._cancel()
            return self._result("ANCHOR_READY", distribution, None, distance)
        if _distance(current, self.candidate) > self.config.pressure_candidate_radius:
            self._start(current, now)
            return self._result("CANDIDATE", distribution, None, distance)

        self.candidate_samples.append(current)
        self.candidate = _median_vector(self.candidate_samples)
        self.candidate_elapsed += 0.0 if gap is None else gap
        if self.candidate_elapsed + 1e-9 < self.config.pressure_candidate_sec:
            return self._result("CANDIDATE", distribution, None, distance)

        old_anchor = self.anchor
        new_anchor = self.candidate
        started = self.candidate_started
        elapsed = self.candidate_elapsed
        self.anchor = new_anchor
        self._cancel()
        old_lr = old_anchor[0] + old_anchor[2] - old_anchor[1] - old_anchor[3]
        new_lr = new_anchor[0] + new_anchor[2] - new_anchor[1] - new_anchor[3]
        delta = new_lr - old_lr
        if delta >= self.config.lateral_guard_delta:
            direction = "LEFT"
        elif delta <= -self.config.lateral_guard_delta:
            direction = "RIGHT"
        else:
            direction = "CENTER"
        event = {
            "type": "posture_change_confirmed",
            "timestamp": now,
            "candidate_started_at": started,
            "candidate_elapsed_sec": elapsed,
            "distance": _distance(old_anchor, new_anchor),
            "direction": direction,
        }
        return self._result("CONFIRMED", distribution, None, 0.0, event)

    def _start(self, current, now):
        self.candidate = current
        self.candidate_samples = [current]
        self.candidate_started = now
        self.candidate_elapsed = 0.0

    def _cancel(self):
        self.candidate = None
        self.candidate_samples = []
        self.candidate_started = None
        self.candidate_elapsed = 0.0

    def _result(self, state, distribution, reason, distance=None, event=None):
        return {
            "state": state,
            "anchor_initialized": self.anchor is not None,
            "distance_from_anchor": distance,
            "candidate_started_at": self.candidate_started,
            "candidate_elapsed_sec": self.candidate_elapsed,
            "invalid_reason": reason,
            "event": event,
            "valid_distribution": distribution["valid"],
        }


class _PressureIrDetector:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        self.anchor = None
        self.buffer = []
        self.forward_confirmed = False
        self.candidate_started = None
        self.candidate_elapsed = 0.0
        self.recovery_started = None
        self.recovery_elapsed = 0.0
        self.recovery_samples = []

    def pause(self):
        self._cancel_temporal()

    def observe(self, now, distribution, chair_ir_mm, *, seated, gap, pressure_result):
        valid_ir = _valid_ir(chair_ir_mm)
        if now is None:
            self._cancel_temporal()
            return self._result("INVALID", valid_ir, "invalid_timestamp")
        if not seated:
            self.reset()
            return self._result("SUSPENDED", valid_ir, "absent")
        if gap is not None and gap <= 0:
            self._cancel_temporal()
            return self._result("SUSPENDED", valid_ir, "timestamp_not_increasing")
        if gap is not None and gap > self.config.max_gap_sec:
            self.reset()
            return self._result("SUSPENDED", valid_ir, "data_gap")
        if not distribution["valid"]:
            self._cancel_temporal()
            return self._result("INVALID", valid_ir, distribution["reason"])
        if not valid_ir:
            self._cancel_temporal()
            return self._result("INVALID", False, "invalid_chair_ir")

        sample = {
            "ir": float(chair_ir_mm),
            "shares": distribution["shares"],
            "front_back": distribution["front_back"],
            "left_right": distribution["left_right"],
        }
        if self.anchor is None:
            self.buffer.append(sample)
            if len(self.buffer) > self.config.anchor_samples:
                self.buffer.pop(0)
            if len(self.buffer) == self.config.anchor_samples:
                proposed = _median_sample(self.buffer)
                ir_radius = max(abs(row["ir"] - proposed["ir"]) for row in self.buffer)
                pressure_radius = max(_distance(row["shares"], proposed["shares"]) for row in self.buffer)
                if ir_radius <= self.config.ir_anchor_radius_mm and pressure_radius <= self.config.anchor_radius:
                    self.anchor = proposed
                    self.buffer.clear()
                    return self._result("ANCHOR_READY", True, None, "NEUTRAL")
            return self._result("UNINITIALIZED", True, None, "BASELINE_COLLECTING")

        evidence = self._evidence(sample)
        classification = evidence["classification"]
        if self.forward_confirmed:
            if self._recovery(evidence):
                self.recovery_samples.append(sample)
                if self.recovery_started is None:
                    self.recovery_started = now
                    self.recovery_elapsed = 0.0
                else:
                    self.recovery_elapsed += 0.0 if gap is None else gap
                if self.recovery_elapsed + 1e-9 >= self.config.recovery_sec:
                    started = self.recovery_started
                    elapsed = self.recovery_elapsed
                    self.anchor = _median_sample(self.recovery_samples)
                    self.forward_confirmed = False
                    self._clear_recovery()
                    event = {
                        "type": "neutral_return_confirmed",
                        "timestamp": now,
                        "recovery_started_at": started,
                        "recovery_elapsed_sec": elapsed,
                    }
                    return self._result("ANCHOR_READY", True, None, "NEUTRAL", evidence, event)
                return self._result("RECOVERY_CANDIDATE", True, None, "NEUTRAL_RECOVERY", evidence)
            self._clear_recovery()
            return self._result("FORWARD_CONFIRMED", True, None, classification, evidence)

        if classification != "FORWARD_EVIDENCE":
            self._cancel_candidate()
            return self._result("ANCHOR_READY", True, None, classification, evidence)
        if self.candidate_started is None:
            self.candidate_started = now
            self.candidate_elapsed = 0.0
        else:
            self.candidate_elapsed += 0.0 if gap is None else gap
        if self.candidate_elapsed + 1e-9 < self.config.forward_candidate_sec:
            return self._result("FORWARD_CANDIDATE", True, None, classification, evidence)

        started = self.candidate_started
        elapsed = self.candidate_elapsed
        self.forward_confirmed = True
        self._cancel_candidate()
        event = {
            "type": "forward_change_confirmed",
            "timestamp": now,
            "candidate_started_at": started,
            "candidate_elapsed_sec": elapsed,
        }
        return self._result("FORWARD_CONFIRMED", True, None, classification, evidence, event)

    def _evidence(self, sample):
        ir_delta = sample["ir"] - self.anchor["ir"]
        front_back_delta = sample["front_back"] - self.anchor["front_back"]
        left_right_delta = sample["left_right"] - self.anchor["left_right"]
        distance = _distance(sample["shares"], self.anchor["shares"])
        lateral = "CENTER"
        if left_right_delta >= self.config.lateral_guard_delta:
            lateral = "LEFT"
        elif left_right_delta <= -self.config.lateral_guard_delta:
            lateral = "RIGHT"
        forward = (
            ir_delta >= self.config.forward_ir_delta_mm
            and front_back_delta >= self.config.forward_front_back_delta
        )
        if forward and lateral != "CENTER":
            classification = "COMBINED_LATERAL_IR"
        elif forward:
            classification = "FORWARD_EVIDENCE"
        elif lateral != "CENTER":
            classification = f"LATERAL_{lateral}"
        else:
            classification = "NEUTRAL"
        return {
            "classification": classification,
            "ir_delta_mm": ir_delta,
            "front_back_delta": front_back_delta,
            "left_right_delta": left_right_delta,
            "neutral_pressure_distance": distance,
        }

    def _recovery(self, evidence):
        return (
            abs(evidence["ir_delta_mm"]) <= self.config.recovery_ir_delta_mm
            and abs(evidence["front_back_delta"]) <= self.config.recovery_front_back_delta
            and evidence["neutral_pressure_distance"] <= self.config.recovery_pressure_distance
        )

    def _cancel_candidate(self):
        self.candidate_started = None
        self.candidate_elapsed = 0.0

    def _clear_recovery(self):
        self.recovery_started = None
        self.recovery_elapsed = 0.0
        self.recovery_samples = []

    def _cancel_temporal(self):
        self._cancel_candidate()
        self._clear_recovery()

    def _result(self, state, valid_ir, reason, classification="UNKNOWN", evidence=None, event=None):
        evidence = evidence or {}
        return {
            "state": state,
            "anchor_initialized": self.anchor is not None,
            "classification": classification,
            "sample_valid_ir": valid_ir,
            "ir_baseline_mm": None if self.anchor is None else self.anchor["ir"],
            "ir_delta_mm": evidence.get("ir_delta_mm"),
            "front_back_delta": evidence.get("front_back_delta"),
            "left_right_delta": evidence.get("left_right_delta"),
            "candidate_started_at": self.candidate_started,
            "candidate_elapsed_sec": self.candidate_elapsed,
            "recovery_started_at": self.recovery_started,
            "recovery_elapsed_sec": self.recovery_elapsed,
            "forward_confirmed": self.forward_confirmed,
            "invalid_reason": reason,
            "event": event,
        }


def _distribution(pressure):
    if (
        not isinstance(pressure, (list, tuple))
        or len(pressure) != 4
        or any(not _finite(value) for value in pressure)
    ):
        return {"valid": False, "reason": "pressure_must_have_four_finite_numbers"}
    values = tuple(float(value) for value in pressure)
    total = sum(values)
    if total <= 0:
        return {"valid": False, "reason": "pressure_sum_must_be_positive"}
    shares = tuple(value / total for value in values)
    return {
        "valid": True,
        "reason": None,
        "shares": shares,
        "front_back": shares[0] + shares[1] - shares[2] - shares[3],
        "left_right": shares[0] + shares[2] - shares[1] - shares[3],
    }


def _same_change(first, second):
    first_start = first.get("started_at")
    second_start = second.get("started_at")
    if not _finite(first_start) or not _finite(second_start):
        return False
    first_end = first.get("confirmed_at")
    second_end = second.get("confirmed_at")
    overlaps = max(first_start, second_start) <= min(first_end, second_end)
    if not overlaps:
        return False
    if first.get("target") == second.get("target"):
        return True
    return "PRESSURE_CHANGE" in {first.get("kind"), second.get("kind")}


def _median_vector(rows):
    return tuple(statistics.median(row[index] for row in rows) for index in range(4))


def _median_sample(rows):
    return {
        "ir": statistics.median(row["ir"] for row in rows),
        "shares": _median_vector([row["shares"] for row in rows]),
        "front_back": statistics.median(row["front_back"] for row in rows),
        "left_right": statistics.median(row["left_right"] for row in rows),
    }


def _distance(first, second):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _timestamp(value):
    return float(value) if _finite(value) else None


def _valid_ir(value):
    return _positive_finite(value) and float(value) < 8191


def _positive_finite(value):
    return _finite(value) and float(value) > 0


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )
