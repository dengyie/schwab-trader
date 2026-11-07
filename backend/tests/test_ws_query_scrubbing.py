"""The legacy ?token= must never reach the access log.

uvicorn logs the raw query string of every handshake, so the middleware moves
the token into a scoped key that only the endpoint reads.
"""
import asyncio

from app.main import LEGACY_TOKEN_KEY, LegacyQueryTokenMiddleware


def run_middleware(scope):
    captured = {}

    async def inner(inner_scope, receive, send):
        captured["scope"] = inner_scope

    middleware = LegacyQueryTokenMiddleware(inner)
    asyncio.run(middleware(scope, None, None))
    return captured["scope"]


def test_token_is_moved_out_of_the_logged_query_string():
    scope = {
        "type": "websocket",
        "path": "/ws",
        "query_string": b"token=secret&symbol=SPY",
    }

    forwarded = run_middleware(scope)

    # What uvicorn logs no longer contains the credential.
    assert forwarded["query_string"] == b"symbol=SPY"
    # The endpoint can still read it.
    assert forwarded["state"][LEGACY_TOKEN_KEY] == "secret"
    # The caller's scope is left untouched.
    assert scope["query_string"] == b"token=secret&symbol=SPY"


def test_scope_without_token_is_passed_through():
    scope = {"type": "websocket", "path": "/ws", "query_string": b"symbol=SPY"}

    forwarded = run_middleware(scope)

    assert forwarded is scope
    assert forwarded["query_string"] == b"symbol=SPY"


def test_http_scopes_are_ignored():
    scope = {"type": "http", "path": "/api/health", "query_string": b"token=secret"}

    forwarded = run_middleware(scope)

    assert forwarded is scope
    assert forwarded["query_string"] == b"token=secret"
