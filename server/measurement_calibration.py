"""Session-scoped measurement calibration lifecycle.

The component is intentionally independent of Flask and Socket.IO.  It owns
phase and baseline state; the application owns side effects when READY is
accepted.
"""
from dataclasses import dataclass
from enum import Enum
from math import isfinite
import threading

from fusion.calibration import CalibrationAccumulator
from fusion.config import DEMO_DISTANCE_EVIDENCE_TIMING
from fusion.distance_temporal import DistanceTemporalState, step as step_distance_temporal
from fusion.distance_evidence import (
    DEFAULT_DISTANCE_EVIDENCE_CONFIG,
    classify_distance_evidence,
)


class MeasurementPhase(str, Enum):
    OFF = "OFF"
    CALIBRATING = "CALIBRATING"
    READY = "READY"
    MEASURING = "MEASURING"


@dataclass(frozen=True)
class CalibrationEvaluation:
    became_ready: bool = False
    became_failed: bool = False


class MeasurementCalibration:
    """Own calibration state for the currently ACTIVE measurement session."""

    def __init__(
        self,
        config,
        *,
        occupancy_min,
        distance_evidence_config=DEFAULT_DISTANCE_EVIDENCE_CONFIG,
        distance_evidence_timing=DEMO_DISTANCE_EVIDENCE_TIMING,
    ):
        self.config = config
        self.occupancy_min = occupancy_min
        self.distance_evidence_config = distance_evidence_config
        self.distance_evidence_timing = distance_evidence_timing
        self._lock = threading.RLock()
        self._generation = 0
        self._clear()

    @property
    def phase(self):
        with self._lock:
            return self._phase

    @property
    def session_id(self):
        with self._lock:
            return self._session_id

    @property
    def baselines(self):
        with self._lock:
            return self._baselines

    @property
    def failed(self):
        with self._lock:
            return self._failed

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def latest_relative_evidence(self):
        with self._lock:
            return dict(self._latest_relative_evidence)

    @property
    def latest_distance_evidence(self):
        with self._lock:
            return self._latest_distance_evidence

    @property
    def latest_distance_temporal(self):
        with self._lock:
            return self._latest_distance_temporal

    def start(self, session_id, now):
        with self._lock:
            self._generation += 1
            self._session_id = str(session_id)
            self._last_session_id = None
            self._phase = MeasurementPhase.CALIBRATING
            self._started_at = float(now)
            self._accumulator = CalibrationAccumulator()
            self._baselines = None
            self._failed = False
            self._latest_relative_evidence = {}
            self._latest_distance_evidence = None
            self._reset_distance_temporal()
            return self._generation

    def stop(self):
        with self._lock:
            stopped_session_id = self._session_id
            self._generation += 1
            self._clear()
            # Retain only the identifier long enough for the OFF event to be
            # correlated.  Accumulator, baselines, and evidence are cleared.
            self._last_session_id = stopped_session_id
            return stopped_session_id

    def observe(self, sample, now):
        """Apply the current sample first, then evaluate its boundary."""
        with self._lock:
            if self._phase is not MeasurementPhase.CALIBRATING or self._failed:
                return CalibrationEvaluation()
            self._accumulator.observe(
                sample,
                occupancy_min=self.occupancy_min,
                vision_sample_id=sample.get("_vision_sender_t"),
            )
            return self._evaluate(float(now))

    def evaluate(self, now):
        """Evaluate duration/readiness or timeout without adding a sample."""
        with self._lock:
            if self._phase is not MeasurementPhase.CALIBRATING or self._failed:
                return CalibrationEvaluation()
            return self._evaluate(float(now))

    def accept_ready(self):
        with self._lock:
            if self._phase is not MeasurementPhase.READY:
                return False
            self._reset_distance_temporal()
            self._phase = MeasurementPhase.MEASURING
            return True

    def force_measuring_for_test(self, session_id):
        """Test-only compatibility hook for pre-calibration integration tests."""
        with self._lock:
            self._generation += 1
            self._session_id = str(session_id)
            self._last_session_id = None
            self._phase = MeasurementPhase.MEASURING
            self._started_at = 0.0
            self._accumulator = CalibrationAccumulator()
            self._baselines = None
            self._failed = False
            self._latest_relative_evidence = {}
            self._latest_distance_evidence = None
            self._reset_distance_temporal()

    def relative_evidence(self, sample, *, now):
        """Prepare Phase C evidence without changing current Fusion behavior."""
        with self._lock:
            if self._phase is not MeasurementPhase.MEASURING or self._baselines is None:
                self._latest_relative_evidence = {}
                self._latest_distance_evidence = None
                return {}
            evidence = {}
            face = sample.get("face_distance_cm")
            if _positive_finite(face):
                evidence["face_approach_delta_cm"] = (
                    self._baselines.face_working_baseline_cm - float(face)
                )
            ir = sample.get("ir") or []
            if ir and _nonnegative_finite(ir[0]):
                evidence["backrest_departure_delta_mm"] = (
                    float(ir[0]) - self._baselines.chair_ir_baseline_mm
                )
            self._latest_relative_evidence = evidence
            pressure = sample.get("pressure") or []
            seated = (
                len(pressure) == 4
                and sum(pressure) >= self.occupancy_min
            )
            self._latest_distance_evidence = classify_distance_evidence(
                face_distance_cm=face,
                face_approach_delta_cm=evidence.get("face_approach_delta_cm"),
                backrest_departure_delta_mm=evidence.get(
                    "backrest_departure_delta_mm"
                ),
                vision_available=(
                    sample.get("face_detected") is True
                    and _positive_finite(face)
                ),
                chair_ir_available=(
                    bool(ir) and _nonnegative_finite(ir[0])
                ),
                seated=seated,
                config=self.distance_evidence_config,
            )
            self._distance_temporal_state, self._latest_distance_temporal = (
                step_distance_temporal(
                    self._distance_temporal_state,
                    self._latest_distance_evidence,
                    now,
                    timing=self.distance_evidence_timing,
                )
            )
            return dict(evidence)

    def snapshot_for(self, monotonic_now, wall_time):
        """Build a contract payload using separate monotonic and wall clocks."""
        with self._lock:
            payload = {
                "v": 1,
                "t": round(float(wall_time), 3),
                "active": self._phase is not MeasurementPhase.OFF,
                "phase": self._phase.value,
            }
            event_session_id = self._session_id or self._last_session_id
            if event_session_id is not None:
                payload["session_id"] = event_session_id
            if self._phase is MeasurementPhase.OFF:
                return payload
            elapsed = max(float(monotonic_now) - self._started_at, 0.0)
            assessment = self._accumulator.assess(self.config)
            payload["calibration"] = self._calibration_payload(
                assessment,
                duration_ready=elapsed >= self.config.minimum_duration_sec,
                elapsed=elapsed,
            )
            return payload

    def _evaluate(self, now):
        elapsed = max(now - self._started_at, 0.0)
        assessment = self._accumulator.assess(self.config)
        if elapsed >= self.config.minimum_duration_sec and assessment.ready:
            self._baselines = assessment.baselines
            self._phase = MeasurementPhase.READY
            return CalibrationEvaluation(became_ready=True)
        if elapsed >= self.config.timeout_sec:
            self._failed = True
            return CalibrationEvaluation(became_failed=True)
        return CalibrationEvaluation()

    def _calibration_payload(self, assessment, *, duration_ready, elapsed):
        vision_ready = assessment.vision_samples_ready and assessment.vision_stable
        chair_ready = assessment.chair_ir_samples_ready and assessment.chair_ir_stable
        progress_parts = [
            min(elapsed / self.config.minimum_duration_sec, 1.0)
            if self.config.minimum_duration_sec
            else 1.0,
            min(
                len(self._accumulator.face_distances_cm)
                / self.config.minimum_vision_samples,
                1.0,
            )
            if self.config.minimum_vision_samples
            else 1.0,
            min(
                len(self._accumulator.chair_ir_distances_mm)
                / self.config.minimum_chair_ir_samples,
                1.0,
            )
            if self.config.minimum_chair_ir_samples
            else 1.0,
        ]
        if self._failed:
            status = "FAILED"
        elif self._phase is MeasurementPhase.READY:
            status = "READY"
        elif self._phase is MeasurementPhase.MEASURING:
            status = "ACCEPTED"
        else:
            status = "COLLECTING"
        value = {
            "status": status,
            "progress": round(min(progress_parts), 3),
            "valid_samples": {
                "vision": len(self._accumulator.face_distances_cm),
                "chair_ir": len(self._accumulator.chair_ir_distances_mm),
            },
            "required_samples": {
                "vision": self.config.minimum_vision_samples,
                "chair_ir": self.config.minimum_chair_ir_samples,
            },
            "readiness": {
                "duration": duration_ready,
                "vision": vision_ready,
                "chair_ir": chair_ready,
            },
        }
        if self._failed:
            value["error"] = "calibration_timeout"
        return value

    def _clear(self):
        self._session_id = None
        self._last_session_id = None
        self._phase = MeasurementPhase.OFF
        self._started_at = None
        self._accumulator = CalibrationAccumulator()
        self._baselines = None
        self._failed = False
        self._latest_relative_evidence = {}
        self._latest_distance_evidence = None
        self._reset_distance_temporal()

    def _reset_distance_temporal(self):
        self._distance_temporal_state = DistanceTemporalState()
        self._latest_distance_temporal = None


def _positive_finite(value):
    try:
        return isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _nonnegative_finite(value):
    try:
        return isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False
