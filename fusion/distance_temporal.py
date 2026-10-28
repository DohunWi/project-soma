"""Pure sustained Phase C evidence policy, independent of severity and SOMA Load.

Only intervals bracketed by compatible known observations count. UNKNOWN freezes
pending/active evidence and recovery; its entry and exit intervals never count.
No unavailable expiry or penalty is implied by a latched sustained observation.
"""
from dataclasses import dataclass, replace
from math import isfinite

from fusion.config import DEMO_DISTANCE_EVIDENCE_TIMING, DistanceEvidenceTiming
from fusion.distance_evidence import DistanceEvidenceClass, DistanceEvidenceResult


FORWARD_CLASSES = (
    DistanceEvidenceClass.BODY_FORWARD_CLOSE,
    DistanceEvidenceClass.FACE_ONLY_CLOSE,
)
RECOVERY_CLASSES = (
    DistanceEvidenceClass.NORMAL,
    DistanceEvidenceClass.BACKREST_AWAY,
)


@dataclass(frozen=True)
class DistanceTemporalState:
    last_t: float | None = None
    last_classification: DistanceEvidenceClass | None = None
    candidate_classification: DistanceEvidenceClass | None = None
    accumulated_sec: float = 0.0
    sustained_classification: DistanceEvidenceClass | None = None
    recovery_sec: float = 0.0


@dataclass(frozen=True)
class DistanceTemporalResult:
    instantaneous_classification: DistanceEvidenceClass
    candidate_classification: DistanceEvidenceClass | None
    accumulated_sec: float
    sustained_classification: DistanceEvidenceClass | None
    recovery_sec: float
    reasons: tuple[str, ...]

    @property
    def active(self):
        """Latched evidence, NOT permission to penalize an unavailable sample."""
        return self.sustained_classification is not None

    def as_dict(self):
        value = {
            "instantaneous_classification": self.instantaneous_classification.value,
            "accumulated_sec": self.accumulated_sec,
            "recovery_sec": self.recovery_sec,
            "active": self.active,
            "reasons": list(self.reasons),
        }
        for name in ("candidate_classification", "sustained_classification"):
            classification = getattr(self, name)
            if classification is not None:
                value[name] = classification.value
        return value


def _result(state, classification, *reasons):
    return DistanceTemporalResult(
        instantaneous_classification=classification,
        candidate_classification=state.candidate_classification,
        accumulated_sec=state.accumulated_sec,
        sustained_classification=state.sustained_classification,
        recovery_sec=state.recovery_sec,
        reasons=tuple(reasons),
    )


def step(
    state: DistanceTemporalState,
    evidence: DistanceEvidenceResult,
    now: float,
    *,
    timing: DistanceEvidenceTiming = DEMO_DISTANCE_EVIDENCE_TIMING,
) -> tuple[DistanceTemporalState, DistanceTemporalResult]:
    """Advance on a Chair tick, returning new immutable state and diagnostics.

Changing forward type starts its own entry timer (no pooled weak/strong time).
An active type remains latched until another type qualifies or known no-close
evidence completes recovery. Pending, not-yet-sustained evidence clears at once
on NORMAL/BACKREST_AWAY, preventing repeated brief movements from adding up.
"""
    now = float(now)
    if not isfinite(now):
        raise ValueError("distance evidence timestamp must be finite")
    classification = evidence.classification
    if not evidence.seated:
        cleared = DistanceTemporalState(last_t=now)
        return cleared, _result(cleared, classification, "absent_reset")
    if state.last_t is not None and now <= state.last_t:
        return state, _result(state, classification, "timestamp_ignored")

    dt = 0.0 if state.last_t is None else now - state.last_t
    gap = dt > timing.max_gap_sec
    if gap:
        dt = 0.0
    updated = replace(state, last_t=now, last_classification=classification)
    if classification is DistanceEvidenceClass.UNKNOWN:
        return updated, _result(updated, classification, "unavailable_freeze")

    reasons = ["gap_not_accumulated"] if gap else []
    if classification in FORWARD_CLASSES:
        elapsed = (
            state.accumulated_sec
            if state.candidate_classification is classification else 0.0
        )
        if state.last_classification is classification:
            elapsed += dt
        threshold = (
            timing.body_forward_enter_sec
            if classification is DistanceEvidenceClass.BODY_FORWARD_CLOSE
            else timing.face_only_enter_sec
        )
        sustained = state.sustained_classification
        if elapsed >= threshold:
            sustained = classification
            reasons.append("sustained")
        else:
            reasons.append("enter_pending")
        updated = replace(
            updated,
            candidate_classification=classification,
            accumulated_sec=elapsed,
            sustained_classification=sustained,
            recovery_sec=0.0,
        )
    else:
        # BACKREST_AWAY means no face approach, not harmful posture. It cannot
        # activate forward evidence, and can recover an earlier close episode.
        recovery = state.recovery_sec
        sustained = state.sustained_classification
        if sustained is not None and state.last_classification in RECOVERY_CLASSES:
            recovery += dt
        if sustained is not None and recovery >= timing.recovery_sec:
            sustained = None
            reasons.append("recovered")
        elif sustained is not None:
            reasons.append("recovering")
        if classification is DistanceEvidenceClass.BACKREST_AWAY:
            reasons.append("backrest_observation_only")
        elif not reasons:
            reasons.append("normal")
        updated = replace(
            updated,
            candidate_classification=None,
            accumulated_sec=0.0,
            sustained_classification=sustained,
            recovery_sec=recovery if sustained is not None else 0.0,
        )
    return updated, _result(updated, classification, *reasons)
