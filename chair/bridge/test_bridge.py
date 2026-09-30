"""Chair serial parsing, transport diagnostics, and raw logging helpers."""

from chair.bridge.bridge import emit_sensor_data, parse_line, raw_log_row


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
