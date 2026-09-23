"""Finite mock streams close Socket.IO only after their last sample emit."""
import sys
from types import SimpleNamespace

from tools.mock import stream


def test_finite_socket_stream_disconnects_after_last_emit(monkeypatch):
    actions = []

    class FakeClient:
        connected = False

        def connect(self, url, auth=None):
            self.connected = True
            actions.append(("connect", url, auth))

        def emit(self, event, payload):
            actions.append(("emit", event, payload))

        def disconnect(self):
            actions.append(("disconnect",))
            self.connected = False

    client = FakeClient()
    monkeypatch.setitem(
        sys.modules,
        "socketio",
        SimpleNamespace(Client=lambda: client),
    )
    monkeypatch.setenv("SOCKET_AUTH_TOKEN", "sensor-token")
    monkeypatch.setattr(stream.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stream.py",
            "--source",
            "chair",
            "--scenario",
            "normal",
            "--duration",
            "1",
        ],
    )

    stream.main()

    assert [action[0] for action in actions] == ["connect", "emit", "disconnect"]
    assert actions[0][2] == {"token": "sensor-token"}
    assert actions[1][1] == "sensor_data"
    assert actions[1][2]["source"] == "chair"
    assert client.connected is False
