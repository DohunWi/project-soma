"""Credential-safe access-token lifetime preflight for development CLIs.

Payload decoding is informational only. The Backend remains authoritative for
signature, issuer, audience, subject, and expiration validation.
"""

from __future__ import annotations

import base64
import json
import math
import time
from dataclasses import dataclass


FRESH_TOKEN_INSTRUCTION = (
    "copy a fresh access token from the logged-in Front session"
)


class AccessTokenLifetimeError(ValueError):
    """The token cannot safely cover the requested development run."""


@dataclass(frozen=True)
class AccessTokenTiming:
    expires_at: float
    issued_at: float | None
    remaining_sec: float


def inspect_access_token_timing(token, *, now=time.time):
    """Read timing claims without authenticating or exposing token identity."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            raise ValueError
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + padding).decode("utf-8")
        )
        expires_at = _numeric_claim(payload, "exp", required=True)
        issued_at = _numeric_claim(payload, "iat", required=False)
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError,
            json.JSONDecodeError) as error:
        raise AccessTokenLifetimeError(
            "Supabase access token is not a well-formed JWT; "
            f"{FRESH_TOKEN_INSTRUCTION}."
        ) from error

    return AccessTokenTiming(
        expires_at=expires_at,
        issued_at=issued_at,
        remaining_sec=expires_at - float(now()),
    )


def require_access_token_lifetime(token, required_sec, *, now=time.time):
    """Reject a token that cannot cover a run; never authenticates the token."""
    required_sec = float(required_sec)
    if not math.isfinite(required_sec) or required_sec <= 0:
        raise ValueError("required_sec must be a positive finite number")

    timing = inspect_access_token_timing(token, now=now)
    remaining_sec = timing.remaining_sec
    if remaining_sec <= 0:
        description = "has expired"
    elif remaining_sec < required_sec:
        description = "has insufficient remaining lifetime"
    else:
        return timing

    raise AccessTokenLifetimeError(
        "Supabase access token "
        f"{description}: required at least {math.ceil(required_sec)}s, "
        f"remaining {max(math.floor(remaining_sec), 0)}s; "
        f"{FRESH_TOKEN_INSTRUCTION}."
    )


def _numeric_claim(payload, name, *, required):
    if not isinstance(payload, dict):
        raise ValueError
    value = payload.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number
