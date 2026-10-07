"""Hardware-independent tests for observational face lateral geometry."""

import math

import pytest

from vision.config import LeanThresholdPolicy
from vision.lateral import (
    LEAN_CENTER,
    LEAN_LEFT,
    LEAN_RIGHT,
    LEAN_UNKNOWN,
    FaceLeanClassifier,
    angle_delta_deg,
    face_lateral_geometry,
    lateral_offset,
)


def points(*, center_x=320.0, face_width=160.0, eye_slope=0.0, scale=1.0):
    center_x *= scale
    face_width *= scale
    eye_left_x = (center_x / scale - 45.0) * scale
    eye_right_x = (center_x / scale + 45.0) * scale
    return {
        234: (center_x - face_width / 2.0, 240.0 * scale),
        454: (center_x + face_width / 2.0, 240.0 * scale),
        33: (eye_left_x, 210.0 * scale),
        263: (eye_right_x, (210.0 + eye_slope * 90.0) * scale),
    }


def test_neutral_calibrated_face_has_zero_offset():
    geometry = face_lateral_geometry(points(center_x=370), 640)

    assert lateral_offset(
        geometry,
        neutral_center_x_ratio=370 / 640,
    ) == pytest.approx(0.0)


def test_image_left_and_right_displacements_have_unambiguous_signs():
    left = face_lateral_geometry(points(center_x=280), 640)
    right = face_lateral_geometry(points(center_x=360), 640)

    assert lateral_offset(left, neutral_center_x_ratio=0.5) < 0
    assert lateral_offset(right, neutral_center_x_ratio=0.5) > 0


def test_resolution_scaling_preserves_normalized_offset_and_roll():
    small = face_lateral_geometry(points(center_x=352, eye_slope=0.1), 640)
    large = face_lateral_geometry(
        points(center_x=352, eye_slope=0.1, scale=2.0),
        1280,
    )

    small_offset = lateral_offset(small, neutral_center_x_ratio=0.5)
    large_offset = lateral_offset(large, neutral_center_x_ratio=0.5)

    assert large_offset == pytest.approx(small_offset)
    assert large.head_roll_deg == pytest.approx(small.head_roll_deg)


def test_face_size_normalization_uses_current_face_width():
    near = face_lateral_geometry(points(center_x=360, face_width=200), 640)
    far = face_lateral_geometry(points(center_x=340, face_width=100), 640)

    assert lateral_offset(near, neutral_center_x_ratio=0.5) == pytest.approx(0.2)
    assert lateral_offset(far, neutral_center_x_ratio=0.5) == pytest.approx(0.2)


def test_roll_sign_follows_image_y_axis_and_delta_wraps():
    positive = face_lateral_geometry(points(eye_slope=0.1), 640)
    negative = face_lateral_geometry(points(eye_slope=-0.1), 640)

    assert positive.head_roll_deg > 0
    assert negative.head_roll_deg < 0
    assert angle_delta_deg(-179, 179) == pytest.approx(2.0)


@pytest.mark.parametrize(
    "bad_points,frame_width",
    [
        (None, 640),
        ({234: (1, 1)}, 640),
        (points(face_width=0), 640),
        (points(), 0),
        (points(), math.nan),
        ({**points(), 454: (math.inf, 1)}, 640),
    ],
)
def test_invalid_nonfinite_or_tiny_geometry_is_unavailable(bad_points, frame_width):
    assert face_lateral_geometry(bad_points, frame_width) is None


def test_invalid_neutral_reference_is_unavailable():
    geometry = face_lateral_geometry(points(), 640)

    assert lateral_offset(geometry, neutral_center_x_ratio=None) is None
    assert lateral_offset(geometry, neutral_center_x_ratio=math.nan) is None


def test_classifier_initial_state_and_center_band():
    classifier = FaceLeanClassifier()

    assert classifier.state == LEAN_CENTER
    assert classifier.update(0.19).direction == LEAN_CENTER
    assert classifier.update(-0.14).direction == LEAN_CENTER


def test_classifier_left_entry_hold_and_exact_release():
    classifier = FaceLeanClassifier()

    entered = classifier.update(0.20)
    assert entered.direction == LEAN_LEFT
    assert entered.transitioned is True
    assert classifier.update(0.101).direction == LEAN_LEFT
    released = classifier.update(0.10)
    assert released.direction == LEAN_CENTER
    assert released.transitioned is True


def test_classifier_right_entry_hold_and_exact_release():
    classifier = FaceLeanClassifier()

    assert classifier.update(-0.15).direction == LEAN_RIGHT
    assert classifier.update(-0.081).direction == LEAN_RIGHT
    assert classifier.update(-0.08).direction == LEAN_CENTER


def test_classifier_never_jumps_directly_between_sides():
    classifier = FaceLeanClassifier()
    classifier.update(0.20)

    first_cross = classifier.update(-0.50)
    second_cross = classifier.update(-0.50)

    assert first_cross.direction == LEAN_CENTER
    assert second_cross.direction == LEAN_RIGHT

    first_return = classifier.update(0.50)
    second_return = classifier.update(0.50)

    assert first_return.direction == LEAN_CENTER
    assert second_return.direction == LEAN_LEFT


@pytest.mark.parametrize("offset", [None, "bad", math.nan, math.inf, -math.inf])
def test_classifier_unavailable_is_unknown_and_preserves_internal_state(offset):
    classifier = FaceLeanClassifier()
    classifier.update(0.20)

    unavailable = classifier.update(offset)

    assert unavailable.direction == LEAN_UNKNOWN
    assert unavailable.transitioned is False
    assert classifier.state == LEAN_LEFT
    assert classifier.update(0.11).direction == LEAN_LEFT


def test_classifier_reset_returns_to_center():
    classifier = FaceLeanClassifier()
    classifier.update(-0.15)

    classifier.reset()

    assert classifier.state == LEAN_CENTER
    assert classifier.update(0.0).direction == LEAN_CENTER


@pytest.mark.parametrize(
    "kwargs",
    [
        {"left_entry": math.nan},
        {"left_entry": 0.1, "left_release": 0.2},
        {"right_entry": -0.05, "right_release": -0.08},
    ],
)
def test_classifier_policy_rejects_invalid_threshold_bands(kwargs):
    with pytest.raises(ValueError):
        LeanThresholdPolicy(**kwargs)
