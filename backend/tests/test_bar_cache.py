"""
Tests for the bar cache: staleness policy, single-flight, failure cooldown and
disk persistence.

The contract these pin is the one the charts depend on: a request never waits
on upstream when any data exists, a stale chart is refreshed exactly once no
matter how many callers ask, and a restart does not start cold.
"""
import json
import os
import threading
import time

import pytest

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.engine.alpaca_market_feed import MAX_CACHE_ENTRIES, AlpacaMarketFeedService
from app.engine.bar_cache import BarCache
from app.models.trading import Bar


def _bars(close=100.0, count=3):
    return [
        Bar(time=1000 + i * 60, open=close, high=close + 1, low=close - 1,
            close=close + i, volume=10.0 * (i + 1))
        for i in range(count)
    ]


@pytest.fixture
def make_feed(monkeypatch):
    """An Alpaca feed with no network at construction."""
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync", lambda self: None)
    monkeypatch.setattr(AlpacaMarketFeedService, "refresh_daily_stats", lambda self, *a, **k: None)

    def _make(cache_dir=None):
        return AlpacaMarketFeedService(
            initial_symbols=["SPY"],
            api_key="test_key",
            api_secret="test_secret",
            cache_dir=cache_dir,
        )

    return _make


def _drain(feed):
    """Wait for scheduled refreshes so assertions cannot race them."""
    pool = feed._refresh_pool
    if pool is not None:
        pool.shutdown(wait=True)


# -- BarCache unit tests ---------------------------------------------------
def test_fresh_entry_does_not_touch_upstream(make_feed, monkeypatch):
    feed = make_feed()
    bars = _bars()
    feed._cache_put("SPY", "15m", bars)

    def boom(*a, **k):
        raise AssertionError("a fresh entry must not be refetched")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)
    assert feed.get_bars("SPY", "15m") == bars


def test_stale_entry_is_served_immediately_and_refreshed_once(make_feed, monkeypatch):
    feed = make_feed()
    stale = _bars(close=50.0)
    feed._cache_put("SPY", "1m", stale, ts=time.time() - 3600)

    calls = []
    fresh = _bars(close=90.0)

    def fetch(sym, timeframe="15m", client=None):
        calls.append(sym)
        time.sleep(0.05)
        return fresh

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    started = time.time()
    for _ in range(5):
        assert feed.get_bars("SPY", "1m") == stale
    assert time.time() - started < 0.5, "stale reads must not block on the refresh"

    _drain(feed)
    assert calls == ["SPY"], "five stale reads must schedule exactly one fetch"
    assert feed.get_bars("SPY", "1m") == fresh


def test_concurrent_cold_requests_share_one_fetch(make_feed, monkeypatch):
    feed = make_feed()
    calls = []
    fresh = _bars(close=77.0)

    def fetch(sym, timeframe="15m", client=None):
        calls.append(sym)
        time.sleep(0.2)
        return fresh

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    results = []
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        results.append(feed.get_bars("SPY", "15m"))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert calls == ["SPY"], "single-flight must collapse the five cold reads"
    assert all(result == fresh for result in results)


def test_failure_cools_down_and_keeps_serving_stale(make_feed, monkeypatch):
    feed = make_feed()
    stale = _bars(close=42.0)
    feed._cache_put("SPY", "5m", stale, ts=time.time() - 3600)

    calls = []

    def fetch(sym, timeframe="15m", client=None):
        calls.append(sym)
        raise ConnectionError("upstream down")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    assert feed.get_bars("SPY", "5m") == stale
    _drain(feed)
    assert calls == ["SPY"]

    # Inside the cooldown window the failure is not retried on every read.
    assert feed.get_bars("SPY", "5m") == stale
    _drain(feed)
    assert calls == ["SPY"]


def test_cold_miss_with_nothing_cached_returns_empty_while_cooling(make_feed, monkeypatch):
    feed = make_feed()

    def fetch(sym, timeframe="15m", client=None):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    assert feed.get_bars("NFLX", "1m") == []
    assert feed.get_bars("NFLX", "1m") == []


def test_disk_round_trip_survives_a_restart(tmp_path):
    first = BarCache(str(tmp_path))
    bars = _bars(close=123.0)
    fetched_at = time.time() - 3600
    first.put("SPY", "15m", bars, fetched_at=fetched_at)

    second = BarCache(str(tmp_path))
    entry = second.get("SPY", "15m")
    assert entry is not None
    assert entry.bars == bars
    assert entry.fetched_at == fetched_at


def test_corrupt_cache_file_is_ignored_and_set_aside(tmp_path):
    cache = BarCache(str(tmp_path))
    path = tmp_path / "bars" / "SPY_1m.json"
    path.write_text("{not json", encoding="utf-8")

    assert cache.get("SPY", "1m") is None
    assert not path.exists()
    assert (tmp_path / "bars" / "SPY_1m.corrupt").exists()


def test_ancient_cache_files_are_pruned(tmp_path):
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    old = bars_dir / "OLD_1m.json"
    old.write_text(json.dumps({"fetched_at": 0, "bars": []}), encoding="utf-8")
    ancient = time.time() - 40 * 24 * 3600
    os.utime(old, (ancient, ancient))

    BarCache(str(tmp_path))
    assert not old.exists()


def test_ram_cache_is_still_bounded(make_feed):
    feed = make_feed()
    for i in range(MAX_CACHE_ENTRIES + 5):
        feed._cache_put(f"SYM{i}", "1m", _bars())
    assert sum(len(tfs) for tfs in feed.multi_tf_bars.values()) == MAX_CACHE_ENTRIES
