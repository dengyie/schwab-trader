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

from app.adapters.alpaca_adapter import AlpacaDataAdapter
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
    """An Alpaca feed with no network at construction.

    ``Client`` is swapped for one that refuses to issue requests rather than
    the preload method being stubbed out: a test must still be able to call
    the real preload and see what it does.
    """
    class _OfflineClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

        def get(self, *a, **k):
            raise AssertionError("tests must not reach the network")

    monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client", _OfflineClient)
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


def test_disk_load_cannot_regress_a_newer_ram_entry(tmp_path):
    """The disk read happens outside the lock, so a put() can land while it is
    in flight. Promoting the just-read file then would drag fetched_at (and
    the ETag) backwards; a RAM entry that is at least as new must win."""
    cache = BarCache(str(tmp_path))
    fresh, stale = _bars(close=200.0), _bars(close=100.0)
    now = time.time()

    cache.put("SPY", "1d", fresh, fetched_at=now)
    # Simulate the interleaving: the disk still holds the older series.
    cache._write_to_disk("SPY", "1d", stale, now - 3600)

    entry = cache._load_from_disk("SPY", "1d")
    assert entry is not None
    assert entry.fetched_at == now
    assert entry.bars == fresh
    assert cache.fetched_at_of("SPY", "1d") == now


def test_disk_load_still_promotes_when_ram_is_empty(tmp_path):
    """The guard must not block the legitimate path: after an eviction (RAM
    empty, disk newer than nothing) the disk entry is promoted as before."""
    cache = BarCache(str(tmp_path))
    bars = _bars(close=55.0)
    fetched_at = time.time() - 120
    cache.put("SPY", "1d", bars, fetched_at=fetched_at)
    cache.bars["SPY"].pop("1d")  # evicted from RAM only; disk remains
    cache.fetched_at["SPY"].pop("1d")

    entry = cache._load_from_disk("SPY", "1d")
    assert entry is not None
    assert entry.bars == bars
    assert entry.fetched_at == fetched_at
    assert cache.fetched_at_of("SPY", "1d") == fetched_at


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


def test_requested_bars_are_written_to_disk(make_feed, monkeypatch, tmp_path):
    """The point of the cache: what a request fetched is still there next boot."""
    cache_root = tmp_path
    feed = make_feed(cache_dir=str(cache_root))
    bars = _bars(close=88.0)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)

    assert feed.get_bars("SPY", "1m") == bars
    assert (cache_root / "bars" / "SPY_1m.json").exists()

    # A new process on the same directory answers from disk, with no upstream call.
    def boom(*a, **k):
        raise AssertionError("restart must not refetch a cached 1m series")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)
    reborn = make_feed(cache_dir=str(cache_root))
    assert reborn.get_bars("SPY", "1m") == bars


def test_iex_preload_bars_do_not_poison_the_cache(make_feed, monkeypatch, tmp_path):
    """The boot preload reads the IEX venue, not the consolidated tape.

    Those bars are a fallback for this process only; persisting them would make
    the next boot's first 1m read a "hit" on a candle series the chart never
    draws on.
    """
    feed = make_feed(cache_dir=str(tmp_path))
    venue_bars = _bars(close=1.0)

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            if "/bars" in url:
                return _Resp()
            raise RuntimeError("no quote in this test")

    monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client", _Client)
    monkeypatch.setattr(
        AlpacaDataAdapter, "parse_rest_bars_response",
        staticmethod(lambda payload, symbol=None: venue_bars),
    )
    feed._preload_historical_bars_sync()

    assert feed.symbols == {"SPY"}
    assert feed.historical_bars["SPY"] == venue_bars
    assert not (tmp_path / "bars" / "SPY_1m.json").exists()
    assert "1m" not in feed.multi_tf_bars.get("SPY", {})


# -- Historical segments (paging back in time) ------------------------------
def _hist_bars(start_ts, count, step=900, close=100.0):
    """Ascending bars ending strictly before a cursor."""
    return [
        Bar(time=start_ts + i * step, open=close, high=close + 1, low=close - 1,
            close=close + (i % 7), volume=10.0)
        for i in range(count)
    ]


def test_hist_segment_fetches_strictly_older_bars(make_feed, monkeypatch):
    feed = make_feed()
    cursor = 1_800_000_000
    seg = _hist_bars(cursor - 5 * 900, 4)
    seen = {}

    def fetch(sym, timeframe="15m", client=None, period1=None, period2=None):
        seen["period1"], seen["period2"] = period1, period2
        return seg

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    bars, fetched_at, has_more = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert [b.time for b in bars] == [b.time for b in seg]
    assert all(b.time < cursor for b in bars)
    assert fetched_at > 0
    assert has_more is False, "a short page means the physical edge was reached"
    assert seen["period2"] == cursor, "the cursor must cap the window's end"


def test_hist_segment_is_served_from_cache_without_upstream(make_feed, monkeypatch):
    feed = make_feed()
    cursor = 1_800_000_000
    seg = _hist_bars(cursor - 5 * 900, 4)
    monkeypatch.setattr(
        YahooFinanceAdapter, "fetch_bars",
        lambda *a, **k: seg,
    )
    feed.get_historical_segment("SPY", "15m", before=cursor)

    calls = []
    def fetch(sym, *a, **k):
        calls.append(sym)
        return seg
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    bars, fetched_at, has_more = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert calls == [], "an immutable segment must never re-fetch while fresh"
    assert [b.time for b in bars] == [b.time for b in seg]


def test_hist_segment_cache_hit_on_us_feed(make_feed, monkeypatch):
    """The US feed mirrors the Yahoo feed's paging: a warm segment must be
    served with zero upstream calls, and a stale one must stay on disk."""
    feed = make_feed()
    cursor = 1_800_000_000
    seg = _hist_bars(cursor - 5 * 900, 4)
    monkeypatch.setattr(
        YahooFinanceAdapter, "fetch_bars",
        lambda *a, **k: seg,
    )
    feed.get_historical_segment("SPY", "15m", before=cursor)

    calls = []
    def fetch(sym, *a, **k):
        calls.append(sym)
        return seg
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    bars, fetched_at, has_more = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert calls == [], "US feed must serve a warm segment without upstream"
    assert [b.time for b in bars] == [b.time for b in seg]
    assert fetched_at > 0
    assert has_more is False


def test_hist_segment_full_page_reports_has_more(make_feed, monkeypatch):
    feed = make_feed()
    cursor = 1_800_000_000
    seg = _hist_bars(cursor - 2001 * 900, 2000)

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: seg)

    bars, _, has_more = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert len(bars) == 2000, "page is trimmed to the segment contract"
    assert has_more is True, "a full page implies history continues above it"
    assert all(b.time < cursor for b in bars)


def test_hist_segment_empty_window_is_a_cached_terminal_state(make_feed, monkeypatch):
    feed = make_feed()
    cursor = 1_800_000_000
    calls = []

    def fetch(sym, *a, **k):
        calls.append(sym)
        return []

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    bars, fetched_at, has_more = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert bars == [] and has_more is False
    # The emptiness is cached: a second page must not hit upstream again.
    bars2, _, has_more2 = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert calls == ["SPY"], "the terminal edge must be cached, not re-fetched"
    assert bars2 == [] and has_more2 is False


def test_hist_segment_failure_fails_quietly(make_feed, monkeypatch):
    feed = make_feed()

    def boom(*a, **k):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)

    bars, fetched_at, has_more = feed.get_historical_segment("SPY", "15m", before=1_800_000_000)
    assert (bars, fetched_at, has_more) == ([], 0.0, False)


def test_hist_segment_ram_cap_evicts_oldest_cursor(make_feed):
    feed = make_feed()
    base = 1_800_000_000
    for i in range(4):
        before = base - i * 900 * 2000
        feed._bar_cache.put("SPY", BarCache.hist_timeframe("15m", before), _hist_bars(before - 900, 3))

    live = [
        tf for tf in feed._bar_cache.bars["SPY"]
        if tf.startswith("15m#h")
    ]
    assert len(live) == 3, "at most MAX_HIST_SEGMENTS_PER_KEY segments stay in RAM"
    # The oldest cursor is the one dropped.
    oldest = BarCache.hist_timeframe("15m", base - 3 * 900 * 2000)
    assert oldest not in feed._bar_cache.bars["SPY"]


def test_hist_segment_disk_file_has_dedicated_name(tmp_path):
    cache = BarCache(directory=str(tmp_path))
    tf = cache.hist_timeframe("15m", 1_800_000_000)
    cache.put("SPY", tf, _hist_bars(1_800_000_000 - 900, 2))
    assert (tmp_path / "bars" / "SPY_15m_h1800000000.json").exists()
    # A fresh-window file must never be created for a segment.
    assert not (tmp_path / "bars" / "SPY_15m.json").exists()


def test_hist_segment_survives_a_restart(tmp_path):
    cache = BarCache(directory=str(tmp_path))
    tf = cache.hist_timeframe("15m", 1_800_000_000)
    seg = _hist_bars(1_800_000_000 - 3 * 900, 3)
    cache.put("SPY", tf, seg)

    reopened = BarCache(directory=str(tmp_path))
    entry = reopened.get("SPY", tf)
    assert entry is not None
    assert entry.bars == seg
