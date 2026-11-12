"""FastAPI authentication dependencies (Supabase JWT) and webhook secret verification."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.core.config import Settings, get_settings
from backend.core.exceptions import AuthenticationError, ConfigurationError
from backend.core.logging import get_logger
from backend.services.supabase.client import get_supabase_client

logger = get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)
_PLACEHOLDER_JWT_SECRETS = {"", "your_supabase_jwt_secret_here"}
_HMAC_ALGORITHMS = {"HS256", "HS384", "HS512"}


class CurrentUser(BaseModel):
    """Authenticated user extracted from a verified Supabase JWT."""

    id: str
    email: str | None = None
    role: str = "authenticated"
    raw_claims: dict[str, Any] = Field(default_factory=dict)


def _claims_to_user(claims: dict[str, Any]) -> CurrentUser:
    user_id = claims.get("sub")
    if not user_id:
        raise AuthenticationError("Token is missing subject claim")
    return CurrentUser(
        id=str(user_id),
        email=claims.get("email"),
        role=str(claims.get("role") or "authenticated"),
        raw_claims=claims,
    )


def _decode_hmac_jwt(token: str, jwt_secret: str) -> CurrentUser:
    try:
        claims = jwt.decode(
            token,
            jwt_secret,
            algorithms=list(_HMAC_ALGORITHMS),
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Authentication token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token") from exc
    return _claims_to_user(claims)


async def _verify_via_supabase_auth(token: str, settings: Settings) -> CurrentUser:
    try:
        client = get_supabase_client(settings)
        response = await asyncio.to_thread(client.auth.get_user, token)
    except ConfigurationError:
        raise
    except Exception as exc:
        logger.warning("Supabase auth.get_user verification failed")
        raise AuthenticationError("Invalid or expired authentication token") from exc

    user = getattr(response, "user", None)
    if user is None and isinstance(response, dict):
        user = response.get("user")

    if user is None:
        raise AuthenticationError("Invalid or expired authentication token")

    user_id = getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else None)
    if not user_id:
        raise AuthenticationError("Supabase user record missing id")

    email = getattr(user, "email", None) or (user.get("email") if isinstance(user, dict) else None)
    role = getattr(user, "role", None) or (
        user.get("role") if isinstance(user, dict) else "authenticated"
    )
    return CurrentUser(id=str(user_id), email=email, role=str(role or "authenticated"))


def _sandbox_signing_key(settings: Settings) -> bytes:
    secret = (settings.SECRET_KEY or "").strip() or "local-sandbox-dev-secret"
    return secret.encode("utf-8")


def issue_sandbox_account_token(
    user_id: str | UUID,
    email: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Issue an HMAC-SHA256 signed local sandbox account token."""
    cfg = settings or get_settings()
    payload = {
        "uid": str(user_id),
        "email": (email or "").strip().lower() or None,
        "sandbox": True,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    sig = hmac.new(_sandbox_signing_key(cfg), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"sandbox-acct:{payload_b64}.{sig}"


def verify_sandbox_account_token(
    token: str,
    settings: Settings | None = None,
) -> CurrentUser:
    """Verify an HMAC-signed ``sandbox-acct:<payload>.<sig>`` token when sandbox mode is enabled."""
    cfg = settings or get_settings()
    if not cfg.LOCAL_SANDBOX_MODE:
        raise AuthenticationError("Local sandbox mode is disabled")

    clean = (token or "").strip()
    if not clean.startswith("sandbox-acct:"):
        raise AuthenticationError("Invalid sandbox account token prefix")

    body = clean[len("sandbox-acct:") :]
    if "." not in body:
        raise AuthenticationError("Malformed sandbox account token")

    payload_b64, sig = body.split(".", 1)
    if not payload_b64 or not sig:
        raise AuthenticationError("Malformed sandbox account token")

    expected_sig = hmac.new(
        _sandbox_signing_key(cfg),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise AuthenticationError("Invalid sandbox account token signature")

    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw_bytes = base64.urlsafe_b64decode((payload_b64 + padding).encode("ascii"))
        claims = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise AuthenticationError("Corrupted sandbox account token payload") from exc

    if not isinstance(claims, dict) or not claims.get("uid"):
        raise AuthenticationError("Sandbox account token is missing user id")

    return CurrentUser(
        id=str(claims["uid"]),
        email=claims.get("email"),
        role="authenticated",
        raw_claims={"sandbox": True},
    )


async def decode_supabase_jwt(token: str, settings: Settings | None = None) -> CurrentUser:
    """Verify a Supabase JWT via HS256 secret or Supabase auth.get_user fallback."""
    if not token or not token.strip():
        raise AuthenticationError("Missing authentication token")

    cfg = settings or get_settings()
    clean_token = token.strip()

    if cfg.LOCAL_SANDBOX_MODE:
        if clean_token.startswith("sandbox-acct:"):
            return verify_sandbox_account_token(clean_token, settings=cfg)
        if clean_token == "sandbox-demo-token" or clean_token.startswith("sandbox:"):
            return CurrentUser(
                id="00000000-0000-0000-0000-000000000001",
                email="demo@local.studio",
                role="authenticated",
                raw_claims={"sandbox": True},
            )

    try:
        header = jwt.get_unverified_header(clean_token)
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Malformed authentication token") from exc

    alg = str(header.get("alg") or "").upper()
    jwt_secret = (cfg.SUPABASE_JWT_SECRET or "").strip()

    if alg in _HMAC_ALGORITHMS and jwt_secret not in _PLACEHOLDER_JWT_SECRETS:
        return _decode_hmac_jwt(clean_token, jwt_secret)

    # Asymmetric tokens (e.g., ES256/RS256) or environments without local JWT secret
    # fall back to Supabase Auth verification.
    return await _verify_via_supabase_auth(clean_token, cfg)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """FastAPI dependency requiring a valid Bearer Supabase JWT."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authorization Bearer token is required")
    return await decode_supabase_jwt(credentials.credentials, settings=settings)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> CurrentUser | None:
    """FastAPI dependency returning CurrentUser if Bearer token is provided, else None."""
    if credentials is None or not credentials.credentials:
        return None
    return await decode_supabase_jwt(credentials.credentials, settings=settings)


def _strip_auth_prefix(secret_value: str) -> str:
    stripped = secret_value.strip()
    lower = stripped.lower()
    for prefix in ("bearer ", "apikey "):
        if lower.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def verify_webhook_secret(provided_secret: str | None, expected_secret: str | None) -> bool:
    """Constant-time verification for SePay / Casso webhook authorization secrets."""
    if not provided_secret or not expected_secret:
        return False
    clean_expected = expected_secret.strip()
    if not clean_expected or clean_expected.startswith("your_"):
        return False
    clean_provided = _strip_auth_prefix(provided_secret)
    if not clean_provided:
        return False
    return hmac.compare_digest(clean_provided, clean_expected)


def assert_webhook_secret(
    provided_secret: str | None,
    expected_secret: str | None,
    *,
    gateway: str = "payment",
) -> None:
    """Verify webhook secret or raise ConfigurationError / AuthenticationError."""
    clean_expected = (expected_secret or "").strip()
    if not clean_expected or clean_expected.startswith("your_"):
        raise ConfigurationError(f"Webhook secret for '{gateway}' is not configured")
    if not verify_webhook_secret(provided_secret, clean_expected):
        logger.warning("Rejected webhook request for gateway=%s due to invalid secret", gateway)
        raise AuthenticationError(f"Invalid webhook secret for '{gateway}'")
