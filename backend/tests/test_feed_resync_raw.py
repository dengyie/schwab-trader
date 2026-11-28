"""Quote-resync regressions (2026-11-28 review B4).

The 15s resync loop used to decorate every watched symbol every sweep (a
``model_copy`` plus two ``is_market_open`` round trips per symbol) and hand
the listeners a rebuilt Quote. Decoration belongs to the broadcast egress
(``main.on_feed_quote``) where it runs once behind the subscriber gate; the
resync must forward the raw cached quote — and must KEEP notifying while the
UI is idle, because the same listener feeds broker execution and
mark-to-market. Gating the notify on browser subscribers would freeze the
paper book the moment the last tab closes.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from app.engine import alpaca_market_feed as feed_module
from app.engine.alpaca_market_feed import AlpacaMarketFeedService
from app.models.trading import Quote


def _bare_feed() -> AlpacaMarketFeedService:
    """An instance whose constructor (and its network preload) is skipped."""
    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed._running = True
    feed.subscriber_probe = None
    feed.symbols = {"SPY"}
    feed._sub_control_lock = threading.Lock()
    feed.latest_quotes = {}
    return feed


def _raw_quote() -> Quote:
    return Quote(
        symbol="SPY", bid=753.47, ask=753.51, last=753.49, volume=120,
        timestamp=datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc),
    )


def _run_one_sweep(feed: AlpacaMarketFeedService, monkeypatch) -> None:
    """Run exactly one iteration body of the periodic resync loop.

    The loop sleeps 15s FIRST, then does its sweep, then sleeps again. Stop it
    on the second sleep so the body has run exactly once.
    """
    real_sleep = asyncio.sleep
    sleeps = 0

    async def stop_after_body(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            feed._running = False
        await real_sleep(0)

    monkeypatch.setattr(feed_module.asyncio, "sleep", stop_after_body)
    # The daily sweep (first statement of the body) is not what these tests
    # exercise; keep it inert instead of allowing a network call.
    monkeypatch.setattr(feed_module.asyncio, "to_thread", lambda fn, *a: _noop_thread(fn, *a))
    asyncio.run(feed._periodic_quote_sync_loop())


async def _noop_thread(fn, *args):
    return None


def test_resync_notifies_the_raw_cached_quote_object(monkeypatch):
    """Identity, not just values: no model_copy, no rebuild — the listener
    receives the exact Quote instance the tick loop stored."""
    feed = _bare_feed()
    raw = _raw_quote()
    feed.latest_quotes["SPY"] = raw
    # Poison the daily context: decoration WOULD change the quote (prev_close
    # attached); the raw forward must not read it at all.
    feed.daily_stats = {"SPY": {"prev_close": 733.0, "last": 733.10, "consolidated": 1.0}}

    notified: list[Quote] = []
    feed.quote_listeners = [notified.append]

    _run_one_sweep(feed, monkeypatch)

    assert notified == [raw]
    assert notified[0] is raw
    assert notified[0].prev_close is None, "resync must not decorate"


def test_resync_keeps_notifying_with_zero_ui_subscribers(monkeypatch):
    """Idle server, no browser watchers: the execution path (broker.on_quote
    via main's listener) must still receive quotes — never gate the notify on
    the subscriber probe."""
    feed = _bare_feed()
    feed.subscriber_probe = lambda: False  # no UI clients at all
    feed.latest_quotes["SPY"] = _raw_quote()

    notified: list[Quote] = []
    feed.quote_listeners = [notified.append]

    _run_one_sweep(feed, monkeypatch)

    assert len(notified) == 1, "execution/mark-to-market feed must survive idle"


def test_resync_never_calls_decorate_even_when_wired(monkeypatch):
    """If a future refactor reintroduces decoration in the loop, this fails."""
    feed = _bare_feed()
    feed.latest_quotes["SPY"] = _raw_quote()
    feed.quote_listeners = []

    def forbidden(quote):
        raise AssertionError("resync must not decorate; egress owns decoration")

    monkeypatch.setattr(feed, "_decorate_daily", forbidden)
    _run_one_sweep(feed, monkeypatch)


def test_resync_skips_symbols_without_a_cached_quote(monkeypatch):
    feed = _bare_feed()
    feed.symbols = {"SPY", "QQQ"}
    feed.latest_quotes = {"SPY": _raw_quote()}
    notified: list[Quote] = []
    feed.quote_listeners = [notified.append]

    _run_one_sweep(feed, monkeypatch)

    assert [q.symbol for q in notified] == ["SPY"]


def test_resync_snapshot_of_symbols_is_taken_under_the_lock(monkeypatch):
    """The loop reads the subscription set under _sub_control_lock (the same
    lock activate/deactivate mutate it under) — no mid-iteration RuntimeError."""
    feed = _bare_feed()

    class FussySet(set):
        def __iter__(self):
            lock_held = feed._sub_control_lock.locked()
            assert lock_held, "symbols must be copied under _sub_control_lock"
            return super().__iter__()

    feed.symbols = FussySet({"SPY"})
    feed.latest_quotes = {"SPY": _raw_quote()}
    feed.quote_listeners = []
    _run_one_sweep(feed, monkeypatch)
