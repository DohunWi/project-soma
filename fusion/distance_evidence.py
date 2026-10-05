"""Pure observational classifier for session-relative Phase C evidence.

The output is diagnostic evidence only.  It does not calculate a penalty,
score, Fusion severity, or feedback decision.
"""
from dataclasses import dataclass
from enum import Enum
from math import isfinite


def _finite(value):
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive_finite(value):
    return _finite(value) and float(value) > 0


def _finite_value(value, *, positive=False):
    valid = _positive_finite(value) if positive else _finite(value)
    return float(value) if valid else None


class DistanceEvidenceClass(str, Enum):
    NORMAL = "NORMAL"
    FACE_ONLY_CLOSE = "FACE_ONLY_CLOSE"
    BODY_FORWARD_CLOSE = "BODY_FORWARD_CLOSE"
    BACKREST_AWAY = "BACKREST_AWAY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DistanceEvidenceConfig:
    """Provisional engineering candidates, not medical posture thresholds."""

    face_approach_threshold_cm: float
    backrest_departure_threshold_mm: float

    def __post_init__(self):
        if not _positive_finite(self.face_approach_threshold_cm):
            raise ValueError("face approach threshold must be positive and finite")
        if not _positive_finite(self.backrest_departure_threshold_mm):
            raise ValueError("backrest departure threshold must be positive and finite")


# One controlled hardware session separated the largest non-close face delta
# (+4.9 cm) from close examples (+18 cm), and repeated baselines varied by only
# ~0.9 cm.  10 cm is an intentionally provisional candidate within that gap.
# Chair IR separated FACE_ONLY_CLOSE (-21 mm) from BACKREST_AWAY (+56 mm) and
# BODY_FORWARD_CLOSE (+128.5 mm); repeat baselines spread ~8 mm.  40 mm stays
# above observed baseline variation while preserving the controlled separation.
DEFAULT_DISTANCE_EVIDENCE_CONFIG = DistanceEvidenceConfig(
    face_approach_threshold_cm=10.0,
    backrest_departure_threshold_mm=40.0,
)


@dataclass(frozen=True)
class DistanceEvidenceResult:
    classification: DistanceEvidenceClass
    face_approach_active: bool
    backrest_departure_active: bool
    vision_available: bool
    chair_ir_available: bool
    seated: bool
    reasons: tuple[str, ...]
    face_distance_cm: float | None = None
    face_approach_delta_cm: float | None = None
    backrest_departure_delta_mm: float | None = None

    def as_dict(self):
        result = {
            "classification": self.classification.value,
            "face_approach_active": self.face_approach_active,
            "backrest_departure_active": self.backrest_departure_active,
            "vision_available": self.vision_available,
            "chair_ir_available": self.chair_ir_available,
            "seated": self.seated,
            "reasons": list(self.reasons),
        }
        for name in (
            "face_distance_cm",
            "face_approach_delta_cm",
            "backrest_departure_delta_mm",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


def classify_distance_evidence(
    *,
    face_distance_cm,
    face_approach_delta_cm,
    backrest_departure_delta_mm,
    vision_available,
    chair_ir_available,
    seated,
    config=DEFAULT_DISTANCE_EVIDENCE_CONFIG,
):
    """Classify one instantaneous observation without temporal state."""
    face_valid = (
        bool(vision_available)
        and _positive_finite(face_distance_cm)
        and _finite(face_approach_delta_cm)
    )
    chair_valid = bool(chair_ir_available) and _finite(
        backrest_departure_delta_mm
    )
    if not seated or not face_valid or not chair_valid:
        reasons = []
        if not seated:
            reasons.append("not_seated")
        if not face_valid:
            reasons.append("vision_unavailable")
        if not chair_valid:
            reasons.append("chair_ir_unavailable")
        return DistanceEvidenceResult(
            classification=DistanceEvidenceClass.UNKNOWN,
            face_approach_active=False,
            backrest_departure_active=False,
            vision_available=face_valid,
            chair_ir_available=chair_valid,
            seated=bool(seated),
            reasons=tuple(reasons),
            face_distance_cm=_finite_value(face_distance_cm, positive=True),
            face_approach_delta_cm=_finite_value(face_approach_delta_cm),
            backrest_departure_delta_mm=_finite_value(
                backrest_departure_delta_mm
            ),
        )

    face_delta = float(face_approach_delta_cm)
    chair_delta = float(backrest_departure_delta_mm)
    face_active = face_delta >= config.face_approach_threshold_cm
    chair_active = chair_delta >= config.backrest_departure_threshold_mm
    if face_active and chair_active:
        classification = DistanceEvidenceClass.BODY_FORWARD_CLOSE
    elif face_active:
        classification = DistanceEvidenceClass.FACE_ONLY_CLOSE
    elif chair_active:
        classification = DistanceEvidenceClass.BACKREST_AWAY
    else:
        classification = DistanceEvidenceClass.NORMAL
    reasons = tuple(
        reason
        for active, reason in (
            (face_active, "face_approach"),
            (chair_active, "backrest_departure"),
        )
        if active
    )
    return DistanceEvidenceResult(
        classification=classification,
        face_approach_active=face_active,
        backrest_departure_active=chair_active,
        vision_available=True,
        chair_ir_available=True,
        seated=True,
        reasons=reasons,
        face_distance_cm=float(face_distance_cm),
        face_approach_delta_cm=face_delta,
        backrest_departure_delta_mm=chair_delta,
    )
