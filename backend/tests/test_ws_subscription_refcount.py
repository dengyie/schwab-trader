"""Subscription refcount regressions (2026-11-23 review P1-4).

A tab close / network drop never sends UNSUBSCRIBE — disconnect() must
release the per-symbol references so the feed-level subscription is torn
down after the LAST watcher leaves. Without the refcount every watched
symbol leaked forever and the 50-symbol dynamic cap was permanently
exhausted within weeks of uptime.
"""
from app.api.websocket_manager import WebSocketManager


def test_subscribe_refcounts_per_socket():
    manager = WebSocketManager()
    ws_a, ws_b = object(), object()

    manager.active_connections[ws_a] = set()
    manager.active_connections[ws_b] = set()

    manager.subscribe(ws_a, "SPY")
    manager.subscribe(ws_b, "SPY")
    assert manager._symbol_refs["SPY"] == 2

    # Duplicate subscribe from the same socket must not inflate the count.
    manager.subscribe(ws_a, "SPY")
    assert manager._symbol_refs["SPY"] == 2

    assert manager.unsubscribe(ws_a, "SPY") is False
    assert manager._symbol_refs["SPY"] == 1
    assert manager.unsubscribe(ws_b, "SPY") is True  # last watcher left
    assert "SPY" not in manager._symbol_refs


def test_disconnect_releases_last_watcher_symbols():
    manager = WebSocketManager()
    ws_a, ws_b = object(), object()
    manager.active_connections[ws_a] = set()
    manager.active_connections[ws_b] = set()

    manager.subscribe(ws_a, "SPY")
    manager.subscribe(ws_a, "QQQ")
    manager.subscribe(ws_b, "SPY")

    released = manager.disconnect(ws_a)
    assert released == {"QQQ"}  # SPY still watched by ws_b
    assert manager._symbol_refs["SPY"] == 1
    assert "QQQ" not in manager._symbol_refs

    released = manager.disconnect(ws_b)
    assert released == {"SPY"}


def test_unsubscribe_unknown_socket_is_safe():
    manager = WebSocketManager()
    assert manager.unsubscribe(object(), "SPY") is False
    assert manager.disconnect(object()) == set()
