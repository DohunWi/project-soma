"""Hardware-independent tests for the Feedback Nano bridge."""
import pytest

from feedback.nano.bridge import (
    OFF_COMMAND,
    NanoBridge,
    NanoSerialTransport,
    commands_for_decision,
)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeSerial:
    def __init__(self):
        self.lines = []
        self.writes = []
        self.closed = False
        self.fail_write = False

    @property
    def in_waiting(self):
        return len(self.lines)

    def readline(self):
        return self.lines.pop(0)

    def write(self, data):
        if self.fail_write:
            raise OSError("serial write failed")
        self.writes.append(data.decode("ascii").strip())

    def close(self):
        self.closed = True


class SerialFactory:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSocket:
    def __init__(self, outcomes=None):
        self.connected = False
        self.outcomes = list(outcomes or [True])
        self.handlers = {}
        self.connect_calls = []
        self.disconnected = False

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.connected = True
        self.handlers["connect"]()

    def disconnect(self):
        self.connected = False
        self.disconnected = True

    def trigger(self, event, payload=None):
        if payload is None:
            return self.handlers[event]()
        return self.handlers[event](payload)


class RecordingTransport:
    def __init__(self):
        self.sent = []
        self.polls = 0
        self.closed = False

    def poll(self):
        self.polls += 1

    def send(self, commands):
        self.sent.append(list(commands))

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ({"level": "NORMAL", "transition": False}, ["LEVEL,NORMAL"]),
        ({"level": "NOTICE", "transition": True}, ["LEVEL,NOTICE"]),
        (
            {"level": "WARNING", "transition": True},
            ["LEVEL,WARNING", "ALERT,WARNING"],
        ),
        ({"level": "WARNING", "transition": False}, ["LEVEL,WARNING"]),
        (
            {"level": "BREAK", "transition": True},
            ["LEVEL,BREAK", "ALERT,BREAK"],
        ),
        ({"level": "BREAK", "transition": False}, ["LEVEL,BREAK"]),
        (
            {"level": "NORMAL", "reason": "ABSENT", "transition": True},
            ["LEVEL,OFF"],
        ),
    ],
)
def test_command_mapping(decision, expected):
    assert commands_for_decision(decision) == expected


def test_invalid_decision_is_rejected():
    with pytest.raises(ValueError):
        commands_for_decision({"level": "OFF", "transition": False})


def test_initial_open_failure_is_nonfatal_and_ready_resyncs_latest_level():
    clock = Clock()
    connected = FakeSerial()
    factory = SerialFactory([OSError("missing"), connected])
    transport = NanoSerialTransport(
        "COM-test",
        serial_factory=factory,
        monotonic=clock,
    )
    transport.send(["LEVEL,NOTICE"])

    transport.poll()
    assert transport.connected is False
    clock.advance(0.5)
    transport.poll()
    assert len(factory.calls) == 1
    clock.advance(0.5)
    transport.poll()
    assert transport.connected is True
    assert connected.writes == []

    connected.lines.extend([b"garbage\n", b"READY\n"])
    transport.poll()
    assert connected.writes == ["LEVEL,NOTICE"]


def test_write_failure_reconnect_replays_level_but_never_alert():
    clock = Clock()
    first = FakeSerial()
    second = FakeSerial()
    factory = SerialFactory([first, second])
    transport = NanoSerialTransport(
        "COM-test",
        serial_factory=factory,
        monotonic=clock,
    )
    transport.poll()
    first.lines.append(b"READY\n")
    transport.poll()
    transport.send(["LEVEL,WARNING", "ALERT,WARNING"])
    assert first.writes[-2:] == ["LEVEL,WARNING", "ALERT,WARNING"]

    first.fail_write = True
    transport.send(["LEVEL,BREAK", "ALERT,BREAK"])
    assert transport.connected is False
    assert transport.latest_level == "LEVEL,BREAK"

    clock.advance(1.0)
    transport.poll()
    second.lines.append(b"READY\n")
    transport.poll()
    assert second.writes == ["LEVEL,BREAK"]


def test_duplicate_level_is_safe_and_close_sends_off():
    serial_connection = FakeSerial()
    transport = NanoSerialTransport(
        "COM-test",
        serial_factory=SerialFactory([serial_connection]),
    )
    transport.poll()
    serial_connection.lines.append(b"READY\n")
    transport.poll()

    transport.send(["LEVEL,NORMAL"])
    transport.send(["LEVEL,NORMAL"])
    transport.close()

    assert serial_connection.writes[-3:] == [
        "LEVEL,NORMAL",
        "LEVEL,NORMAL",
        OFF_COMMAND,
    ]
    assert serial_connection.closed is True


def test_backend_failure_and_disconnect_retry_with_device_role_and_handle_events():
    clock = Clock()
    socket = FakeSocket([OSError("offline"), True, True])
    transport = RecordingTransport()
    bridge = NanoBridge(
        "http://backend",
        "shared-token",
        transport,
        socket,
        monotonic=clock,
        sleeper=lambda _seconds: None,
    )

    bridge.tick()
    assert transport.polls == 1
    assert socket.connected is False
    clock.advance(1.0)
    bridge.tick()
    assert socket.connected is True
    assert socket.connect_calls[-1][1]["auth"] == {
        "token": "shared-token",
        "role": "feedback_device",
    }

    socket.trigger(
        "feedback_device",
        {"level": "WARNING", "transition": True},
    )
    socket.trigger("feedback_device_off", {"v": 1})
    socket.trigger("feedback_device", {"level": "INVALID", "transition": True})
    assert transport.sent == [
        ["LEVEL,WARNING", "ALERT,WARNING"],
        [OFF_COMMAND],
    ]

    socket.connected = False
    socket.trigger("disconnect")
    bridge.tick()
    assert socket.connected is True
    assert len(socket.connect_calls) == 3

    bridge.close()
    assert transport.closed is True
    assert socket.disconnected is True
