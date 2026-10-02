"""Chair serial parsing, transport diagnostics, and raw logging helpers."""

import io
import json

import pytest

from chair.bridge.bridge import (
    ChairMonitor,
    build_parser,
    dispatch_sample,
    emit_sensor_data,
    format_monitor_line,
    parse_line,
    raw_log_row,
)


def sample_event():
    return {
        "v": 1,
        "t": 1000.125,
        "source": "chair",
        "device_id": "smart_chair_01",
        "user_name": "guest",
        "chair": {"pressure": [938, 912, 965, 927], "ir": [505]},
    }


def test_raw_log_has_named_physical_channels_and_timestamp():
    pressure, ir = parse_line("D,101,202,303,404,505")

    assert raw_log_row(1000.125, pressure, ir) == {
        "t": 1000.125,
        "fl": 101,
        "fr": 202,
        "bl": 303,
        "br": 404,
        "ir": 505,
    }


def test_debug_emit_uses_sensor_event_without_exposing_authentication():
    emitted = []
    diagnostics = []

    class FakeSocket:
        def emit(self, name, payload):
            emitted.append((name, payload))

    payload = {
        "v": 1,
        "t": 1000.125,
        "source": "chair",
        "device_id": "smart_chair_01",
        "user_name": "guest",
        "chair": {"pressure": [101, 202, 303, 404], "ir": [505]},
    }
    emit_sensor_data(
        FakeSocket(),
        payload,
        debug=True,
        diagnostic=diagnostics.append,
    )

    assert emitted == [("sensor_data", payload)]
    assert diagnostics == [
        "[bridge-debug] sensor_data emit requested "
        "t=1000.125 device_id=smart_chair_01"
    ]
    assert "token" not in diagnostics[0].lower()


def test_monitor_flag_parses_without_changing_stdout_default():
    args = build_parser().parse_args(["--monitor"])

    assert args.monitor is True
    assert args.stdout is False


def test_monitor_output_formats_all_fsr_values_sum_and_diff():
    line = format_monitor_line([938, 912, 965, 927])

    assert line == (
        "[chair-monitor] FL=  938 FR=  912 BL=  965 BR=  927 "
        "SUM= 3742 DIFF=   +64"
    )


def test_monitor_is_rate_limited_to_about_one_hz():
    times = iter([10.0, 10.5, 11.0])
    lines = []
    monitor = ChairMonitor(clock=lambda: next(times), diagnostic=lines.append)

    assert monitor.observe([1, 2, 3, 4]) is True
    assert monitor.observe([5, 6, 7, 8]) is False
    assert monitor.observe([9, 10, 11, 12]) is True
    assert len(lines) == 2


def test_invalid_monitor_interval_is_rejected():
    with pytest.raises(ValueError, match="greater than zero"):
        ChairMonitor(interval_sec=0)


def test_monitor_does_not_disable_backend_emit_path():
    emitted = []
    observed = []

    class Monitor:
        def observe(self, pressure):
            observed.append(pressure)

    event = sample_event()
    dispatch_sample(
        event,
        raw_ir=505,
        monitor=Monitor(),
        emit=emitted.append,
    )

    assert observed == [[938, 912, 965, 927]]
    assert emitted == [event]


def test_stdout_keeps_inspection_only_behavior_with_monitor():
    stdout_lines = []

    class Monitor:
        def observe(self, pressure):
            return None

    event = sample_event()
    dispatch_sample(
        event,
        raw_ir=505,
        monitor=Monitor(),
        emit=None,
        stdout=stdout_lines.append,
    )

    assert json.loads(stdout_lines[0]) == event


def test_raw_log_monitor_and_backend_emit_can_run_together():
    raw_log = io.StringIO()
    observed = []
    emitted = []

    class Monitor:
        def observe(self, pressure):
            observed.append(pressure)

    event = sample_event()
    dispatch_sample(
        event,
        raw_ir=505,
        raw_log=raw_log,
        monitor=Monitor(),
        emit=emitted.append,
    )

    assert json.loads(raw_log.getvalue()) == {
        "t": 1000.125,
        "fl": 938,
        "fr": 912,
        "bl": 965,
        "br": 927,
        "ir": 505,
    }
    assert observed == [[938, 912, 965, 927]]
    assert emitted == [event]
