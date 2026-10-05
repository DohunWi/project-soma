"""Session-scoped Chair/Vision cache and deterministic Fusion input merge.

Receipt freshness belongs to the server transport layer, while state decisions
belong to ``fusion``.  This module keeps those responsibilities separate and
never calls Fusion, Socket.IO, or persistence itself.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from server.config import DEFAULT_SENSOR_MERGE_POLICY, SensorMergePolicy


VISION_MERGE_FIELDS = (
    "blink_rate",
    "face_distance_cm",
    "face_detected",
    "detect_rate",
    "blink_rate_baseline",
    "face_distance_baseline_cm",
    "face_lateral_offset",
    "head_roll_deg",
    "head_roll_delta_deg",
    "face_lateral_calibrated",
    "face_lean_direction",
)


class VisionAvailability(str, Enum):
    UNSEEN = "UNSEEN"
    FRESH = "FRESH"
    STALE = "STALE"
    RECOVERED = "RECOVERED"


@dataclass(frozen=True)
class CachedPayload:
    payload: dict
    sender_t: float
    received_at: float


class SessionSensorCache:
    """Keep only the latest samples belonging to one ACTIVE session."""

    def __init__(
        self,
        policy: SensorMergePolicy = DEFAULT_SENSOR_MERGE_POLICY,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy.vision_freshness_sec <= 0 or policy.vision_max_skew_sec <= 0:
            raise ValueError("sensor merge timing values must be positive")
        self.policy = policy
        self._monotonic = monotonic
        self.reset()

    @property
    def latest_chair(self) -> CachedPayload | None:
        return self._latest_chair

    @property
    def latest_vision(self) -> CachedPayload | None:
        return self._latest_vision

    @property
    def vision_availability(self) -> VisionAvailability:
        return self._vision_availability

    def reset(self) -> None:
        self._latest_chair: CachedPayload | None = None
        self._latest_vision: CachedPayload | None = None
        self._vision_availability = VisionAvailability.UNSEEN
        self._vision_was_stale = False

    def update_vision(self, payload: dict, *, received_at: float | None = None) -> bool:
        """Cache a Vision sample unless its sender timestamp is older."""
        receipt = self._receipt_time(received_at)
        sender_t = float(payload["t"])
        previous = self._latest_vision
        if previous is not None and sender_t < previous.sender_t:
            return False

        recovered = self._vision_was_stale
        if previous is not None:
            recovered = recovered or (
                receipt - previous.received_at >= self.policy.vision_freshness_sec
            )
        self._latest_vision = CachedPayload(
            payload=copy.deepcopy(payload),
            sender_t=sender_t,
            received_at=receipt,
        )
        self._vision_availability = (
            VisionAvailability.RECOVERED if recovered else VisionAvailability.FRESH
        )
        self._vision_was_stale = False
        return True

    def merged_chair_sample(
        self,
        payload: dict,
        *,
        received_at: float | None = None,
    ) -> dict | None:
        """Build a Fusion sample without advancing the Chair ordering checkpoint."""
        receipt = self._receipt_time(received_at)
        chair_t = float(payload["t"])
        if self._latest_chair is not None and chair_t <= self._latest_chair.sender_t:
            return None

        chair = payload["chair"]
        sample = {
            "pressure": list(chair["pressure"]),
            "ir": list(chair.get("ir", [])),
            "user_name": payload["user_name"],
        }
        vision = self._fresh_vision(chair_t, receipt)
        if vision is not None:
            for key in VISION_MERGE_FIELDS:
                if key in vision:
                    sample[key] = vision[key]
            # Backend-internal identity used only to avoid counting one cached
            # Vision frame more than once during calibration.
            sample["_vision_sender_t"] = self._latest_vision.sender_t
        return sample

    def mark_chair_processed(
        self,
        payload: dict,
        *,
        received_at: float | None = None,
    ) -> None:
        """Advance ordering only after Fusion and state validation succeed."""
        self._latest_chair = CachedPayload(
            payload=copy.deepcopy(payload),
            sender_t=float(payload["t"]),
            received_at=self._receipt_time(received_at),
        )

    def _fresh_vision(self, chair_t: float, receipt: float) -> dict | None:
        cached = self._latest_vision
        if cached is None:
            self._vision_availability = VisionAvailability.UNSEEN
            return None

        receipt_age = max(receipt - cached.received_at, 0.0)
        sender_skew = abs(chair_t - cached.sender_t)
        if (
            receipt_age >= self.policy.vision_freshness_sec
            or sender_skew >= self.policy.vision_max_skew_sec
        ):
            self._vision_availability = VisionAvailability.STALE
            self._vision_was_stale = True
            return None

        vision = cached.payload["vision"]
        self._vision_availability = VisionAvailability.FRESH
        return vision

    def _receipt_time(self, value: float | None) -> float:
        return self._monotonic() if value is None else float(value)
