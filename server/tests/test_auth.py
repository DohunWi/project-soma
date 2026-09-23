"""Supabase access token verification tests without network access."""
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.auth import (  # noqa: E402
    AuthenticationError,
    AuthenticationUnavailable,
    SupabaseAuthVerifier,
    bearer_token,
)

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class FakeAuthApi:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def get_claims(self, _token):
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.auth = FakeAuthApi(response, error)


def claims(**updates):
    result = {
        "iss": "https://project.supabase.co/auth/v1",
        "aud": "authenticated",
        "role": "authenticated",
        "exp": 2000,
        "sub": str(USER_ID),
    }
    result.update(updates)
    return {"claims": result}


def verifier(response=None, error=None):
    return SupabaseAuthVerifier(
        url="https://project.supabase.co",
        publishable_key="test-publishable-key",
        client=FakeClient(response, error),
        now=lambda: 1000,
    )


def test_verified_sub_becomes_uuid_identity():
    assert verifier(claims()).verify("valid-token").user_id == USER_ID


@pytest.mark.parametrize(
    "response",
    [
        claims(iss="https://other.supabase.co/auth/v1"),
        claims(aud="anon"),
        claims(role="anon"),
        claims(exp=1000),
        claims(sub="not-a-uuid"),
        {"claims": {}},
    ],
)
def test_untrusted_claims_are_rejected(response):
    with pytest.raises(AuthenticationError):
        verifier(response).verify("untrusted-token")


def test_missing_configuration_is_auth_unavailable():
    auth = SupabaseAuthVerifier(url="", publishable_key="")
    with pytest.raises(AuthenticationUnavailable):
        auth.verify("token")


@pytest.mark.parametrize("header", [None, "", "Basic token", "Bearer ", "Bearer a b"])
def test_bearer_token_rejects_invalid_header(header):
    with pytest.raises(AuthenticationError):
        bearer_token(header)


def test_bearer_token_extracts_value_without_logging_or_decoding_it():
    assert bearer_token("Bearer secret-value") == "secret-value"
