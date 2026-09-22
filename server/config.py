"""Runtime profile selection for the Soma backend."""
import os
from dataclasses import dataclass

from fusion.config import DEMO_FUSION_TIMING, NORMAL_FUSION_TIMING, FusionTiming


class ProfileConfigError(ValueError):
    """SOMA_MODE contains a value the backend cannot run."""


@dataclass(frozen=True)
class StoragePolicy:
    """Storage cadence reserved for the later state_logs implementation."""

    db_snapshot_interval_sec: float


@dataclass(frozen=True)
class RuntimeProfile:
    """Independent Fusion and storage settings selected for one server run."""

    name: str
    fusion: FusionTiming
    storage: StoragePolicy


DEMO_PROFILE = RuntimeProfile(
    name="demo",
    fusion=DEMO_FUSION_TIMING,
    storage=StoragePolicy(db_snapshot_interval_sec=5.0),
)

NORMAL_PROFILE = RuntimeProfile(
    name="normal",
    fusion=NORMAL_FUSION_TIMING,
    storage=StoragePolicy(db_snapshot_interval_sec=30.0),
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
