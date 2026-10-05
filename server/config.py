"""Runtime profile selection for the Soma backend."""
import os
from dataclasses import dataclass

from fusion.config import (
    DEMO_DISTANCE_EVIDENCE_TIMING,
    DEMO_FUSION_TIMING,
    DEMO_SOMA_LOAD_CONFIG,
    NORMAL_DISTANCE_EVIDENCE_TIMING,
    NORMAL_FUSION_TIMING,
    NORMAL_SOMA_LOAD_CONFIG,
    DistanceEvidenceTiming,
    FusionTiming,
    SomaLoadConfig,
)
from fusion.calibration import CalibrationConfig


class ProfileConfigError(ValueError):
    """SOMA_MODE contains a value the backend cannot run."""


@dataclass(frozen=True)
class StoragePolicy:
    """Periodic state_logs snapshot cadence for one runtime profile."""

    db_snapshot_interval_sec: float


@dataclass(frozen=True)
class SensorMergePolicy:
    """Source freshness rules shared by every runtime profile."""

    vision_freshness_sec: float
    vision_max_skew_sec: float


@dataclass(frozen=True)
class RuntimeProfile:
    """Independent Fusion and storage settings selected for one server run."""

    name: str
    fusion: FusionTiming
    load: SomaLoadConfig
    storage: StoragePolicy
    calibration: CalibrationConfig
    distance_evidence_timing: DistanceEvidenceTiming


DEFAULT_SENSOR_MERGE_POLICY = SensorMergePolicy(
    vision_freshness_sec=3.0,
    vision_max_skew_sec=3.0,
)


DEMO_PROFILE = RuntimeProfile(
    name="demo",
    fusion=DEMO_FUSION_TIMING,
    load=DEMO_SOMA_LOAD_CONFIG,
    distance_evidence_timing=DEMO_DISTANCE_EVIDENCE_TIMING,
    storage=StoragePolicy(db_snapshot_interval_sec=5.0),
    calibration=CalibrationConfig(
        minimum_duration_sec=5.0,
        minimum_vision_samples=5,
        minimum_chair_ir_samples=5,
        timeout_sec=30.0,
    ),
)

NORMAL_PROFILE = RuntimeProfile(
    name="normal",
    fusion=NORMAL_FUSION_TIMING,
    load=NORMAL_SOMA_LOAD_CONFIG,
    distance_evidence_timing=NORMAL_DISTANCE_EVIDENCE_TIMING,
    storage=StoragePolicy(db_snapshot_interval_sec=30.0),
    calibration=CalibrationConfig(
        minimum_duration_sec=8.0,
        minimum_vision_samples=8,
        minimum_chair_ir_samples=8,
        timeout_sec=45.0,
    ),
)

PROFILES = {
    DEMO_PROFILE.name: DEMO_PROFILE,
    NORMAL_PROFILE.name: NORMAL_PROFILE,
}


def load_runtime_profile(mode=None):
    """Resolve SOMA_MODE, defaulting only a missing variable to Demo."""
    selected = os.getenv("SOMA_MODE") if mode is None else mode
    if selected is None:
        return DEMO_PROFILE
    if selected not in PROFILES:
        allowed = ", ".join(PROFILES)
        raise ProfileConfigError(
            f"Invalid SOMA_MODE={selected!r}. Expected one of: {allowed}."
        )
    return PROFILES[selected]
