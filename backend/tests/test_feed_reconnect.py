"""Reconnect policy must escalate, not hammer.

On 2026-11-10 production logged 86 disconnects in a few minutes, every 11-12
seconds, each "reconnecting in 1.0s". Two processes sharing one Alpaca key
evict each other, and the old loop reset the backoff on every successful
``connect`` — so an eviction ping-pong reconnected at one second forever, hid
the cause, and hammered the endpoint. A connection only earns a clean slate
once it has actually stayed up.
"""
from __future__ import annotations

import asyncio
import types

import websockets

from app.engine import alpaca_market_feed as feed_module
from app.engine.alpaca_market_feed import (
    INITIAL_RECONNECT_BACKOFF,
    MAX_RECONNECT_BACKOFF,
    STABLE_CONNECTION_SECONDS,
    AlpacaMarketFeedService,
    next_reconnect_backoff,
)


def test_short_lived_connections_keep_escalating():
    delay = INITIAL_RECONNECT_BACKOFF
    seen = []
    for _ in range(6):
        delay = next_reconnect_backoff(delay, connected_seconds=0.0)
        seen.append(delay)
    assert seen == [1.5, 2.25, 3.375, 5.0625, 7.59375, 11.390625]


def test_backoff_is_capped():
    delay = INITIAL_RECONNECT_BACKOFF
    for _ in range(50):
        delay = next_reconnect_backoff(delay, connected_seconds=0.0)
    assert delay == MAX_RECONNECT_BACKOFF


def test_a_stable_connection_earns_a_clean_slate():
    assert next_reconnect_backoff(MAX_RECONNECT_BACKOFF, connected_seconds=STABLE_CONNECTION_SECONDS) == (
        INITIAL_RECONNECT_BACKOFF
    )
    # Just under the bar still escalates.
    assert next_reconnect_backoff(MAX_RECONNECT_BACKOFF, connected_seconds=STABLE_CONNECTION_SECONDS - 1) == (
        MAX_RECONNECT_BACKOFF
    )


def test_listener_loop_does_not_reset_backoff_on_every_connect(monkeypatch):
    """The regression itself: a stream that connects then drops must back off."""

    class DroppingWS:
        async def send(self, *args, **kwargs):
            return None

        async def recv(self):
            raise websockets.exceptions.ConnectionClosed(None, None)

    class ConnectCM:
        async def __aenter__(self):
            return DroppingWS()

        async def __aexit__(self, *exc):
            return False

    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed._running = True
    feed.stream_url = "wss://example.invalid/stream"
    feed.api_key = "key"
    feed.api_secret = "secret"
    feed.symbols = {"SPY"}

    monkeypatch.setattr(feed_module.websockets, "connect", lambda *a, **k: ConnectCM())

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 4:
            feed._running = False

    monkeypatch.setattr(feed_module.asyncio, "sleep", fake_sleep)
    asyncio.run(feed._websocket_listener_loop())

    # Old behaviour logged 1.0 every time; now each drop escalates.
    assert delays == [1.0, 1.5, 2.25, 3.375]


def test_listener_loop_survives_an_auth_rejection(monkeypatch):
    """A rejected key raises, and the loop must back off rather than spin."""

    class RejectingWS:
        def __init__(self):
            self.sent = 0

        async def send(self, *args, **kwargs):
            return None

        async def recv(self):
            self.sent += 1
            if self.sent == 1:
                return '[{"T":"success","msg":"connected"}]'
            return '[{"T":"error","code":402,"msg":"auth failed"}]'

    class ConnectCM:
        async def __aenter__(self):
            return RejectingWS()

        async def __aexit__(self, *exc):
            return False

    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed._running = True
    feed.stream_url = "wss://example.invalid/stream"
    feed.api_key = "key"
    feed.api_secret = "secret"
    feed.symbols = {"SPY"}

    calls = types.SimpleNamespace(count=0)
    monkeypatch.setattr(feed_module.websockets, "connect", lambda *a, **k: ConnectCM())

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        calls.count += 1
        if len(delays) >= 2:
            feed._running = False

    monkeypatch.setattr(feed_module.asyncio, "sleep", fake_sleep)
    asyncio.run(feed._websocket_listener_loop())

    assert calls.count == 2
    assert delays == [1.0, 1.5]
