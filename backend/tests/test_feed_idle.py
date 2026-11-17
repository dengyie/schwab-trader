"""Background load must follow demand.

The chart prewarmer used to re-fetch thousands of candles per symbol every
pass, and the daily-context sweep hit Yahoo once per symbol every 45 seconds
even with nobody connected. Together they kept the single uvicorn worker at
100% CPU and stretched ``/api/health`` to 20 seconds on an otherwise idle
server. These tests pin the presence gate and the TTLs that stop that.
"""
from __future__ import annotations

import asyncio

from app.engine import alpaca_market_feed as feed_module
from app.engine.alpaca_market_feed import AlpacaMarketFeedService


def _bare_feed():
    """An instance whose constructor (and its network preload) is skipped."""
    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed._running = True
    feed.subscriber_probe = None
    return feed


def test_presence_gate_defaults_to_prewarming_when_unwired():
    assert _bare_feed()._has_chart_subscribers() is True


def test_probe_answer_decides_whether_charts_are_prewarmed():
    feed = _bare_feed()
    feed.subscriber_probe = lambda: False
    assert feed._has_chart_subscribers() is False
    feed.subscriber_probe = lambda: True
    assert feed._has_chart_subscribers() is True


def test_a_failing_probe_never_silences_the_prewarmer():
    feed = _bare_feed()

    def broken():
        raise RuntimeError("websocket manager gone")

    feed.subscriber_probe = broken
    assert feed._has_chart_subscribers() is True


def test_prewarm_loop_does_no_work_while_nobody_is_connected(monkeypatch):
    feed = _bare_feed()
    feed.subscriber_probe = lambda: False

    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        feed_module.YahooFinanceAdapter,
        "fetch_bars",
        staticmethod(lambda symbol, timeframe=None: fetched.append((symbol, timeframe)) or []),
    )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # End the loop after the initial delay and two idle waits.
        if len(sleeps) >= 3:
            feed._running = False

    monkeypatch.setattr(feed_module.asyncio, "sleep", fake_sleep)
    asyncio.run(feed._prewarm_cache_loop())

    assert fetched == []
    assert feed.IDLE_PREWARM_SECONDS in sleeps


def test_daily_context_is_not_swept_more_often_than_a_few_minutes():
    assert AlpacaMarketFeedService.SNAPSHOT_TTL >= 120.0
    assert AlpacaMarketFeedService.SNAPSHOT_TTL_CLOSED > AlpacaMarketFeedService.SNAPSHOT_TTL


def _utc(stamp: str):
    from datetime import datetime, timezone

    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)


def test_fast_open_check_agrees_with_the_full_clock():
    from app.core.markets import MARKETS, is_market_open, market_clock

    moments = [
        "2026-11-09T13:00:00",  # before the US open
        "2026-11-09T15:00:00",  # mid-session
        "2026-11-09T23:00:00",  # after the close
        "2026-11-07T15:00:00",  # Saturday
        "2026-11-08T15:00:00",  # Sunday
    ]
    for market in MARKETS.all():
        for stamp in moments:
            moment = _utc(stamp)
            assert is_market_open(market, moment) is market_clock(market, moment)["is_open"], (
                market.code,
                stamp,
            )


def test_wall_clock_clock_is_cheap_under_a_tick_storm():
    import time as _time

    from app.core.markets import MARKETS, is_market_open

    us = MARKETS.require("US")
    started = _time.perf_counter()
    for _ in range(20_000):
        is_market_open(us)
    per_call_us = (_time.perf_counter() - started) / 20_000 * 1e6

    # Un-memoised this rebuilt ~14 days of sessions (and their ISO strings),
    # which is what put the worker at 100% CPU; a tick must stay well under
    # 50us.
    assert per_call_us < 50, per_call_us


def test_tick_path_stores_raw_quotes_and_read_path_decorates():
    """The tick loop must not rebuild the Quote per print (2026-11-17).

    Decorating every accepted IEX print (two is_market_open round trips plus a
    model_copy) kept ~1/3 of a core busy during a session with zero websocket
    clients. The tick path now stores and forwards the raw venue quote; the
    decoration happens on the way out (read path / subscriber-gated broadcast).
    """
    from app.models.trading import Quote

    feed = _bare_feed()
    feed.latest_quotes = {}
    feed.daily_stats = {}
    raw = Quote(
        symbol="SPY", bid=753.47, ask=753.51, last=753.49,
        volume=120, timestamp=_utc("2026-11-17T15:00:00"),
    )
    feed.latest_quotes["SPY"] = raw
    feed.daily_stats["SPY"] = {"prev_close": 733.0, "day_high": 760.0, "day_low": 730.0, "volume": 5_000_000}

    # The cache holds the raw quote untouched...
    assert feed.latest_quotes["SPY"] is raw
    assert feed.latest_quotes["SPY"].prev_close is None

    # ...and the read path is where the consolidated context appears.
    served = feed.get_latest_quote("SPY")
    assert served is not None
    assert served.prev_close == 733.0
    assert served.volume == 5_000_000


def test_decorate_quote_is_the_public_entry_for_the_broadcast_path():
    """main.on_feed_quote decorates behind the subscriber gate via this alias."""
    from app.models.trading import Quote

    feed = _bare_feed()
    feed.daily_stats = {}
    raw = Quote(symbol="SPY", bid=0.0, ask=0.0, last=753.49, timestamp=_utc("2026-11-17T15:00:00"))
    feed.daily_stats["SPY"] = {"prev_close": 733.0, "last": 733.10, "consolidated": True, "last_ts": 1763382000.0}

    decorated = feed.decorate_quote(raw)
    assert decorated is not None
    # Venue context attached (the fields the frontend ticker row needs).
    assert decorated.prev_close == 733.0
    # The raw quote object is left untouched: the broker cache keeps it as-is.
    assert raw.prev_close is None


def test_is_market_open_explicit_now_is_never_cached():
    """Determinism: callers passing ``now`` (tests, clock rendering) bypass the TTL cache."""
    from datetime import datetime, timezone

    from app.core.markets import MARKETS, is_market_open, _market_open_cache

    us = MARKETS.require("US")
    warm = is_market_open(us)  # populates the cache for US
    assert ("US" in _market_open_cache) == True

    # An explicit ``now`` still computes from that moment (open Sunday evening UTC
    # is pre-open Monday or Sunday closure depending on the moment chosen).
    saturday = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    assert is_market_open(us, saturday) is False
    assert is_market_open(us, saturday) is False
