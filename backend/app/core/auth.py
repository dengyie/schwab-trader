"""
Single-user access control for the trading API.

The terminal is deployed on a public host, so every account-mutating or
account-reading endpoint must be authenticated. The client stores the token in
localStorage and sends it as ``X-Auth-Token`` (REST) or ``?token=`` (WebSocket).
"""
from __future__ import annotations
import hmac
from fastapi import Header, HTTPException

from app.core.config import settings


def token_matches(provided: str | None) -> bool:
    """Constant-time comparison against the configured admin token."""
    expected = settings.admin_token
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def require_auth(x_auth_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency guarding every account-facing route."""
    if not settings.admin_token:
        # Fail closed: never expose trading endpoints when no token is configured.
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured on the server")
    if not token_matches(x_auth_token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Token"},
        )
