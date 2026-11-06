"""
Unit tests for YahooFinanceAdapter, 4h aggregation, client lifecycle, and multi-timeframe fallback.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.models.trading import Bar
from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.engine.alpaca_market_feed import AlpacaMarketFeedService, MAX_CACHE_ENTRIES

ET = ZoneInfo("America/New_York")


def _et_ts(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def test_yahoo_aggregate_bars_4h_aligned_to_session_open():
    # 2026-11-02 is a Monday (EST). Six 1h bars from the 09:30 ET open:
    # 09:30, 10:30, 11:30, 12:30 -> bucket 1; 13:30, 14:30 -> bucket 2.
    t = _et_ts(2026, 11, 2, 9, 30)
    bars = [
        Bar(time=t + 0 * 3600, open=100.0, high=105.0, low=99.0, close=104.0, volume=1000.0),
        Bar(time=t + 1 * 3600, open=104.0, high=108.0, low=103.0, close=107.0, volume=1500.0),
        Bar(time=t + 2 * 3600, open=107.0, high=109.0, low=106.0, close=108.0, volume=800.0),
        Bar(time=t + 3 * 3600, open=108.0, high=110.0, low=107.0, close=109.5, volume=1200.0),
        Bar(time=t + 4 * 3600, open=110.0, high=112.0, low=109.0, close=111.0, volume=2000.0),
        Bar(time=t + 5 * 3600, open=111.0, high=115.0, low=110.5, close=114.0, volume=2500.0),
        # Next trading day: must start a new bucket even though it is < 4h later.
        Bar(time=_et_ts(2026, 11, 3, 9, 30), open=114.0, high=116.0, low=113.0, close=115.0, volume=900.0),
    ]

    aggregated = YahooFinanceAdapter._aggregate_bars(bars, bucket_seconds=14400)
    assert len(aggregated) == 3

    b1 = aggregated[0]
    assert b1.time == t
    assert (b1.open, b1.high, b1.low, b1.close) == (100.0, 110.0, 99.0, 109.5)
    assert b1.volume == 4500.0

    b2 = aggregated[1]
    assert b2.time == t + 4 * 3600
    assert (b2.open, b2.high, b2.low, b2.close) == (110.0, 115.0, 109.0, 114.0)
    assert b2.volume == 4500.0

    b3 = aggregated[2]
    assert b3.time == _et_ts(2026, 11, 3, 9, 30)
    assert b3.volume == 900.0


def test_yahoo_client_lifecycle():
    client1 = YahooFinanceAdapter.get_client()
    assert client1 is not None
    assert not client1.is_closed

    client2 = YahooFinanceAdapter.get_client()
    assert client1 is client2

    YahooFinanceAdapter.close_client()
    assert YahooFinanceAdapter._shared_client is None


def _make_feed(monkeypatch) -> AlpacaMarketFeedService:
    # Never hit the network during unit tests.
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync", lambda self: None)
    return AlpacaMarketFeedService(
        initial_symbols=["SPY"],
        api_key="test_key",
        api_secret="test_secret",
    )


def test_alpaca_market_feed_fallback_order(monkeypatch):
    feed = _make_feed(monkeypatch)

    # 1. Populate 1m bars in historical_bars
    m1_bars = [Bar(time=1000, open=500.0, high=501.0, low=499.0, close=500.5, volume=100.0)]
    feed.historical_bars["SPY"] = m1_bars

    # 2. Populate stale 1h cache
    h1_bars = [Bar(time=3600, open=500.0, high=510.0, low=495.0, close=508.0, volume=50000.0)]
    feed.multi_tf_bars["SPY"] = {"1h": h1_bars}
    feed._bars_cache_ts["SPY"] = {"1h": 0.0}  # completely expired TTL

    # 3. Simulate Yahoo Finance failing
    def mock_fetch_fail(sym, timeframe="15m", client=None):
        raise ConnectionError("Yahoo Finance network timeout")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", mock_fetch_fail)

    # When Yahoo fails, must return stale 1h bars, NOT 1m bars!
    bars = feed.get_bars("SPY", timeframe="1h")
    assert len(bars) == 1
    assert bars[0].volume == 50000.0
    assert bars[0].high == 510.0

    # 4. If 1m requested and no cache, returns 1m historical bars
    feed.multi_tf_bars["SPY"].pop("1m", None)
    feed._bars_cache_ts["SPY"].pop("1m", None)
    bars_1m = feed.get_bars("SPY", timeframe="1m")
    assert len(bars_1m) == 1
    assert bars_1m[0].volume == 100.0


def test_get_bars_does_not_overwrite_live_trade_price(monkeypatch):
    feed = _make_feed(monkeypatch)
    feed.last_trade_prices["SPY"] = 600.25

    fresh = [Bar(time=1000, open=590.0, high=595.0, low=589.0, close=594.0, volume=10.0)]
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: fresh)

    bars = feed.get_bars("SPY", timeframe="15m")
    assert bars == fresh
    # A historical close must never clobber the live trade price.
    assert feed.last_trade_prices["SPY"] == 600.25


def test_unknown_timeframe_returns_empty():
    # Unknown keys are rejected before any network call; never silently map to 15m.
    assert YahooFinanceAdapter.fetch_bars("SPY", timeframe="bogus") == []


def test_bar_cache_is_bounded(monkeypatch):
    feed = _make_feed(monkeypatch)
    for i in range(MAX_CACHE_ENTRIES + 20):
        feed._cache_put(f"SYM{i}", "1m", [Bar(time=i, open=1, high=1, low=1, close=1, volume=1)])
    total = sum(len(tfs) for tfs in feed.multi_tf_bars.values())
    assert total == MAX_CACHE_ENTRIES
