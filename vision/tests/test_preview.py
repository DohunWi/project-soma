"""Optional preview diagnostics remain formatting-only and tolerate missing data."""

from vision.preview import diagnostic_lines


def test_preview_diagnostics_include_requested_runtime_observations():
    lines = diagnostic_lines(
        detected=True,
        calibrating=False,
        progress=0.0,
        ear=0.241,
        closed_threshold=0.054,
        open_threshold=0.120,
        blink_rate=7.0,
        observed_sec=60.0,
        distance_cm=59.4,
        lean_direction="LEFT",
    )
    text = "\n".join(lines)
    assert "FACE YES" in text
    assert "CAL READY" in text
    assert "EAR 0.241" in text
    assert "C<0.054 O>0.120" in text
    assert "BLINK 7.0/min" in text
    assert "DIST 59.4cm" in text
    assert "LEAN LEFT" in text


def test_preview_diagnostics_show_missing_face_and_calibration_progress():
    lines = diagnostic_lines(
        detected=False,
        calibrating=True,
        progress=0.25,
        ear=None,
        closed_threshold=0.21,
        open_threshold=0.25,
        blink_rate=None,
        observed_sec=2.0,
        distance_cm=None,
        lean_direction="UNKNOWN",
    )
    text = "\n".join(lines)
    assert "FACE NO" in text
    assert "CAL 25%" in text
    assert "EAR NA" in text
    assert "observing 2s" in text
    assert "DIST NA" in text
