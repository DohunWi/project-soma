import math

import pytest

from fusion.distance_evidence import (
    DEFAULT_DISTANCE_EVIDENCE_CONFIG,
    DistanceEvidenceClass,
    DistanceEvidenceConfig,
    classify_distance_evidence,
)


def classify(face_delta, chair_delta, *, face_distance=None, **overrides):
    values = {
        "face_distance_cm": (
            60.0 - face_delta if face_distance is None else face_distance
        ),
        "face_approach_delta_cm": face_delta,
        "backrest_departure_delta_mm": chair_delta,
        "vision_available": True,
        "chair_ir_available": True,
        "seated": True,
    }
    values.update(overrides)
    return classify_distance_evidence(**values)


@pytest.mark.parametrize(
    ("face_distance", "face_delta", "chair_delta", "expected"),
    [
        (60.65, 1.25, -63.5, DistanceEvidenceClass.NORMAL),
        (43.9, 18.0, -21.0, DistanceEvidenceClass.FACE_ONLY_CLOSE),
        (43.9, 18.0, 128.5, DistanceEvidenceClass.BODY_FORWARD_CLOSE),
        (57.0, 4.9, 56.0, DistanceEvidenceClass.BACKREST_AWAY),
        (66.0, 0.9, 8.0, DistanceEvidenceClass.NORMAL),
    ],
)
def test_controlled_hardware_observations(
    face_distance,
    face_delta,
    chair_delta,
    expected,
):
    result = classify(
        face_delta,
        chair_delta,
        face_distance=face_distance,
    )

    assert result.classification is expected
    assert result.face_distance_cm == face_distance


@pytest.mark.parametrize(
    ("face_delta", "chair_delta", "expected"),
    [
        (7.999, 39.999, DistanceEvidenceClass.NORMAL),
        (8.0, 39.999, DistanceEvidenceClass.FACE_ONLY_CLOSE),
        (8.001, 39.999, DistanceEvidenceClass.FACE_ONLY_CLOSE),
        (7.999, 40.0, DistanceEvidenceClass.BACKREST_AWAY),
        (7.999, 40.001, DistanceEvidenceClass.BACKREST_AWAY),
        (8.0, 40.0, DistanceEvidenceClass.BODY_FORWARD_CLOSE),
    ],
)
def test_candidate_threshold_boundaries(face_delta, chair_delta, expected):
    assert classify(face_delta, chair_delta).classification is expected


def test_negative_deltas_are_valid_normal_evidence():
    result = classify(-5.0, -30.0)

    assert result.classification is DistanceEvidenceClass.NORMAL
    assert result.face_approach_active is False
    assert result.backrest_departure_active is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"vision_available": False},
        {"face_distance_cm": None},
        {"face_distance_cm": 0.0},
        {"face_approach_delta_cm": math.nan},
        {"chair_ir_available": False},
        {"backrest_departure_delta_mm": None},
        {"backrest_departure_delta_mm": math.inf},
        {"seated": False},
    ],
)
def test_missing_stale_invalid_or_absent_is_unknown(overrides):
    result = classify(18.0, 128.5, **overrides)

    assert result.classification is DistanceEvidenceClass.UNKNOWN
    assert result.face_approach_active is False
    assert result.backrest_departure_active is False


def test_result_is_observational_and_serializable():
    result = classify(18.0, 128.5)

    assert result.as_dict() == {
        "classification": "BODY_FORWARD_CLOSE",
        "face_approach_active": True,
        "backrest_departure_active": True,
        "vision_available": True,
        "chair_ir_available": True,
        "seated": True,
        "reasons": ["face_approach", "backrest_departure"],
        "face_distance_cm": 42.0,
        "face_approach_delta_cm": 18.0,
        "backrest_departure_delta_mm": 128.5,
    }
    assert DEFAULT_DISTANCE_EVIDENCE_CONFIG.face_approach_threshold_cm == 8.0
    assert (
        DEFAULT_DISTANCE_EVIDENCE_CONFIG.backrest_departure_threshold_mm
        == 40.0
    )


def test_thresholds_are_explicitly_injectable():
    result = classify_distance_evidence(
        face_distance_cm=43.9,
        face_approach_delta_cm=18.0,
        backrest_departure_delta_mm=56.0,
        vision_available=True,
        chair_ir_available=True,
        seated=True,
        config=DistanceEvidenceConfig(
            face_approach_threshold_cm=20.0,
            backrest_departure_threshold_mm=60.0,
        ),
    )

    assert result.classification is DistanceEvidenceClass.NORMAL
