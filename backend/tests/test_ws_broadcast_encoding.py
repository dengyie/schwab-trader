"""Broadcast encoding regressions (2026-11-28 review B3).

The manager used to require a pre-``model_dump``ed dict, so every broadcast
paid "Pydantic model -> dict" plus "dict -> JSON text" as two separate
passes, encoded the envelope before knowing whether anyone could receive it,
and left a throttle stamp behind for symbols nobody watched. These tests pin
the fixed contract:

* The manager accepts Pydantic models AND legacy plain dicts (main.py's
  current call shape keeps working either way).
* The payload is encoded exactly once (``model_dump_json`` — never via an
  intermediate ``model_dump`` dict) and only after the conflation gate and a
  non-empty target list.
* The wire schema is unchanged: ``{"type": ..., "data": {...}}`` parse-equal
  to the old ``json.dumps`` output.
* Throttle stamps die with the last watcher instead of accumulating one
  float per ever-quoted symbol.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.api.websocket_manager import WebSocketManager
from app.models.trading import Bar, Quote, Trade


class FakeSocket:
    """Minimal stand-in: send_text() succeeds or fails per `fail` flag."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(payload)


def _manager_with_watcher(symbol: str = "SPY", throttle_ms: float = 1000.0):
    manager = WebSocketManager(throttle_ms=throttle_ms)
    ws = FakeSocket()
    manager.active_connections[ws] = set()
    manager._authenticated.add(ws)
    manager.subscribe(ws, symbol)
    return manager, ws


def _quote(**overrides) -> Quote:
    fields = dict(
        symbol="SPY", bid=753.47, ask=753.51, last=753.49,
        timestamp=datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return Quote(**fields)


def test_model_and_dict_broadcast_are_parse_equal():
    """The wire schema is unchanged by model input: a client cannot tell
    whether the manager received a Quote or main.py's legacy model_dump dict."""
    manager, ws = _manager_with_watcher()
    quote = _quote()

    asyncio.run(manager.broadcast_quote(quote))
    model_payload = ws.sent[0]

    ws.sent.clear()
    asyncio.run(manager.broadcast_quote(quote.model_dump(mode="json"), force=True))
    dict_payload = ws.sent[0]

    envelope = json.loads(model_payload)
    assert envelope["type"] == "QUOTE_UPDATE"
    assert envelope["data"]["symbol"] == "SPY"
    assert envelope["data"]["last"] == 753.49
    assert envelope == json.loads(dict_payload)


def test_model_is_encoded_once_never_through_model_dump():
    """``model_dump`` + ``json.dumps`` is two passes over the same data; the
    manager must serialize a model straight to text via ``model_dump_json``."""

    class NoDictQuote(Quote):
        def model_dump(self, **kwargs):
            raise AssertionError("manager must not convert the model to a dict first")

    manager, ws = _manager_with_watcher()
    asyncio.run(manager.broadcast_quote(NoDictQuote(**_quote().model_dump())))
    assert json.loads(ws.sent[0])["data"]["symbol"] == "SPY"


def test_conflation_gates_before_the_encode():
    """A sub-throttle tick is conflated without serializing anything."""
    manager, ws = _manager_with_watcher(throttle_ms=1000.0)  # 1s window
    quote = _quote()

    asyncio.run(manager.broadcast_quote(quote))
    assert len(ws.sent) == 1

    encode_calls: list[object] = []
    original = manager._encode

    def counting(data):
        encode_calls.append(data)
        return original(data)

    manager._encode = counting
    asyncio.run(manager.broadcast_quote(_quote(last=753.50)))
    assert len(ws.sent) == 1, "second tick inside the window must be conflated"
    assert encode_calls == [], "conflated tick must not be serialized"


def test_no_target_tick_encodes_nothing_and_leaves_no_stamp():
    """A quote for a symbol nobody watches must not pay an encode nor leave a
    throttle stamp in last_broadcast_ts (the no-target stamp accumulator)."""
    manager, _ws = _manager_with_watcher("QQQ")  # only QQQ is watched

    asyncio.run(manager.broadcast_quote(_quote(symbol="SPY")))

    assert manager.last_broadcast_ts == {}
    assert manager.last_broadcast_ts.get("SPY") is None


def test_unwatched_symbol_stamp_never_appears_even_forced():
    manager, _ws = _manager_with_watcher("QQQ")
    asyncio.run(manager.broadcast_quote(_quote(symbol="SPY"), force=True))
    assert manager.last_broadcast_ts == {}


def test_unauthenticated_sockets_receive_nothing():
    manager = WebSocketManager()
    accepted = FakeSocket()
    manager.active_connections[accepted] = {"SPY"}  # accepted, never trusted

    asyncio.run(manager.broadcast_quote(_quote()))

    assert accepted.sent == []
    assert manager.last_broadcast_ts == {}, "no trusted target: no stamp either"


def test_stamp_dies_with_the_last_watcher_on_unsubscribe():
    manager = WebSocketManager()
    ws = FakeSocket()
    manager.active_connections[ws] = set()
    manager._authenticated.add(ws)
    manager.subscribe(ws, "SPY")

    asyncio.run(manager.broadcast_quote(_quote()))
    assert "SPY" in manager.last_broadcast_ts

    assert manager.unsubscribe(ws, "SPY") is True
    assert "SPY" not in manager.last_broadcast_ts


def test_stamp_survives_while_other_watchers_remain():
    manager = WebSocketManager()
    first, second = FakeSocket(), FakeSocket()
    for ws in (first, second):
        manager.active_connections[ws] = set()
        manager._authenticated.add(ws)
        manager.subscribe(ws, "SPY")

    asyncio.run(manager.broadcast_quote(_quote()))
    assert manager.unsubscribe(first, "SPY") is False
    assert "SPY" in manager.last_broadcast_ts

    manager.disconnect(second)  # last watcher leaves via the drop path
    assert "SPY" not in manager.last_broadcast_ts
    assert "SPY" not in manager._symbol_refs


def test_broadcast_trade_accepts_model_and_dict():
    manager, ws = _manager_with_watcher()
    trade = Trade(
        trade_id="t1", order_id="o1", symbol="SPY", side="BUY",
        price=753.49, quantity=10,
    )

    asyncio.run(manager.broadcast_trade(trade))
    assert json.loads(ws.sent[0])["type"] == "TRADE_UPDATE"

    ws.sent.clear()
    asyncio.run(manager.broadcast_trade(trade.model_dump(mode="json")))
    assert json.loads(ws.sent[0]) == {
        "type": "TRADE_UPDATE", "data": trade.model_dump(mode="json"),
    }


def test_broadcast_bar_accepts_model_and_dict_and_keeps_symbol_key():
    manager, ws = _manager_with_watcher()
    bar = Bar(time=1764322800, open=753.0, high=754.0, low=752.0, close=753.5, volume=100.0)

    asyncio.run(manager.broadcast_bar("spy", bar))  # symbol arg is normalized
    envelope = json.loads(ws.sent[0])
    assert envelope["type"] == "BAR_UPDATE"
    assert envelope["symbol"] == "SPY"
    assert envelope["data"]["close"] == 753.5

    ws.sent.clear()
    asyncio.run(manager.broadcast_bar("SPY", bar.model_dump(mode="json")))
    assert json.loads(ws.sent[0]) == envelope


def test_undeserializable_payload_is_dropped_without_raising():
    """A payload the encoder cannot render must log and drop, never take the
    tick path (or the fill broadcast) down."""
    manager, ws = _manager_with_watcher()

    asyncio.run(manager.broadcast({"type": "X", "data": object()}))
    assert ws.sent == []

    asyncio.run(manager.broadcast_quote({"symbol": "SPY", "last": {1, 2}}))
    assert ws.sent == []


def test_order_fill_broadcast_dict_contract_unchanged():
    """_broadcast_order_filled passes a plain dict; it must still reach every
    trusted client exactly as before (broadcast() has no symbol filter)."""
    manager = WebSocketManager()
    watcher, bystander = FakeSocket(), FakeSocket()
    for ws, symbols in ((watcher, {"SPY"}), (bystander, set())):
        manager.active_connections[ws] = symbols
        manager._authenticated.add(ws)
    manager.subscribe(watcher, "SPY")

    untrusted = FakeSocket()
    manager.active_connections[untrusted] = {"SPY"}  # accepted, not trusted

    asyncio.run(manager.broadcast({"type": "ORDER_FILLED", "trade": {"symbol": "SPY"}}))

    assert json.loads(watcher.sent[0])["type"] == "ORDER_FILLED"
    assert len(bystander.sent) == 1, "trusted clients get all-client broadcasts"
    assert untrusted.sent == [], "but never an untrusted socket"
