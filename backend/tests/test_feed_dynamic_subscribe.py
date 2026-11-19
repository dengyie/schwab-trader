"""Runtime stream subscriptions: WS SUBSCRIBE must reach the live IEX socket.

Anthropic listed on 2026-11-19 as ``ANT``; the IEX stream carried real ticks,
but the subscription set was fixed at boot (``default_symbols``) and the
``MultiMarketFeed.activate`` hasattr-guard silently skipped the Alpaca feed —
so a new symbol could never get a live tick. The feed now queues runtime
changes and the listener loop forwards them as incremental
subscribe/unsubscribe frames without waiting for a reconnect.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.engine import alpaca_market_feed as feed_module
from app.engine.alpaca_market_feed import AlpacaMarketFeedService, MAX_DYNAMIC_SYMBOLS


def _loop_feed() -> AlpacaMarketFeedService:
    """Loop-test feed via ``__new__`` (no preload, no network), mirroring
    tests/test_feed_reconnect.py; adds the fields activate() touches."""
    import threading

    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed._running = True
    feed.stream_url = "wss://example.invalid/stream"
    feed.api_key = "key"
    feed.api_secret = "secret"
    feed.symbols = {"SPY", "QQQ"}
    feed._core_symbols = frozenset(feed.symbols)
    feed._sub_control = None
    feed._sub_control_lock = threading.Lock()
    feed._sub_control_pending = []
    feed._daily_warm_tasks = set()
    # activate() warms daily stats in the background; keep it inert in tests.
    feed.ensure_daily_stats = lambda *a, **k: None
    feed.feed = "iex"
    feed._stream_state = "idle"
    feed._stream_since = 0.0
    feed._stream_last_error = None
    feed._stream_failures = 0
    feed._stream_quota = False
    return feed


def test_activate_adds_symbol_and_queues_incremental_frame():
    feed = _loop_feed()
    feed._sub_control_lock = __import__("threading").Lock()

    feed.activate("ANT")
    assert "ANT" in feed.symbols
    assert feed._sub_control_pending == [("add", "ANT")]

    # Lowercase normalises through resolve_symbol.
    feed.activate("amd")
    assert "AMD" in feed.symbols

    # Core symbols are idempotent no-ops.
    before = list(feed._sub_control_pending)
    feed.activate("SPY")
    assert feed._sub_control_pending == before


def test_deactivate_removes_runtime_symbol_but_never_core():
    feed = _loop_feed()
    import threading
    feed._sub_control_lock = threading.Lock()

    feed.activate("ANT")
    feed.deactivate("ANT")
    assert "ANT" not in feed.symbols
    assert feed._sub_control_pending == [("add", "ANT"), ("remove", "ANT")]

    # A stray client must not kick SPY off the stream.
    feed.deactivate("SPY")
    assert "SPY" in feed.symbols
    assert len(feed._sub_control_pending) == 2

    # Unknown symbol: nothing to remove.
    feed.deactivate("NOPE")
    assert len(feed._sub_control_pending) == 2


def test_activate_rejects_over_dynamic_cap():
    import string
    import threading
    feed = _loop_feed()
    feed._sub_control_lock = threading.Lock()

    def sym(i: int) -> str:
        return "T" + string.ascii_uppercase[i % 26] + str(i)

    for i in range(MAX_DYNAMIC_SYMBOLS):
        feed.activate(sym(i))
    queued = len(feed._sub_control_pending)

    feed.activate("OVER1")
    feed.activate("OVER2")
    assert len(feed._sub_control_pending) == queued
    assert "OVER1" not in feed.symbols


def test_listener_loop_forwards_incremental_frames(monkeypatch):
    """The loop must drain the control queue and send incremental frames on
    the live socket, interleaved with IEX ticks."""

    sent: list[str] = []

    class ScriptedWS:
        """Answers the 3-frame handshake, then blocks on recv; records sends."""

        def __init__(self):
            self.handshakes = 0
            self.gate = asyncio.Event()

        async def send(self, payload):
            sent.append(payload)

        async def recv(self):
            self.handshakes += 1
            if self.handshakes == 1:
                return '[{"T":"success","msg":"connected"}]'
            if self.handshakes == 2:
                return '[{"T":"success","msg":"authenticated"}]'
            if self.handshakes == 3:
                return '[{"T":"success","msg":"subscribed"}]'
            await self.gate.wait()
            raise websockets.exceptions.ConnectionClosed(None, None)

    class ConnectCM:
        async def __aenter__(self):
            return ScriptedWS()

        async def __aexit__(self, *exc):
            return False

    import websockets

    feed = _loop_feed()
    feed.activate("ANT")  # queued pre-loop, folded into the boot subscribe
    monkeypatch.setattr(feed_module.websockets, "connect", lambda *a, **k: ConnectCM())

    async def main():
        task = asyncio.ensure_future(feed._websocket_listener_loop())
        await asyncio.sleep(0.3)  # let auth + boot subscribe land
        feed.activate("PLTR")     # runtime change after connect
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())

    # sent[0]=auth, sent[1]=boot subscribe, sent[2]=incremental PLTR.
    boot = json.loads(sent[1])
    assert boot["action"] == "subscribe"
    assert {"SPY", "QQQ", "ANT"} <= set(boot["quotes"])

    incremental = json.loads(sent[2])
    assert incremental == {
        "action": "subscribe",
        "quotes": ["PLTR"],
        "trades": ["PLTR"],
        "bars": ["PLTR"],
    }


def test_control_only_wake_does_not_reprocess_last_frame(monkeypatch):
    """A control-only wake (no socket frame) must not reparse the previous
    message — the historical guard against double-processing a tick."""

    class ScriptedWS:
        """Answers the 3-frame handshake, then serves one quote tick before
        blocking on recv."""

        def __init__(self):
            self.received = []
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

        async def recv(self):
            self.received.append(1)
            n = len(self.received)
            if n <= 3:
                return '[{"T":"success","msg":"%s"}]' % [
                    "connected", "authenticated", "subscribed"][n - 1]
            if n == 4:
                return json.dumps([{"T": "q", "S": "SPY", "bp": 1.0, "bs": 1,
                                    "ap": 1.1, "as": 1, "t": "2026-11-19T20:00:00Z"}])
            # Block "forever": control queue gets the next wake.
            await asyncio.sleep(3600)

    class ConnectCM:
        async def __aenter__(self):
            return ScriptedWS()

        async def __aexit__(self, *exc):
            return False

    feed = _loop_feed()
    monkeypatch.setattr(feed_module.websockets, "connect", lambda *a, **k: ConnectCM())

    # Tick-processing needs the lazy dicts the __new__ bypass skipped.
    feed.quote_listeners = []
    feed.latest_quotes = {}
    feed.last_trade_prices = {}
    feed._last_quote_emit = {}
    quotes = []
    feed.quote_listeners.append(quotes.append)

    async def drive():
        await asyncio.wait_for(feed._websocket_listener_loop(), timeout=5.0)

    async def main():
        task = asyncio.ensure_future(drive())
        await asyncio.sleep(0.3)  # let the tick land
        feed.activate("ANT")      # control-only wake
        await asyncio.sleep(0.3)
        feed._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    # Exactly one quote was stored: the control wake did not re-emit it.
    assert len(feed.latest_quotes) == 1
    assert "SPY" in feed.latest_quotes


def test_pending_changes_survive_until_loop_starts():
    """activate() before start() parks changes in the plain list; the loop
    side folds them into the boot subscribe (no lost updates)."""
    feed = _loop_feed()
    import threading
    feed._sub_control_lock = threading.Lock()

    feed.activate("ANT")
    assert feed._sub_control is None
    assert feed._sub_control_pending == [("add", "ANT")]

    async def fold():
        q = feed._take_sub_control()
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items = asyncio.run(fold())
    # The parked "add" was already applied to self.symbols, so the boot
    # subscribe covers it: the fold drops it as redundant.
    assert items == []
    assert feed._sub_control is not None
    assert feed._sub_control_pending == []
    assert "ANT" in feed.symbols

    # Once the queue exists, later changes go into it directly (not the
    # pending list): a remove queued after boot is NOT covered by the boot
    # subscribe and must survive as a queue entry.
    feed.deactivate("ANT")
    assert feed._sub_control_pending == []
    assert "ANT" not in feed.symbols

    async def drain():
        items = []
        while not feed._sub_control.empty():
            items.append(feed._sub_control.get_nowait())
        return items

    assert asyncio.run(drain()) == [("remove", "ANT")]
