"""Pure formatting helpers for the optional Vision preview diagnostics."""


def diagnostic_lines(
    *, detected, calibrating, progress, ear, closed_threshold, open_threshold,
    blink_rate, observed_sec, distance_cm, lean_direction,
):
    """Return compact overlay lines without changing any Vision calculation."""
    calibration = f"CAL {progress * 100:.0f}%" if calibrating else "CAL READY"
    face = "FACE YES" if detected else "FACE NO"
    ear_text = f"{ear:.3f}" if ear is not None else "NA"
    rate = (
        f"{blink_rate:.1f}/min"
        if blink_rate is not None
        else f"observing {observed_sec:.0f}s"
    )
    distance = f"{distance_cm:.1f}cm" if distance_cm is not None else "NA"
    return [
        f"{face} | {calibration}",
        f"EAR {ear_text} | C<{closed_threshold:.3f} O>{open_threshold:.3f}",
        f"BLINK {rate} | DIST {distance}",
        f"LEAN {lean_direction}",
    ]
