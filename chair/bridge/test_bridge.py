"""Chair serial parsing and development raw logging helpers."""

from chair.bridge.bridge import parse_line, raw_log_row


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
