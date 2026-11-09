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
