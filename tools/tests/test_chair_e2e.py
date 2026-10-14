"""Tests for the authenticated development-only Chair E2E observer."""

import base64
import json

import pytest

from tools.chair_e2e import (
    ChairE2EError,
    post_measurement,
    register_handlers,
    run_observer,
)
from tools.access_token import AccessTokenLifetimeError


TOKEN_NOW = 1_000.0


def _jwt(*, expires_at=10_000.0, subject="private-user-id"):
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expires_at, 'sub': subject})}.sig"


VALID_TOKEN = _jwt()


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

    def connect(self, url, auth, transports=None):
        self.connected = True
        self.connect_auth = (url, auth, transports)

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
        VALID_TOKEN,
        socket_client=client,
        post=post,
        wait=interrupt,
        write=lines.append,
        token_now=lambda: TOKEN_NOW,
    )

    assert client.connect_auth == (
        "http://127.0.0.1:5000",
        {"token": VALID_TOKEN},
        ["polling"],
    )
    assert calls == ["/api/measurement/start", "/api/measurement/stop"]
    assert client.disconnected is True
    assert all(VALID_TOKEN not in line for line in lines)


def test_failed_start_does_not_issue_stop():
    client = FakeSocket()
    calls = []

    def post(_url, endpoint, _token):
        calls.append(endpoint)
        raise ChairE2EError("start rejected")

    with pytest.raises(ChairE2EError, match="start rejected"):
        run_observer(
            "http://127.0.0.1:5000",
            VALID_TOKEN,
            socket_client=client,
            post=post,
            token_now=lambda: TOKEN_NOW,
        )

    assert calls == ["/api/measurement/start"]
    assert client.disconnected is True


def test_completed_observation_without_state_explains_stdout_transport_mode():
    client = FakeSocket()
    lines = []

    def post(_url, endpoint, _token):
        return {"status": "success", "endpoint": endpoint}

    run_observer(
        "http://127.0.0.1:5000",
        VALID_TOKEN,
        duration_sec=60,
        socket_client=client,
        post=post,
        wait=lambda _client, _duration: None,
        write=lines.append,
        token_now=lambda: TOKEN_NOW,
    )

    assert any("no state received" in line for line in lines)
    assert any("without --stdout" in line for line in lines)
    assert all(VALID_TOKEN not in line for line in lines)


def test_failed_stop_disconnects_authenticated_socket_without_exposing_token():
    client = FakeSocket()
    lines = []

    def post(_url, endpoint, _token):
        if endpoint.endswith("/stop"):
            raise ChairE2EError("HTTP 401 (invalid_token)")
        return {"status": "success"}

    with pytest.raises(ChairE2EError, match="invalid_token"):
        run_observer(
            "http://127.0.0.1:5000",
            VALID_TOKEN,
            duration_sec=1,
            socket_client=client,
            post=post,
            wait=lambda _client, _duration: None,
            write=lines.append,
            token_now=lambda: TOKEN_NOW,
        )

    assert client.disconnected is True
    assert any("Backend can release the session" in line for line in lines)
    assert all(VALID_TOKEN not in line for line in lines)


def test_lifetime_preflight_rejects_before_socket_or_measurement_start():
    client = FakeSocket()
    calls = []
    token = _jwt(expires_at=TOKEN_NOW + 299, subject="must-not-leak")

    with pytest.raises(AccessTokenLifetimeError) as captured:
        run_observer(
            "http://127.0.0.1:5000",
            token,
            socket_client=client,
            post=lambda *_args: calls.append("post"),
            token_now=lambda: TOKEN_NOW,
        )

    message = str(captured.value)
    assert "required at least 300s" in message
    assert "remaining 299s" in message
    assert token not in message
    assert "must-not-leak" not in message
    assert client.connect_auth is None
    assert calls == []
