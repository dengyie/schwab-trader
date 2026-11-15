"""
Single-user access control for the trading API.

The terminal is deployed on a public host, so every account-mutating or
account-reading endpoint must be authenticated. Three credentials are
accepted, in order of preference:

* an ``X-Auth-Token`` header carrying the ADMIN_TOKEN (scripts, prod_audit);
* an HttpOnly session cookie issued by ``POST /api/auth/login`` (the
  browser's normal path — the admin token never has to live in
  localStorage);
* a session token presented in the header (an API client that logged in
  once and kept the issued token).

The legacy WebSocket ``?token=`` query parameter is still honored.
"""
from __future__ import annotations

import collections
import hmac
import secrets
import threading
import time
from datetime import datetime, timezone
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

# ---------------------------------------------------------------------------
# Server-side sessions (2026-11-16): an HttpOnly cookie lets the browser
# authenticate without storing the admin token in localStorage. Sessions are
# persisted so a deploy does not force a re-login.
# ---------------------------------------------------------------------------
SESSION_COOKIE = "st_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600.0
SESSIONS_VERSION = 1

_sessions: Dict[str, float] = {}
_sessions_lock = threading.Lock()
_session_store = None  # JsonStateStore, wired from main (None = memory-only)


def init_session_store(store) -> None:
    """Attach the persisted session store and load any live sessions."""
    global _session_store
    _session_store = store
    if store is None:
        return
    snapshot = store.load()
    raw = (snapshot or {}).get("sessions") or {}
    now = time.time()
    with _sessions_lock:
        _sessions.clear()
        for token, expiry in raw.items():
            try:
                if float(expiry) > now:
                    _sessions[str(token)] = float(expiry)
            except (TypeError, ValueError):
                continue


def _persist_sessions_locked() -> None:
    if _session_store is None:
        return
    _session_store.save({
        "version": SESSIONS_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "sessions": dict(_sessions),
    })


def create_session() -> Tuple[str, datetime]:
    """Issue a session token; returns (token, expiry)."""
    token = secrets.token_urlsafe(32)
    expiry = time.time() + SESSION_TTL_SECONDS
    with _sessions_lock:
        now = time.time()
        for t in [t for t, exp in _sessions.items() if exp <= now]:
            _sessions.pop(t, None)
        _sessions[token] = expiry
        _persist_sessions_locked()
    return token, datetime.fromtimestamp(expiry, tz=timezone.utc)


def revoke_session(token: Optional[str]) -> bool:
    if not token:
        return False
    with _sessions_lock:
        existed = _sessions.pop(token, None) is not None
        if existed:
            _persist_sessions_locked()
    return existed


def session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    with _sessions_lock:
        expiry = _sessions.get(token)
    return bool(expiry and expiry > time.time())


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


def _credential_ok(request: Request, x_auth_token: str | None) -> bool:
    """Admin token (header), session cookie, or session token (header)."""
    if token_matches(x_auth_token):
        return True
    if session_valid(request.cookies.get(SESSION_COOKIE)):
        return True
    # A session token riding in the header: only valid when it is NOT equal
    # to the admin token (already checked above) — cheap dict probe.
    return session_valid(x_auth_token)


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

    if _credential_ok(request, x_auth_token):
        _clear_auth_failures(ip)
        return

    _record_auth_failure(ip)
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Token"},
    )
