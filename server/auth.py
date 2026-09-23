"""Verify Supabase access tokens without trusting client identity fields."""
import os
import time
import uuid
from dataclasses import dataclass


class AuthenticationError(ValueError):
    """The supplied credential is missing, malformed, or not trustworthy."""


class AuthenticationUnavailable(RuntimeError):
    """The configured Supabase Auth service cannot currently verify tokens."""


@dataclass(frozen=True)
class AuthIdentity:
    """Identity derived only from a verified Supabase access token."""

    user_id: uuid.UUID


class SupabaseAuthVerifier:
    """Lazily create a Supabase client and verify JWT claims through its SDK."""

    def __init__(self, *, url=None, publishable_key=None, client=None, now=time.time):
        self._url = url if url is not None else os.getenv("SUPABASE_URL")
        self._publishable_key = (
            publishable_key
            if publishable_key is not None
            else os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_KEY")
        )
        self._client = client
        self._now = now

    def verify(self, token):
        """Return a UUID identity after signature and standard claim validation."""
        if not isinstance(token, str) or not token:
            raise AuthenticationError("invalid access token")
        client = self._get_client()
        try:
            response = client.auth.get_claims(token)
        except Exception as error:  # SDK exception types differ between releases.
            if _is_transport_error(error):
                raise AuthenticationUnavailable("auth service unavailable") from error
            raise AuthenticationError("invalid access token") from error

        claims = _claims_dict(response)
        expected_issuer = f"{self._url.rstrip('/')}/auth/v1"
        if claims.get("iss") != expected_issuer:
            raise AuthenticationError("invalid token issuer")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if "authenticated" not in audiences or claims.get("role") != "authenticated":
            raise AuthenticationError("invalid token audience")
        try:
            if float(claims["exp"]) <= float(self._now()):
                raise AuthenticationError("expired access token")
            user_id = uuid.UUID(str(claims["sub"]))
        except (KeyError, TypeError, ValueError) as error:
            raise AuthenticationError("invalid token subject") from error
        return AuthIdentity(user_id=user_id)

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._url or not self._publishable_key:
            raise AuthenticationUnavailable("Supabase Auth is not configured")
        try:
            from supabase import create_client
        except ImportError as error:
            raise AuthenticationUnavailable("Supabase SDK is not installed") from error
        try:
            self._client = create_client(self._url, self._publishable_key)
        except Exception as error:  # Client setup may validate URL/configuration.
            raise AuthenticationUnavailable("Supabase Auth setup failed") from error
        return self._client


def bearer_token(header):
    """Extract one Bearer token without returning it in any error message."""
    if not isinstance(header, str) or not header.startswith("Bearer "):
        raise AuthenticationError("missing bearer token")
    token = header[7:].strip()
    if not token or " " in token:
        raise AuthenticationError("invalid bearer token")
    return token


def _claims_dict(response):
    claims = response.get("claims") if isinstance(response, dict) else getattr(
        response, "claims", None
    )
    if claims is None:
        raise AuthenticationError("verified claims are missing")
    if isinstance(claims, dict):
        return claims
    if hasattr(claims, "model_dump"):
        return claims.model_dump()
    if hasattr(claims, "dict"):
        return claims.dict()
    raise AuthenticationError("verified claims are invalid")


def _is_transport_error(error):
    """Classify network failures without importing an optional HTTP client."""
    return error.__class__.__module__.startswith(("httpx", "httpcore"))
