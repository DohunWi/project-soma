import pytest

from server.feedback_coordinator import FEEDBACK_DECISION_VALIDATOR
from server.feedback_device_projection import (
    MIN_BREAK_DISPLAY_SEC,
    FeedbackDeviceProjector,
)


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def feedback(*, transition, timestamp=1000.0):
    return {
        "v": 1,
        "t": timestamp,
        "session_id": SESSION_ID,
        "level": "BREAK",
        "reason": "SUSTAINED_DANGER",
        "transition": transition,
        "recommended_break_sec": 20,
    }


def test_initial_break_is_preserved_for_three_seconds_then_projects_fusion_state():
    projector = FeedbackDeviceProjector()
    initial = feedback(transition=True)

    assert projector.project(initial, {"state": "DANGER"}, 10.0) == initial
    assert projector.project(
        feedback(transition=False, timestamp=1001.0),
        {"state": "CAUTION"},
        10.0 + MIN_BREAK_DISPLAY_SEC - 0.001,
    )["level"] == "BREAK"

    projected = projector.project(
        feedback(transition=False, timestamp=1002.0),
        {"state": "CAUTION"},
        10.0 + MIN_BREAK_DISPLAY_SEC,
    )
    assert projected == {
        "v": 1,
        "t": 1002.0,
        "session_id": SESSION_ID,
        "level": "NOTICE",
        "reason": "FUSION_CAUTION",
        "transition": False,
    }


@pytest.mark.parametrize(
    ("fusion_state", "level", "reason"),
    [
        ("NORMAL", "NORMAL", None),
        ("CAUTION", "NOTICE", "FUSION_CAUTION"),
        ("DANGER", "WARNING", "FUSION_DANGER"),
    ],
)
def test_held_break_projects_steady_fusion_led_without_alert_transition(
    fusion_state,
    level,
    reason,
):
    projector = FeedbackDeviceProjector()
    projector.project(feedback(transition=True), {"state": "DANGER"}, 0.0)

    projected = projector.project(
        feedback(transition=False),
        {"state": fusion_state},
        MIN_BREAK_DISPLAY_SEC,
    )

    assert projected["level"] == level
    assert projected["transition"] is False
    assert projected.get("reason") == reason
    assert "recommended_break_sec" not in projected
    assert FEEDBACK_DECISION_VALIDATOR.is_valid(projected)


def test_reset_discards_break_display_window():
    projector = FeedbackDeviceProjector()
    projector.project(feedback(transition=True), {"state": "DANGER"}, 10.0)
    projector.reset()

    projected = projector.project(
        feedback(transition=False),
        {"state": "NORMAL"},
        10.1,
    )
    assert projected["level"] == "NORMAL"


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_break_display_duration_must_be_positive_and_finite(duration):
    with pytest.raises(ValueError):
        FeedbackDeviceProjector(duration)
