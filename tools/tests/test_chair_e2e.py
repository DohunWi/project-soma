"""Tests for the authenticated development-only Chair E2E observer."""

import json

import pytest

from tools.chair_e2e import (
    ChairE2EError,
    post_measurement,
    register_handlers,
    run_observer,
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


class FakeSocket:
    def __init__(self):
        self.connected = False
        self.handlers = {}
        self.connect_auth = None
        self.disconnected = False

    def on(self, name, handler):
        self.handlers[name] = handler

    def connect(self, url, auth):
        self.connected = True
        self.connect_auth = (url, auth)

    def disconnect(self):
        self.connected = False
        self.disconnected = True


def test_measurement_request_uses_bearer_token_without_putting_it_in_url():
    observed = {}

    def opener(request, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.get_header("Authorization")
        observed["timeout"] = timeout
        return FakeResponse(201, {"status": "success"})

    payload = post_measurement(
        "http://127.0.0.1:5000",
        "/api/measurement/start",
        "secret-token",
        opener=opener,
    )

    assert payload == {"status": "success"}
    assert observed == {
        "url": "http://127.0.0.1:5000/api/measurement/start",
        "authorization": "Bearer secret-token",
        "timeout": 10,
    }
    assert "secret-token" not in observed["url"]


def test_state_and_feedback_handlers_print_only_event_payloads():
    client = FakeSocket()
    lines = []
    register_handlers(client, lines.append)

    client.handlers["state"]({"state": "NORMAL"})
    client.handlers["feedback"]({"level": "NOTICE"})

    assert lines == [
        '[state] {"state": "NORMAL"}',
        '[feedback] {"level": "NOTICE"}',
    ]


def test_observer_stops_measurement_and_disconnects_on_interrupt():
    client = FakeSocket()
    calls = []
    lines = []

    def post(_url, endpoint, _token):
        calls.append(endpoint)
        return {"status": "success", "endpoint": endpoint}

    def interrupt(_client, _duration):
        raise KeyboardInterrupt

    run_observer(
        "http://127.0.0.1:5000",
        "secret-token",
        socket_client=client,
        post=post,
        wait=interrupt,
        write=lines.append,
    )

    assert client.connect_auth == (
        "http://127.0.0.1:5000",
        {"token": "secret-token"},
    )
    assert calls == ["/api/measurement/start", "/api/measurement/stop"]
    assert client.disconnected is True
    assert all("secret-token" not in line for line in lines)


def test_failed_start_does_not_issue_stop():
    client = FakeSocket()
    calls = []

    def post(_url, endpoint, _token):
        calls.append(endpoint)
        raise ChairE2EError("start rejected")

    with pytest.raises(ChairE2EError, match="start rejected"):
        run_observer(
            "http://127.0.0.1:5000",
            "secret-token",
            socket_client=client,
            post=post,
        )

    assert calls == ["/api/measurement/start"]
    assert client.disconnected is True
