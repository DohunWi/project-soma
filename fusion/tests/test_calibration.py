import math

from fusion.calibration import (
    CalibrationAccumulator,
    median_absolute_deviation,
)
from fusion.state import OCCUPANCY_MIN
from server.config import DEMO_PROFILE, NORMAL_PROFILE


def sample(*, pressure=None, ir=250, face=60.0, detected=True, vision_t=1.0):
    return {
        "pressure": pressure or [300, 300, 300, 300],
        "ir": [ir],
        "face_detected": detected,
        "face_distance_cm": face,
        "_vision_sender_t": vision_t,
    }


def test_median_and_mad_produce_robust_working_baselines():
    accumulator = CalibrationAccumulator()
    for index, (face, ir) in enumerate(
        [(60, 250), (61, 252), (59, 248), (60, 251), (100, 800)]
    ):
        accumulator.observe(
            sample(face=face, ir=ir, vision_t=index),
            occupancy_min=OCCUPANCY_MIN,
        )

    assessment = accumulator.assess(DEMO_PROFILE.calibration)

    assert assessment.ready is True
    assert assessment.baselines.face_working_baseline_cm == 60.0
    assert assessment.baselines.chair_ir_baseline_mm == 251.0
    assert assessment.baselines.face_mad_cm == 1.0
    assert assessment.baselines.chair_ir_mad_mm == 1.0
    assert median_absolute_deviation([1, 2, 100]) == 1.0


def test_invalid_chair_and_vision_observations_are_excluded_and_vision_deduplicates():
    accumulator = CalibrationAccumulator()
    observations = [
        sample(pressure=[1, 1, 1, 1], vision_t=1),
        sample(ir=-1, vision_t=2),
        sample(face=-1, vision_t=3),
        sample(face=math.nan, vision_t=4),
        sample(detected=False, vision_t=5),
        sample(vision_t=6),
        sample(vision_t=6),
    ]
    for value in observations:
        accumulator.observe(value, occupancy_min=OCCUPANCY_MIN)

    assert len(accumulator.chair_ir_distances_mm) == 5
    assert accumulator.face_distances_cm == [60.0, 60.0, 60.0]


def test_demo_and_normal_calibration_requirements_are_explicit():
    demo = DEMO_PROFILE.calibration
    normal = NORMAL_PROFILE.calibration

    assert (
        demo.minimum_duration_sec,
        demo.minimum_vision_samples,
        demo.minimum_chair_ir_samples,
        demo.timeout_sec,
    ) == (5.0, 5, 5, 30.0)
    assert (
        normal.minimum_duration_sec,
        normal.minimum_vision_samples,
        normal.minimum_chair_ir_samples,
        normal.timeout_sec,
    ) == (8.0, 8, 8, 45.0)
    assert demo.face_mad_limit_cm == normal.face_mad_limit_cm == 4.0
    assert demo.chair_ir_mad_limit_mm == normal.chair_ir_mad_limit_mm == 40.0


def test_unstable_face_samples_do_not_create_a_baseline():
    accumulator = CalibrationAccumulator()
    for index, face in enumerate((40, 50, 60, 70, 80)):
        accumulator.observe(
            sample(face=face, vision_t=index),
            occupancy_min=OCCUPANCY_MIN,
        )

    assessment = accumulator.assess(DEMO_PROFILE.calibration)

    assert assessment.vision_samples_ready is True
    assert assessment.vision_stable is False
    assert assessment.ready is False
