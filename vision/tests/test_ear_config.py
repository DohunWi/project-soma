"""Vision personalized EAR threshold policy tests."""

import math

import pytest

from vision.blink.ear import BlinkCounter
from vision.config import (
    EAR_CLOSED_RATIO,
    EAR_FALLBACK_CLOSED,
    EAR_FALLBACK_OPEN,
    EAR_OPEN_RATIO,
    EarThresholdPolicy,
    resolve_ear_thresholds,
)


def test_valid_baseline_produces_relative_thresholds_for_injection():
    selection = resolve_ear_thresholds(0.80)
    counter = BlinkCounter(
        closed_threshold=selection.closed_threshold,
        open_threshold=selection.open_threshold,
    )

    assert selection.personalized is True
    assert selection.open_ear_baseline == pytest.approx(0.80)
    assert counter.closed_threshold == pytest.approx(0.80 * EAR_CLOSED_RATIO)
    assert counter.open_threshold == pytest.approx(0.80 * EAR_OPEN_RATIO)


@pytest.mark.parametrize(
    "baseline",
    [None, "", "not-a-number", 0.0, -0.1, math.nan, math.inf],
)
def test_invalid_or_missing_baseline_uses_absolute_fallback(baseline):
    selection = resolve_ear_thresholds(baseline)

    assert selection.personalized is False
    assert selection.open_ear_baseline is None
    assert selection.closed_threshold == EAR_FALLBACK_CLOSED == 0.21
    assert selection.open_threshold == EAR_FALLBACK_OPEN == 0.25


@pytest.mark.parametrize(
    "closed_ratio,open_ratio",
    [(0.5, 0.5), (0.6, 0.5), (math.nan, 0.5), (0.2, math.inf)],
)
def test_personalized_threshold_policy_rejects_invalid_ordering(
    closed_ratio,
    open_ratio,
):
    with pytest.raises(ValueError):
        EarThresholdPolicy(
            closed_ratio=closed_ratio,
            open_ratio=open_ratio,
        )
