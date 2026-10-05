"""Pure robust baseline calculation for one measurement calibration window.

This module owns no clock, socket, session, or I/O.  The backend decides when
to observe a Chair-authoritative merged sample and when to evaluate readiness.
"""
from dataclasses import dataclass, field
from math import isfinite
from statistics import median


@dataclass(frozen=True)
class CalibrationConfig:
    minimum_duration_sec: float
    minimum_vision_samples: int
    minimum_chair_ir_samples: int
    timeout_sec: float
    face_mad_limit_cm: float = 4.0
    chair_ir_mad_limit_mm: float = 40.0

    def __post_init__(self):
        if self.minimum_duration_sec < 0 or self.timeout_sec <= 0:
            raise ValueError("calibration duration must be non-negative and timeout positive")
        if self.minimum_duration_sec >= self.timeout_sec:
            raise ValueError("calibration timeout must exceed minimum duration")
        if self.minimum_vision_samples < 0 or self.minimum_chair_ir_samples < 0:
            raise ValueError("calibration sample requirements must be non-negative")
        if self.face_mad_limit_cm < 0 or self.chair_ir_mad_limit_mm < 0:
            raise ValueError("calibration MAD limits must be non-negative")


@dataclass(frozen=True)
class WorkingBaselines:
    face_working_baseline_cm: float
    chair_ir_baseline_mm: float
    face_mad_cm: float
    chair_ir_mad_mm: float
    vision_samples: int
    chair_ir_samples: int


@dataclass(frozen=True)
class CalibrationAssessment:
    baselines: WorkingBaselines | None
    vision_samples_ready: bool
    chair_ir_samples_ready: bool
    vision_stable: bool
    chair_ir_stable: bool

    @property
    def ready(self):
        return self.baselines is not None


def median_absolute_deviation(values):
    """Return the unscaled median absolute deviation of finite observations."""
    finite = [float(value) for value in values if isfinite(float(value))]
    if not finite:
        raise ValueError("at least one finite value is required")
    center = median(finite)
    return float(median(abs(value - center) for value in finite))


@dataclass
class CalibrationAccumulator:
    """Collect valid, distinct samples and derive median/MAD baselines."""

    face_distances_cm: list[float] = field(default_factory=list)
    chair_ir_distances_mm: list[float] = field(default_factory=list)
    _vision_sample_ids: set[float] = field(default_factory=set, repr=False)

    def observe(
        self,
        sample,
        *,
        occupancy_min,
        vision_sample_id=None,
    ):
        if vision_sample_id is None:
            vision_sample_id = sample.get("_vision_sender_t")
        pressure = sample.get("pressure") or []
        ir = sample.get("ir") or []
        occupied = len(pressure) == 4 and sum(pressure) >= occupancy_min
        if occupied and ir and _finite_at_least(ir[0], 0.0):
            self.chair_ir_distances_mm.append(float(ir[0]))

        face_distance = sample.get("face_distance_cm")
        face_detected = sample.get("face_detected") is True
        distinct = (
            vision_sample_id is None
            or float(vision_sample_id) not in self._vision_sample_ids
        )
        if face_detected and _finite_at_least(face_distance, 0.0, strict=True) and distinct:
            self.face_distances_cm.append(float(face_distance))
            if vision_sample_id is not None:
                self._vision_sample_ids.add(float(vision_sample_id))

    def assess(self, config):
        vision_count_ready = len(self.face_distances_cm) >= config.minimum_vision_samples
        chair_count_ready = (
            len(self.chair_ir_distances_mm) >= config.minimum_chair_ir_samples
        )
        face_mad = (
            median_absolute_deviation(self.face_distances_cm)
            if self.face_distances_cm
            else None
        )
        chair_mad = (
            median_absolute_deviation(self.chair_ir_distances_mm)
            if self.chair_ir_distances_mm
            else None
        )
        vision_stable = face_mad is not None and face_mad <= config.face_mad_limit_cm
        chair_stable = (
            chair_mad is not None and chair_mad <= config.chair_ir_mad_limit_mm
        )
        baselines = None
        if vision_count_ready and chair_count_ready and vision_stable and chair_stable:
            baselines = WorkingBaselines(
                face_working_baseline_cm=float(median(self.face_distances_cm)),
                chair_ir_baseline_mm=float(median(self.chair_ir_distances_mm)),
                face_mad_cm=face_mad,
                chair_ir_mad_mm=chair_mad,
                vision_samples=len(self.face_distances_cm),
                chair_ir_samples=len(self.chair_ir_distances_mm),
            )
        return CalibrationAssessment(
            baselines=baselines,
            vision_samples_ready=vision_count_ready,
            chair_ir_samples_ready=chair_count_ready,
            vision_stable=vision_stable,
            chair_ir_stable=chair_stable,
        )


def _finite_at_least(value, minimum, *, strict=False):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    if not isfinite(numeric):
        return False
    return numeric > minimum if strict else numeric >= minimum
