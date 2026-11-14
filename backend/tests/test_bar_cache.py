"""
Tests for the bar cache: staleness policy, single-flight, failure cooldown and
disk persistence.

The contract these pin is the one the charts depend on: a request never waits
on upstream when any data exists, a stale chart is refreshed exactly once no
matter how many callers ask, and a restart does not start cold.
"""
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone

import pytest

from app.adapters import defeatbeta_adapter
from app.adapters.alpaca_adapter import AlpacaDataAdapter
from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.core.timeframes import WHOLE_WINDOW_SPAN_SECONDS
from app.engine.alpaca_market_feed import MAX_CACHE_ENTRIES, AlpacaMarketFeedService
from app.engine.bar_cache import DISK_FORMAT_VERSION, BarCache
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


def test_old_format_cache_file_is_ignored_and_set_aside(tmp_path):
    """A disk file written under an older window contract must not survive a
    deploy as a cache hit.

    Measured production failure (2026-11-14): the 15m fresh window moved from
    Yahoo's range=1mo (~31d, 599 bars) to the 59d epoch window, and the nine
    default symbols that the prewarm loop does not cover served their old
    31d file for days — stale-while-revalidate handed it to the first read
    after every restart, and nobody else hit those keys to trigger the
    refresh. A format marker turns such files into a miss.
    """
    bars_dir = tmp_path / "bars"
    old = bars_dir / "MSFT_15m.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    # A v1-shaped payload: right schema, no "v" marker.
    old.write_text(json.dumps({
        "symbol": "MSFT", "timeframe": "15m", "fetched_at": time.time() - 120,
        "bars": [[1000 + i * 900, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(3)],
    }), encoding="utf-8")

    cache = BarCache(str(tmp_path))
    assert cache.get("MSFT", "15m") is None  # not served from the stale shape
    assert not old.exists()
    assert (bars_dir / "MSFT_15m.oldformat").exists()

    # And the first real write re-seals the file under the current version.
    cache.put("MSFT", "15m", _bars(), fetched_at=time.time())
    payload = json.loads((bars_dir / "MSFT_15m.json").read_text(encoding="utf-8"))
    assert payload["v"] == DISK_FORMAT_VERSION


def test_ancient_cache_files_are_pruned(tmp_path):
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    old = bars_dir / "OLD_1m.json"
    old.write_text(json.dumps({"fetched_at": 0, "bars": []}), encoding="utf-8")
    ancient = time.time() - 40 * 24 * 3600
    os.utime(old, (ancient, ancient))

    BarCache(str(tmp_path))
    assert not old.exists()


def test_disk_write_is_fsync_free(tmp_path, monkeypatch):
    """2026-11-14 outage regression pin: the deep-hist walk wrote one fsynced
    page a second on top of a warm swap — load 10.6 on 2 cores with 3% CPU
    (pure D-state), box wedged 40+ minutes. These payloads are a cache; the
    same-filesystem rename is atomic and a lost tail of immutable history
    just re-fetches. fsync must therefore never appear on the write path."""
    cache = BarCache(str(tmp_path))
    calls = []
    real_fsync = os.fsync
    def spy_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr("app.engine.bar_cache.os.fsync", spy_fsync)

    cache.put("SPY", "15m", _bars(), fetched_at=time.time())
    assert calls == [], "cache writes must not fsync"


def test_prune_cap_scales_down_on_small_disk(tmp_path, monkeypatch):
    """The 4000 cap assumes a big disk; on the 1GB-RAM GCP e2 with a 30GB
    standard PD the rebuild cost of a huge archive is what wedged the box.
    The cap must taper with free disk instead of being a hard constant."""
    import app.engine.bar_cache as bc

    from collections import namedtuple
    disk_usage_result = namedtuple("usage", "total used free")
    # 1GB free: 15% ≈ 160MB ≈ ~1,800 files — above the floor, tapered.
    fake = lambda _: disk_usage_result(
        total=30 * 2**30, used=29 * 2**30, free=1 * 2**30)
    monkeypatch.setattr(bc.shutil, "disk_usage", fake)
    resolved = bc._resolve_max_disk_files()
    assert resolved == max(bc.MAX_DISK_FILES_FLOOR, int((1 * 2**30 * 0.15) / 90_000))
    assert resolved < bc.MAX_DISK_FILES_CEILING, "1GB free must taper below the ceiling"

    # A starved disk tapers all the way to the floor.
    fake_tiny = lambda _: disk_usage_result(
        total=30 * 2**30, used=30 * 2**30 - 200 * 2**20, free=200 * 2**20)
    monkeypatch.setattr(bc.shutil, "disk_usage", fake_tiny)
    assert bc._resolve_max_disk_files() == bc.MAX_DISK_FILES_FLOOR

    fake_big = lambda _: disk_usage_result(
        total=400 * 2**30, used=100 * 2**30, free=300 * 2**30)
    monkeypatch.setattr(bc.shutil, "disk_usage", fake_big)
    assert bc._resolve_max_disk_files() == bc.MAX_DISK_FILES_CEILING


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
    # cursor 2027 sits far above the IEX archive floor (2016): even a short
    # page has more history below it (the first page is clamped to Yahoo's
    # 59d span and can never fill — length alone would strand the chart).
    assert has_more is True
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
    assert has_more is True, "cursor above the archive floor ⇒ more below"


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
    """Yahoo-path empty ABOVE the floor falls through to the archive; when
    the archive also answers empty (both sources exhausted — a symbol
    younger than the window) the emptiness IS the real terminal edge: cached,
    has_more=0. An archive *failure* instead cool downs without caching
    (covered in test_deep_hist)."""
    feed = make_feed()
    cursor = 1_800_000_000

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    monkeypatch.setattr(feed, "_fetch_alpaca_hist", lambda *a, **k: [])

    bars, fetched_at, has_more = feed.get_historical_segment("SPY", "15m", before=cursor)
    assert bars == [] and has_more is False
    seg_tf = BarCache.hist_timeframe("15m", cursor)
    cached = feed._bar_cache.get("SPY", seg_tf)
    assert cached is not None and cached.bars == []
    # Cached: a second request must not hit upstream again.
    bars2, _, has_more2 = feed.get_historical_segment("SPY", "15m", before=cursor)
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


def test_hist_segment_terminal_empty_survives_a_restart(tmp_path):
    """A stored empty segment IS a cache entry, not a miss (F4, 2026-11-14):
    the writer recorded a terminal fact (archive floor / young symbol), and
    rejecting it on disk-load made every reboot re-hit upstream — routing the
    answer through the 503 cooldown path mid-outage instead of the cached 200.
    (The fresh-window path never writes empties: _fetch_upstream returns None
    without putting, so allowing empty loads cannot resurrect stale windows.)"""
    cache = BarCache(directory=str(tmp_path))
    tf = cache.hist_timeframe("15m", 1_800_000_000)
    cache.put("SPY", tf, [], fetched_at=time.time())

    reopened = BarCache(directory=str(tmp_path))
    entry = reopened.get("SPY", tf)
    assert entry is not None
    assert entry.bars == []
    assert entry.fetched_at > 0
    assert reopened.is_fresh(entry, tf) is True


def test_fresh_window_empty_is_still_not_written(tmp_path):
    """The other half of the F4 contract: a fresh-window upstream miss must
    not persist an empty file — only segment paths ever write empties, which
    is what makes the empty-survives-reboot load above safe."""
    first = BarCache(str(tmp_path))
    # _fetch_upstream's guard is in the feed; at the cache level pin it by
    # never calling put() for a fresh window without bars.
    assert (tmp_path / "bars" / "SPY_15m.json").exists() is False


def test_daily_window_falls_back_to_defeatbeta(make_feed, monkeypatch):
    """Yahoo answering the whole-window daily request with nothing must not
    leave the daily chart blank: it may be the rate limit, not the listing's
    birth. DefeatBeta's 30y US dailies fill the window instead."""
    feed = make_feed()
    deep = _hist_bars(1_700_000_000, 12, step=86400)  # 12 daily bars

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter.fetch_daily_history",
        lambda sym: (deep, time.time()),
    )

    bars, fetched_at = feed.get_bars_with_meta("SPY", "1d")
    assert [b.time for b in bars] == [b.time for b in deep]
    assert fetched_at > 0
    # The fallback result is cached under the normal key, so the next read
    # (before Yahoo recovers) does not re-scan anything.
    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter.fetch_daily_history",
        lambda sym: (_ for _ in ()).throw(AssertionError("must be cached")),
    )
    assert feed.get_bars("SPY", "1d") == deep


def test_daily_window_defeatbeta_failure_stays_empty_and_cools(make_feed, monkeypatch):
    """When the fallback is also unreachable, the daily key stays unserved —
    but defeatbeta's own failure cooldown keeps the next click from paying
    another dataset scan."""
    feed = make_feed()
    scans = []

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    def dead_scan(sym):
        scans.append(sym)
        raise ConnectionError("dataset unreachable")
    # Patch the scan, not fetch_daily_history: the cooldown under test lives
    # inside fetch_daily_history itself.
    monkeypatch.setattr("app.adapters.defeatbeta_adapter._load_daily_bars", dead_scan)

    assert feed.get_bars("SPY", "1d") == []
    assert feed.get_bars("SPY", "1d") == []
    assert scans == ["SPY"], "the failure cooldown absorbs the retry"


def test_daily_window_fallback_only_for_us_daily(make_feed, monkeypatch):
    """The gate has two halves: intraday windows and non-US symbols must
    never touch defeatbeta — it is US daily-only by construction."""
    feed = make_feed()
    calls = []

    def spy_fallback(sym):
        calls.append(sym)
        return None
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    monkeypatch.setattr("app.adapters.defeatbeta_adapter.fetch_daily_history", spy_fallback)

    # Intraday empty window on a US symbol: true edge (or Yahoo throttle —
    # but defeatbeta cannot answer intraday, so it must not be consulted).
    feed.get_bars("SPY", "15m")
    assert calls == []

    # Non-US daily: the dataset has no HK bars, so even a daily window
    # must not scan it.
    class _HK:
        code = "HK"
    monkeypatch.setattr("app.engine.alpaca_market_feed.market_of", lambda sym: _HK())
    feed.get_bars("SPY", "1d")
    assert calls == []


def test_whole_window_fallback_trims_to_yahoo_window(make_feed, monkeypatch):
    """The fallback must not hand the chart a wider window than Yahoo's own
    whole-window fetch (1d=5y, 1wk=10y): when Yahoo recovers, the chart would
    visibly shrink back. 1mo is unbounded like its Yahoo source, and a
    listing dead for the whole span keeps its full real history — the trim
    must never blank the only data available."""
    feed = make_feed()
    now = time.time()
    cutoff_1d = now - WHOLE_WINDOW_SPAN_SECONDS["1d"]
    recent = Bar(time=int(now) - 100 * 86400, open=10, high=11, low=9, close=10, volume=1)
    ancient = Bar(time=int(cutoff_1d) - 100 * 86400, open=10, high=11, low=9, close=10, volume=1)

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter.fetch_history",
        lambda sym, tf: ([ancient, recent], time.time()),
    )
    bars, _ = feed.get_bars_with_meta("SPY", "1d")
    assert [b.time for b in bars] == [recent.time], "bars older than the 5y window are trimmed"

    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter.fetch_history",
        lambda sym, tf: ([ancient], time.time()),
    )
    bars, _ = feed.get_bars_with_meta("DEAD", "1d")
    assert [b.time for b in bars] == [ancient.time], "a full-window-dead listing keeps its history"

    # 1mo is unbounded on both sides: nothing may be dropped.
    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter.fetch_history",
        lambda sym, tf: ([ancient, recent], time.time()),
    )
    bars, _ = feed.get_bars_with_meta("DEAD2", "1mo")
    assert [b.time for b in bars] == [ancient.time, recent.time]

    # 1wk trims to its own span (10y): the 5y-old bar survives, the
    # decade-old one does not.
    cutoff_1wk = now - WHOLE_WINDOW_SPAN_SECONDS["1wk"]
    decade_old = Bar(time=int(cutoff_1wk) - 100 * 86400, open=10, high=11, low=9, close=10, volume=1)
    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter.fetch_history",
        lambda sym, tf: ([decade_old, ancient, recent], time.time()),
    )
    bars, _ = feed.get_bars_with_meta("DEAD3", "1wk")
    assert [b.time for b in bars] == [ancient.time, recent.time]


def test_weekly_monthly_fallback_aggregates_from_dailies(make_feed, monkeypatch):
    """Yahoo answering a weekly/monthly whole-window request with nothing
    must not leave those charts blank either: the dataset has no coarser
    bars, so the fallback aggregates its dailies with Yahoo's conventions."""
    feed = make_feed()
    daily = defeatbeta_adapter._rows_to_bars([
        # Two weeks apart, each with three sessions.
        ("2026-11-02", 10.0, 12.0, 9.0, 11.0, 100),   # Mon
        ("2026-11-03", 11.0, 13.0, 10.0, 12.0, 200),  # Tue
        ("2026-11-04", 12.0, 14.0, 11.0, 13.0, 300),  # Wed
        ("2026-11-09", 13.0, 15.0, 12.0, 14.0, 400),  # Mon next week
    ])
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.adapters.defeatbeta_adapter._load_daily_bars",
        lambda sym: daily,
    )

    bars, fetched_at = feed.get_bars_with_meta("SPY", "1wk")
    assert fetched_at > 0
    assert len(bars) == 2
    first, second = bars
    # Stamped midnight ET of the bucket's Monday (04:00Z/05:00Z by DST).
    assert datetime.fromtimestamp(first.time, tz=timezone.utc).isoformat().startswith("2026-11-02T05:00")
    assert (first.open, first.high, first.low, first.close, first.volume) == (10.0, 14.0, 9.0, 13.0, 600.0)
    assert (second.open, second.close, second.volume) == (13.0, 14.0, 400.0)

    bars, _ = feed.get_bars_with_meta("SPY", "1mo")
    assert len(bars) == 1
    # One monthly bucket stamped midnight ET of the 1st — Nov 1 2026 midnight
    # is still EDT (the clocks fall back at 02:00 local), hence 04:00Z.
    assert datetime.fromtimestamp(bars[0].time, tz=timezone.utc).isoformat().startswith("2026-11-01T04:00")
    assert bars[0].close == 14.0 and bars[0].volume == 1000.0


def test_fetch_history_rejects_unsupported_timeframes():
    """The adapter is the single source of what it can answer; intraday keys
    must never reach the dataset (it has no intraday bars)."""
    for tf in ("15m", "1h", "1m", "4h", "bogus"):
        assert defeatbeta_adapter.fetch_history("SPY", tf) is None


def test_defeatbeta_scan_does_not_block_other_symbols(monkeypatch):
    """The dataset scan is seconds of network IO; it must run outside the
    cache lock, or one hung upstream serializes every other symbol's
    fallback (and the health probe's stats() call) behind it."""
    started = threading.Event()
    release = threading.Event()

    def scan(sym):
        if sym == "SLOW":
            started.set()
            release.wait(5.0)
        return []

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: [])
    monkeypatch.setattr("app.adapters.defeatbeta_adapter._load_daily_bars", scan)

    in_flight = threading.Thread(target=defeatbeta_adapter.fetch_daily_history, args=("SLOW",))
    in_flight.start()
    assert started.wait(5.0), "the slow scan should have started"

    t0 = time.time()
    result = defeatbeta_adapter.fetch_daily_history("FAST")
    elapsed = time.time() - t0
    release.set()
    in_flight.join(5.0)

    assert result is not None
    assert elapsed < 1.0, "another symbol's in-flight scan must not block this one"


def test_defeatbeta_rows_skip_partial_and_stamp_session_open():
    """One malformed row must not fail the whole symbol's history (a partial
    row is not a candle), and the daily stamp must match Yahoo's session-open
    convention — 9:30 America/New_York, prod-measured 13:30 UTC in summer /
    14:30 UTC in winter — so the two sources are interchangeable."""
    rows = [
        ("2025-07-01", 10.0, 11.0, 9.5, 10.5, 100),
        ("2025-07-02", 10.0, None, 9.5, 10.5, 100),
        ("2026-11-10", 20.0, 21.0, 19.5, 20.5, 200),
    ]
    bars = defeatbeta_adapter._rows_to_bars(rows)
    assert len(bars) == 2
    assert datetime.fromtimestamp(bars[0].time, tz=timezone.utc).isoformat() == "2025-07-01T13:30:00+00:00"
    assert datetime.fromtimestamp(bars[1].time, tz=timezone.utc).isoformat() == "2026-11-10T14:30:00+00:00"


def test_aggregation_buckets_across_year_boundary():
    """ISO-week keys must survive the year boundary: the week containing New
    Year folds under its own ISO year (2026-W53, and 2026 has 53 weeks
    because its Jan 1 is a Thursday), monthly buckets fold by calendar
    month, and every stamp stays the bucket anchor's midnight ET."""
    daily = defeatbeta_adapter._rows_to_bars([
        ("2026-12-28", 10.0, 11.0, 9.0, 10.5, 100),  # Mon, ISO 2026-W53
        ("2026-12-31", 10.5, 12.0, 10.0, 11.5, 200),  # Thu, same ISO week
        ("2027-01-04", 11.5, 13.0, 11.0, 12.5, 400),  # Mon, ISO 2027-W01
    ])
    weekly = defeatbeta_adapter._aggregate_weekly(daily)
    assert len(weekly) == 2
    assert datetime.fromtimestamp(weekly[0].time, tz=timezone.utc).isoformat().startswith("2026-12-28T05:00")
    assert datetime.fromtimestamp(weekly[1].time, tz=timezone.utc).isoformat().startswith("2027-01-04T05:00")
    assert (weekly[0].open, weekly[0].high, weekly[0].close, weekly[0].volume) == (10.0, 12.0, 11.5, 300.0)

    monthly = defeatbeta_adapter._aggregate_monthly(daily)
    assert len(monthly) == 2
    assert datetime.fromtimestamp(monthly[0].time, tz=timezone.utc).isoformat().startswith("2026-12-01T05:00")
    assert datetime.fromtimestamp(monthly[1].time, tz=timezone.utc).isoformat().startswith("2027-01-01T05:00")
    assert (monthly[1].open, monthly[1].close, monthly[1].volume) == (11.5, 12.5, 400.0)


def test_scan_timeout_arms_cooldown_and_reports_error(monkeypatch):
    """A hung dataset scan must be bounded: the watchdog turns it into a
    cooldown plus a health-visible error instead of pinning the caller
    thread on a dead upstream."""
    import app.adapters.defeatbeta_adapter as db

    def hung_scan(sym):
        time.sleep(5.0)
        return []

    monkeypatch.setattr("app.adapters.defeatbeta_adapter._load_daily_bars", hung_scan)
    monkeypatch.setattr(db, "_SCAN_TIMEOUT_SECONDS", 0.1)

    t0 = time.time()
    assert db.fetch_daily_history("SPY") is None
    assert time.time() - t0 < 2.0, "the watchdog bounds the wait"
    stats = db.stats()
    assert stats["last_error"] and "exceeded" in stats["last_error"]

    # The armed cooldown absorbs the immediate retry without re-scanning.
    t0 = time.time()
    assert db.fetch_daily_history("SPY") is None
    assert time.time() - t0 < 0.5, "cooldown short-circuits before the scan"


def test_hist_segment_stays_out_of_ram_when_disk_backed(tmp_path):
    """Disk-only contract (2026-11-14): the deep-hist prewarm promoted ~110
    pages per (symbol, tf) into the RAM LRU and pushed the 1GB e2 instance
    to 596MB RSS mid-walk. With a disk directory, a segment put() records
    only its index (fetched_at) — the bars themselves must not live in RAM.
    """
    cache = BarCache(directory=str(tmp_path))
    tf = cache.hist_timeframe("15m", 1_800_000_000)
    seg = _hist_bars(1_800_000_000 - 900, 500)
    cache.put("SPY", tf, seg)

    assert "SPY" not in cache.bars, "segment bars must not occupy the RAM LRU"
    assert cache.fetched_at_of("SPY", tf) > 0, "index keeps the freshness ts"
    # The disk copy is the cache: get() pages it back on demand.
    entry = cache.get("SPY", tf)
    assert entry is not None and len(entry.bars) == 500
    assert entry.bars == seg


def test_hist_segment_disk_cache_evicts_oldest_files_when_over_cap(tmp_path, monkeypatch):
    """With segments disk-only, the global file cap must run on the write
    path (the walker emits a page a second) — not just at boot. Writing
    more files than the cap deletes the oldest mtimes.
    """
    monkeypatch.setattr("app.engine.bar_cache._resolve_max_disk_files", lambda: 2)
    cache = BarCache(directory=str(tmp_path))
    for i in range(4):
        tf = cache.hist_timeframe("15m", 1_800_000_000 - i * 900 * 2000)
        cache.put("SPY", tf, _hist_bars(1_800_000_000 - i * 900 * 2000 - 900, 2))
        time.sleep(0.01)  # distinct mtimes for the eviction order

    files = sorted(p.name for p in (tmp_path / "bars").glob("*.json"))
    assert len(files) == 2, f"cap=2 must evict down to 2, got {len(files)}"
    # The newest two survive.
    assert "SPY_15m_h1800000000.json" not in files, "oldest cursor evicted"
    assert "SPY_15m_h1796400000.json" in files and "SPY_15m_h1794600000.json" in files


def test_index_eviction_never_drops_a_fresh_window_ts(tmp_path):
    """P2 pin (2026-11-14 review): fresh windows share one _order with
    hist index keys. The index evictor used to pop from the LRU head
    blindly — dropping a fresh window's fetched_at while its bars stayed
    resident, turning every reader into a pointless background refresh.
    Only index-only (hist) keys are evictable there.
    """
    cache = BarCache(directory=str(tmp_path), max_entries=2)
    fresh = _bars()
    cache.put("SPY", "15m", fresh)
    # A walker burst big enough to blow the index cap (2*8=16).
    for i in range(20):
        cache.put("QQQ", BarCache.hist_timeframe("15m", 1000 + i), [])
    entry = cache.get("SPY", "15m")
    assert entry is not None and len(entry.bars) == len(fresh)
    assert entry.fetched_at > 0, (
        "index eviction must not strip a resident fresh window's ts"
    )
