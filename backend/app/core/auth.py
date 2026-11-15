"""
Single-user access control for the trading API.

The terminal is deployed on a public host, so every account-mutating or
account-reading endpoint must be authenticated. The client stores the token in
localStorage and sends it as ``X-Auth-Token`` (REST) or an ``AUTH`` frame
(WebSocket); the legacy ``?token=`` query parameter is still honored.
"""
from __future__ import annotations

import collections
import hmac
import threading
import time
from typing import Deque, Dict, Optional, Tuple

from fastapi import Header, HTTPException, Request

from app.core.config import settings

# Token-guess throttling. The comparison is constant-time, but without a
# failure budget an attacker can still probe the token at wire speed forever.
# Sliding window per client IP: after AUTH_FAIL_LIMIT failures inside
# AUTH_FAIL_WINDOW seconds, that IP gets 429 until the window drains. The
# legitimate user gets 8 wrong tokens per minute — far above any realistic
# mistype rate — while brute force collapses to 8 probes/minute.
AUTH_FAIL_WINDOW = 60.0
AUTH_FAIL_LIMIT = 8
AUTH_PENALTY_IPS = 1024  # bound the ip table against address spoofing

_auth_failures: Dict[str, Deque[float]] = {}
_auth_failures_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # The service binds 127.0.0.1 behind Cloudflare, so the connecting socket
    # is the tunnel, not the visitor; CF's injected header is the real source
    # and only exists for traffic CF actually proxied.
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


def _failures_in_window(ip: str, now: float) -> int:
    dq = _auth_failures.get(ip)
    if not dq:
        return 0
    while dq and now - dq[0] > AUTH_FAIL_WINDOW:
        dq.popleft()
    return len(dq)


def _record_auth_failure(ip: str) -> None:
    with _auth_failures_lock:
        dq = _auth_failures.setdefault(ip, collections.deque())
        dq.append(time.time())
        # Keep the table bounded: an attacker spoofing many IPs (each behind
        # CF, so this is mostly theoretical) must not grow it without end.
        if len(_auth_failures) > AUTH_PENALTY_IPS:
            oldest = min(_auth_failures, key=lambda k: _auth_failures[k][-1] if _auth_failures[k] else 0.0)
            _auth_failures.pop(oldest, None)


def _clear_auth_failures(ip: str) -> None:
    with _auth_failures_lock:
        dq = _auth_failures.get(ip)
        if dq:
            dq.clear()


def token_matches(provided: str | None) -> bool:
    """Constant-time comparison against the configured admin token."""
    expected = settings.admin_token
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def require_auth(request: Request, x_auth_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency guarding every account-facing route."""
    ip = _client_ip(request)
    now = time.time()
    with _auth_failures_lock:
        recent = _failures_in_window(ip, now)
    if recent >= AUTH_FAIL_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many failed auth attempts; retry later",
            headers={"Retry-After": str(int(AUTH_FAIL_WINDOW))},
        )

    if not settings.admin_token:
        # Fail closed: never expose trading endpoints when no token is configured.
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured on the server")
    if not token_matches(x_auth_token):
        _record_auth_failure(ip)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Token"},
        )
    _clear_auth_failures(ip)
