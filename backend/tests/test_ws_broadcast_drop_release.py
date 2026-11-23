"""Broadcast-path drop regressions (2026-11-23 second review P1-2).

_send_many() drops dead/slow clients by calling disconnect() directly —
but the feed-level release (_release_feed_symbols) lived only in
websocket_endpoint's exception handlers. A socket dropped by the
broadcast path is already popped from active_connections, so the later
endpoint-level disconnect() returns an EMPTY set and feed.deactivate()
never runs: every slow tab leaked its symbols into the feed forever,
re-exhausting the 50-symbol dynamic cap the refcount fix was meant to
save. The release hook must fire wherever a socket leaves the manager.
"""
import asyncio

from app.api.websocket_manager import WebSocketManager


class FakeSocket:
    """Minimal stand-in: send_text() succeeds or fails per `fail` flag."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(payload)


def test_broadcast_drop_releases_last_watcher_symbols():
    manager = WebSocketManager()
    slow = FakeSocket(fail=True)  # a stuck tab: send times out / errors
    manager.active_connections[slow] = set()
    manager._authenticated.add(slow)
    manager.subscribe(slow, "QQQ")

    released_via_hook: list[set[str]] = []
    manager.on_last_watcher_left = lambda syms: released_via_hook.append(set(syms))

    asyncio.run(manager._send_many('{"type":"QUOTE"}', [slow]))

    # The socket must be gone from the manager...
    assert slow not in manager.active_connections
    assert manager._symbol_refs.get("QQQ") is None
    # ...and the feed-level release must have been notified.
    assert released_via_hook == [{"QQQ"}]


def test_broadcast_drop_only_releases_last_watcher():
    manager = WebSocketManager()
    slow = FakeSocket(fail=True)
    healthy = FakeSocket()
    for ws in (slow, healthy):
        manager.active_connections[ws] = set()
        manager._authenticated.add(ws)
    manager.subscribe(slow, "SPY")
    manager.subscribe(healthy, "SPY")  # SPY still watched elsewhere

    released_via_hook: list[set[str]] = []
    manager.on_last_watcher_left = lambda syms: released_via_hook.append(set(syms))

    asyncio.run(manager._send_many('{"type":"QUOTE"}', [slow, healthy]))

    assert manager._symbol_refs["SPY"] == 1
    assert released_via_hook == []  # not the last watcher — no feed release


def test_broadcast_drop_without_hook_still_cleans_refs():
    """main.py wires the hook at import time; a bare manager must not crash."""
    manager = WebSocketManager()
    slow = FakeSocket(fail=True)
    manager.active_connections[slow] = set()
    manager.subscribe(slow, "SPY")

    asyncio.run(manager._send_many("x", [slow]))

    assert slow not in manager.active_connections
    assert "SPY" not in manager._symbol_refs
