"""Unit tests for broadcast target selection.

A socket is accepted before the token is verified, so the manager must never
treat accepted as trusted: otherwise the simulation feed delivers a quote to a
client that is about to be rejected.
"""
from app.api.websocket_manager import WebSocketManager


def test_unauthenticated_socket_receives_nothing():
    manager = WebSocketManager()
    fake_socket = object()

    manager.active_connections[fake_socket] = {"SPY"}
    assert manager._targets() == []
    assert manager._targets("SPY") == []

    manager.mark_authenticated(fake_socket)
    assert manager._targets() == [fake_socket]
    assert manager._targets("SPY") == [fake_socket]
    # A trusted socket only gets symbols it actually subscribed to.
    assert manager._targets("QQQ") == []


def test_disconnect_forgets_trust():
    manager = WebSocketManager()
    fake_socket = object()

    manager.active_connections[fake_socket] = {"SPY"}
    manager.mark_authenticated(fake_socket)
    manager.disconnect(fake_socket)

    assert manager.active_connections == {}
    assert not manager.is_authenticated(fake_socket)
    assert manager._targets() == []


def test_disconnect_is_idempotent():
    manager = WebSocketManager()
    fake_socket = object()

    # Closing an already-removed socket must not raise key errors.
    manager.disconnect(fake_socket)
    manager.disconnect(fake_socket)


def test_subscribers_only_counts_authenticated_watchers():
    manager = WebSocketManager()
    assert manager.has_subscribers() is False

    # Accepted but not yet trusted: it may never receive a quote.
    pending = object()
    manager.active_connections[pending] = {"SPY"}
    assert manager.has_subscribers() is False

    watcher = object()
    manager.active_connections[watcher] = set()
    manager.mark_authenticated(watcher)
    assert manager.has_subscribers() is False

    manager.subscribe(watcher, "spy")
    assert manager.has_subscribers() is True

    manager.unsubscribe(watcher, "SPY")
    assert manager.has_subscribers() is False
