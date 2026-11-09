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
