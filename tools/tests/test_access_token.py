"""Credential-safe JWT timing preflight tests."""

import base64
import json

import pytest

from tools.access_token import (
    AccessTokenLifetimeError,
    inspect_access_token_timing,
    require_access_token_lifetime,
)


NOW = 1_000.0


def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def test_valid_jwt_with_sufficient_remaining_lifetime():
    token = jwt({"exp": 2_000, "iat": 900, "sub": "private-user-id"})

    timing = require_access_token_lifetime(
        token,
        300,
        now=lambda: NOW,
    )

    assert timing.expires_at == 2_000
    assert timing.issued_at == 900
    assert timing.remaining_sec == 1_000


@pytest.mark.parametrize(
    ("expires_at", "message"),
    [
        (999, "has expired"),
        (1_299, "has insufficient remaining lifetime"),
    ],
)
def test_expired_or_insufficient_jwt_is_rejected_without_leakage(
    expires_at,
    message,
):
    token = jwt({"exp": expires_at, "sub": "private-user-id"})

    with pytest.raises(AccessTokenLifetimeError) as captured:
        require_access_token_lifetime(token, 300, now=lambda: NOW)

    rendered = str(captured.value)
    assert message in rendered
    assert token not in rendered
    assert "private-user-id" not in rendered
    assert "copy a fresh access token" in rendered


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "header.invalid-base64.signature",
        jwt({"sub": "private-user-id"}),
        jwt({"exp": "not-a-number", "sub": "private-user-id"}),
    ],
)
def test_malformed_jwt_is_rejected_without_token_or_subject(token):
    with pytest.raises(AccessTokenLifetimeError) as captured:
        inspect_access_token_timing(token, now=lambda: NOW)

    rendered = str(captured.value)
    assert "not a well-formed JWT" in rendered
    assert token not in rendered
    assert "private-user-id" not in rendered
