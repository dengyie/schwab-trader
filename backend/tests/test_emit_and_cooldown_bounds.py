"""Bound regressions (2026-11-28 review B5): nothing per-symbol grows forever.

Two leak paths pinned here:

* defeatbeta's failure cooldown dict used to keep one entry per distinct
  symbol that EVER failed; a long-lived process probing dead symbols grew it
  without bound. Expired cooldowns are now pruned when a new failure arms —
  under _cache_lock, so no reader sees a half-pruned map.
* The tick loop's per-symbol emit stamp (_last_quote_emit) used to survive
  deactivate; it is dropped with the subscription, race-safe against the
  event-loop tick path.
"""
from __future__ import annotations

import threading
import time

import pytest

import app.adapters.defeatbeta_adapter as db
from app.engine.alpaca_market_feed import AlpacaMarketFeedService, QUOTE_EMIT_SECONDS


# -- defeatbeta cooldown pruning -------------------------------------------

def _arm(symbol: str, armed_at: float) -> None:
    with db._cache_lock:
        db._cooldown[symbol] = armed_at


@pytest.fixture(autouse=True)
def _clean_cooldown():
    with db._cache_lock:
        db._cooldown.clear()
    yield
    with db._cache_lock:
        db._cooldown.clear()


def test_prune_drops_only_expired_cooldowns_under_the_lock():
    now = time.time()
    _arm("OLD", now - db._DEFEATBETA_COOLDOWN_SECONDS - 1.0)
    _arm("FRESH", now - 1.0)

    with db._cache_lock:  # the lock the prune must run under
        db._prune_expired_cooldowns(now)

    assert "OLD" not in db._cooldown
    assert "FRESH" in db._cooldown, "a live cooldown gates retries and must survive"


def test_prune_keeps_a_cooldown_that_can_still_gate():
    """Prune must agree with the retry gate exactly: the fast path retries
    when `now - armed >= window`, so an entry aged to (or past) the window can
    no longer gate and must be pruned; anything younger still gates."""
    now = time.time()
    _arm("GATING", now - db._DEFEATBETA_COOLDOWN_SECONDS + 1.0)
    _arm("EXPIRED", now - db._DEFEATBETA_COOLDOWN_SECONDS)

    with db._cache_lock:  # the lock the prune must run under
        db._prune_expired_cooldowns(now)

    assert "GATING" in db._cooldown
    assert "EXPIRED" not in db._cooldown


def test_new_failure_arms_a_pruned_cooldown_map(monkeypatch):
    """The prune runs when a failure arms: a map full of expired entries is
    cleaned in the same event, and the new failure still lands."""
    now = time.time()
    _arm("STALE1", now - db._DEFEATBETA_COOLDOWN_SECONDS - 10)
    _arm("STALE2", now - db._DEFEATBETA_COOLDOWN_SECONDS - 20)

    def dead_scan(sym):
        raise ConnectionError("dataset unreachable")

    monkeypatch.setattr(db, "_load_daily_bars", dead_scan)
    assert db.fetch_daily_history("SPY") is None

    with db._cache_lock:
        assert "STALE1" not in db._cooldown
        assert "STALE2" not in db._cooldown
        assert time.time() - db._cooldown["SPY"] < 1.0
        assert db._last_error


def test_pruned_symbol_can_retry_immediately_after_expiry(monkeypatch):
    """Prune must not change the retry semantics: after the window passes the
    symbol is scannable again (and an in-window failure still short-circuits)."""
    scans: list[str] = []

    def counting_scan(sym):
        scans.append(sym)
        raise ConnectionError("flaky")

    monkeypatch.setattr(db, "_load_daily_bars", counting_scan)

    assert db.fetch_daily_history("SPY") is None  # arms the cooldown
    assert db.fetch_daily_history("SPY") is None  # in-window: absorbed
    assert scans == ["SPY"]

    # Age the cooldown past its window, then simulate the arm-time prune.
    with db._cache_lock:
        db._cooldown["SPY"] = time.time() - db._DEFEATBETA_COOLDOWN_SECONDS - 0.001
    db.fetch_daily_history("SPY")
    assert scans == ["SPY", "SPY"], "expired cooldown no longer gates the retry"


# -- feed _last_quote_emit lifecycle ----------------------------------------

def _loop_feed() -> AlpacaMarketFeedService:
    """__new__-built feed with only the fields deactivate() touches (mirrors
    tests/test_feed_dynamic_subscribe.py — __new__ instances carry no
    instance dict, so deactivate must be getattr-defensive)."""
    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed._running = True
    feed.stream_url = "wss://example.invalid/stream"
    feed.api_key = "key"
    feed.api_secret = "secret"
    feed.symbols = {"SPY"}
    feed._core_symbols = frozenset(feed.symbols)
    feed._sub_control = None
    feed._sub_control_lock = threading.Lock()
    feed._sub_control_pending = []
    feed._daily_warm_tasks = set()
    feed.ensure_daily_stats = lambda *a, **k: None
    feed.feed = "iex"
    feed._stream_state = "idle"
    feed._stream_since = 0.0
    feed._stream_last_error = None
    feed._stream_failures = 0
    feed._stream_quota = False
    return feed


def test_deactivate_drops_the_emit_stamp():
    feed = _loop_feed()
    feed.activate("ANT")
    feed._last_quote_emit = {"ANT": time.monotonic()}  # tick loop stamped it

    feed.deactivate("ANT")

    assert "ANT" not in feed._last_quote_emit
    assert "ANT" not in feed.symbols


def test_deactivate_without_emit_stamp_attr_still_works():
    """__new__-built feeds in older tests never set _last_quote_emit; the pop
    must be getattr-defensive, not an AttributeError."""
    feed = _loop_feed()
    feed.activate("ANT")
    assert not hasattr(feed, "_last_quote_emit")
    feed.deactivate("ANT")  # must not raise
    assert "ANT" not in feed.symbols


def test_deactivate_keeps_emit_stamps_of_still_live_symbols():
    feed = _loop_feed()
    feed.activate("ANT")
    stamp = time.monotonic()
    feed._last_quote_emit = {"ANT": stamp, "SPY": stamp}

    feed.deactivate("ANT")

    assert feed._last_quote_emit == {"SPY": stamp}


def test_deactivate_emit_pop_is_race_safe_against_tick_reads():
    """The tick loop reads/writes single keys (get/setitem, never iteration)
    while deactivate pops; hammer both concurrently and the stamp must end
    up either present-and-live or gone — never corrupting the dict."""
    feed = _loop_feed()
    feed.activate("ANT")
    feed._last_quote_emit = {"ANT": 0.0}
    stop = threading.Event()
    errors: list[Exception] = []

    def tick_side():
        while not stop.is_set():
            try:
                sym = "ANT"
                if time.monotonic() - feed._last_quote_emit.get(sym, 0.0) < QUOTE_EMIT_SECONDS:
                    continue
                feed._last_quote_emit[sym] = time.monotonic()
            except Exception as exc:  # any torn-state symptom fails the test
                errors.append(exc)
                return

    worker = threading.Thread(target=tick_side, daemon=True)
    worker.start()
    try:
        for _ in range(50):
            feed.activate("ANT")
            feed.deactivate("ANT")
    finally:
        stop.set()
        worker.join(timeout=2.0)

    assert errors == []
    assert isinstance(feed._last_quote_emit, dict)


def test_emit_window_constant_matches_manager_conflation():
    """The two 40ms gates (feed emit window, manager conflation) were sized
    to the same ~25 FPS UI budget; they must not drift apart silently."""
    from app.api.websocket_manager import WebSocketManager

    assert QUOTE_EMIT_SECONDS == WebSocketManager().throttle_sec
