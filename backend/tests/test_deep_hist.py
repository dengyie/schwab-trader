"""Deep history routing: US 15m/5m segments past Yahoo's ceiling fall back
to the Alpaca IEX archive.

The chart's "see all the data" ask: Yahoo caps 15m/5m at 59 days, but the
IEX free feed serves those bars back to 2020-07 (measured 2026-11-13). The
segment fetch must therefore (a) stop clamping period1 for those pages,
(b) route to Alpaca only when the window actually crosses the ceiling and
only for US symbols, and (c) keep the page contract (trim to page_size,
X-Bars-Has-More) intact.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest
import time

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.engine.alpaca_market_feed import (
    ALPACA_DEEP_HIST_TIMEFRAMES,
    AlpacaMarketFeedService,
    DEEP_HIST_FLOOR,
)
from app.models.trading import Bar

ET = ZoneInfo("America/New_York")

# Anchor for cursors ("40d back") — any stable recent epoch works. It must
# NOT be used to assert clamp boundaries: `_fetch_hist_segment` clamps
# against time.time(), and this ET-noon anchor drifts ±hours from it
# depending on when the suite runs (the 2026-11-14 CI red was exactly that:
# real time had passed the day's ET noon, so NOW-59d exceeded
# time.time()-59d and a `>=` clamp assertion blew up). Boundary asserts use
# _NOW_RT below — same source as the implementation.
NOW = int(datetime.now(ET).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
_NOW_RT = int(time.time())
CEILING = 59 * 86400


def _bars(times, vols=None, base=680.0):
    vols = vols or [1_000.0 + i for i in range(len(times))]
    return [Bar(time=t, open=base, high=base + 5, low=base - 5, close=base + 1, volume=v)
            for t, v in zip(times, vols)]


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync",
                        lambda self: None)
    monkeypatch.setattr(AlpacaMarketFeedService, "refresh_daily_stats",
                        lambda self, *a, **k: None)
    AlpacaMarketFeedService._deep_hist_errors.update(
        {"count": 0, "consecutive": 0, "last_ts": 0.0})
    return AlpacaMarketFeedService(initial_symbols=["QQQ"], api_key="k",
                                   api_secret="s")


def test_timeframe_set():
    assert ALPACA_DEEP_HIST_TIMEFRAMES == {"5m", "15m"}


def test_deep_window_routes_to_alpaca_without_clamp(feed):
    """A window crossing the 59d ceiling goes to Alpaca with the *unclamped*
    epoch window; Yahoo must not be asked for a page it cannot serve."""
    before = NOW - 90 * 86400          # 90d back: inside IEX-only territory
    deep = _bars([before - 3600, before - 1800])
    with patch.object(feed, "_fetch_alpaca_hist", return_value=deep) as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars") as yahoo:
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    yahoo.assert_not_called()
    assert alpaca.call_args.args[2:4] == (max(0, before - 366 * 86400), before)
    assert [b.time for b in out] == [b.time for b in deep]


def test_window_within_ceiling_stays_on_yahoo(feed):
    """A page fully inside Yahoo's coverage never touches the archive."""
    before = NOW - 40 * 86400          # 40d back: Yahoo has this
    ybars = _bars([before - 3600, before - 1800])
    with patch.object(feed, "_fetch_alpaca_hist") as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars", return_value=ybars) as yahoo:
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    alpaca.assert_not_called()
    assert yahoo.call_args.kwargs["period2"] == before
    # Clamped to the ceiling: period1 may not precede the live 59d edge
    # (clamped against time.time(), not the ET-noon anchor).
    assert yahoo.call_args.kwargs["period1"] >= _NOW_RT - CEILING
    assert [b.time for b in out] == [b.time for b in ybars]


def test_straddling_window_uses_unclamped_period1_on_alpaca(feed):
    """A page whose window *starts* inside Yahoo's range but ends before the
    ceiling crosses into the archive: the whole window goes to Alpaca
    unclamped (one source, no stitched seams)."""
    before = NOW - 65 * 86400
    deep = _bars([before - 3600])
    with patch.object(feed, "_fetch_alpaca_hist", return_value=deep) as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars") as yahoo:
        feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    yahoo.assert_not_called()
    assert alpaca.call_args.args[2] == max(0, before - 366 * 86400)


def test_1m_never_routes_to_alpaca(feed):
    """1m's upstream is 7d on both sources — the clamp must survive there."""
    before = NOW - 90 * 86400
    with patch.object(feed, "_fetch_alpaca_hist") as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]) as yahoo:
        feed._fetch_hist_segment("QQQ", "1m", "1m<h%d" % before, before)
    alpaca.assert_not_called()
    assert yahoo.call_args.kwargs["period1"] >= _NOW_RT - 7 * 86400


def test_intl_symbol_never_routes_to_alpaca(feed):
    """The archive is US-only; HK/JP/CN pages keep the Yahoo clamp (the chart
    shows its truthful 2-month edge instead of pretending to more)."""
    before = NOW - 90 * 86400
    with patch.object(feed, "_fetch_alpaca_hist") as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]) as yahoo:
        feed._fetch_hist_segment("0700.HK", "15m", "15m<h%d" % before, before)
    alpaca.assert_not_called()
    assert yahoo.call_args.kwargs["period1"] >= _NOW_RT - CEILING


def test_alpaca_page_keeps_has_more_contract(feed):
    """A window deeper than one page is trimmed to page_size so
    X-Bars-Has-More stays a function of the *contract*, not of Alpaca's
    HTTP page size."""
    before = NOW - 90 * 86400
    many = _bars(list(range(before - 2500 * 900, before, 900)))  # 2500 x 15m
    with patch.object(feed, "_fetch_alpaca_hist", return_value=many):
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    assert len(out) == 2000
    assert feed._has_more(out, 2000, before, "15m", "QQQ") is True


def test_sliding_edge_cursor_falls_through_to_archive(feed):
    """The client's first prefill page uses the fresh window's oldest bar as
    its cursor — ABOVE Yahoo's ceiling, where the sliding edge answers empty
    (measured 2026-11-14: cursor 09-16 13:30, Yahoo's oldest slid past it).
    Yahoo empty at such a cursor must fall through to the IEX archive (which
    serves it fine) instead of cooling down and stranding the client's
    prefill at page one."""
    before = NOW - 40 * 86400  # above the ceiling, below nothing
    with patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]), \
         patch.object(feed, "_fetch_alpaca_hist",
                      return_value=_bars([before - 900 * i for i in range(1, 2100)])) as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]):
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    assert len(out) == 2000, "archive page trimmed to the contract"
    assert alpaca.call_args.args[2:4] == (before - 366 * 86400, before)
    assert feed._bar_cache.get("QQQ", "15m<h%d" % before) is not None
    # And a young symbol (archive empty too) is a real terminal edge: cached
    # emptiness, not a cooldown.
    with patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]), \
         patch.object(feed, "_fetch_alpaca_hist", return_value=[]):
        out2 = feed._fetch_hist_segment("COIN", "15m", "15m<h%d" % before, before)
    assert out2 == []
    assert feed._bar_cache.get("COIN", "15m<h%d" % before) is not None
    assert feed._bar_cache.get("COIN", "15m<h%d" % before).bars == []


def test_first_short_page_above_floor_still_has_more(feed):
    """The FIRST page's window is clamped to Yahoo's 59d span (~1119 bars for
    15m), so a length-only has_more could never fire there — the chart
    stranded at the seed edge (measured 2026-11-14). A short page at a cursor
    above the IEX floor must still report more below; at/below the floor it
    must not."""
    before = NOW - 40 * 86400  # first-page cursor inside Yahoo's span
    short = _bars([before - 900 * i for i in range(1, 1119)])
    assert len(short) < 2000
    assert feed._has_more(short, 2000, before, "15m", "QQQ") is True
    # At the floor: terminal.
    floor_page = _bars([DEEP_HIST_FLOOR + 900 * i for i in range(1, 1119)])
    assert feed._has_more(floor_page, 2000, DEEP_HIST_FLOOR, "15m", "QQQ") is False
    # Non-deep timeframe / intl symbol keep the length-only contract.
    assert feed._has_more(short, 2000, before, "1h", "QQQ") is False
    assert feed._has_more(short, 2000, before, "15m", "0700.HK") is False


def test_fetch_alpaca_hist_pages_to_window_end(feed, monkeypatch):
    """next_page_token chains until exhaustion — past the naive 10-page
    bound (measured reality: a response holds ~750 bars, not limit=10000,
    so a 366d window needs more than 10 pages) — and the gathered window
    must REACH the cursor: the newest bar abuts ``before`` within one
    bucket, otherwise no later page could ever fill the hole."""

    class _Resp:
        status_code = 200
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    class _Client:
        def __init__(self, maker):
            self.maker = maker
            self.calls = []
        def get(self, url, params=None, **k):
            self.calls.append(dict(params or {}))
            return _Resp(self.maker(len(self.calls)))

    class _Ctx:
        def __init__(self, inner): self.inner = inner
        def __enter__(self): return self.inner
        def __exit__(self, *a): return False

    before = NOW - 90 * 86400
    BUCKET = 900
    N_PAGES = 12  # crosses the old fixed cap of 10
    PER_PAGE = 700  # measured IEX reality: far below limit=10000
    t_end = before - BUCKET  # newest bar the chain can return
    total = N_PAGES * PER_PAGE
    t_start = t_end - (total - 1) * BUCKET

    def page(n):  # 1-based
        times = range(t_start + (n - 1) * PER_PAGE * BUCKET,
                      t_start + n * PER_PAGE * BUCKET, BUCKET)
        payload = {"bars": {"QQQ": [
            {"t": datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
             "o": 680, "h": 685, "l": 675, "c": 681, "v": 100}
            for t in times]}}
        if n < N_PAGES:
            payload["next_page_token"] = f"T{n + 1}"
        return payload

    client = _Client(page)
    monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client",
                        lambda *a, **k: _Ctx(client))

    out = feed._fetch_alpaca_hist("QQQ", "15m", t_start, before)
    assert len(out) == total
    assert all(b.time < before for b in out)
    # Window continuity: the newest gathered bar abuts the cursor.
    assert max(b.time for b in out) == t_end
    # The chain walked every page via the token.
    assert len(client.calls) == N_PAGES
    for i, call in enumerate(client.calls[1:], start=2):
        assert call.get("page_token") == f"T{i}"


def test_fetch_alpaca_hist_http_error_raises(feed, monkeypatch):
    """A non-200 anywhere in the chain raises (caller cool-downs instead of
    caching an empty segment as a fake physical boundary) and is counted."""

    class _Resp:
        status_code = 500
        def json(self):
            return {}

    class _Client:
        def get(self, url, params=None, **k):
            return _Resp()

    class _Ctx:
        def __init__(self, inner): self.inner = inner
        def __enter__(self): return self.inner
        def __exit__(self, *a): return False

    before = NOW - 90 * 86400
    monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client",
                        lambda *a, **k: _Ctx(_Client()))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        feed._fetch_alpaca_hist("QQQ", "15m", before - 86400, before)
    stats = AlpacaMarketFeedService.deep_hist_error_stats()
    assert stats["count"] == 1 and stats["consecutive"] == 1
    # And the failure must NOT be cached as an empty segment: it reaches
    # the caller as an exception, which cool-downs the key instead.
    with patch.object(feed, "_fetch_alpaca_hist",
                      side_effect=RuntimeError("HTTP 500")), \
         patch.object(YahooFinanceAdapter, "fetch_bars") as yahoo:
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    yahoo.assert_not_called()
    assert out is None
    assert feed._bar_cache.get("QQQ", "15m<h%d" % before) is None


def test_deep_hist_success_resets_consecutive(feed, monkeypatch):
    AlpacaMarketFeedService.record_deep_hist_error(429)
    AlpacaMarketFeedService.record_deep_hist_error(429)
    assert AlpacaMarketFeedService.deep_hist_error_stats()["consecutive"] == 2

    class _Resp:
        status_code = 200
        def json(self):
            return {"bars": {"QQQ": []}}  # empty page, still a success

    class _Client:
        def get(self, url, params=None, **k):
            return _Resp()

    class _Ctx:
        def __init__(self, inner): self.inner = inner
        def __enter__(self): return self.inner
        def __exit__(self, *a): return False

    before = NOW - 90 * 86400
    monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client",
                        lambda *a, **k: _Ctx(_Client()))
    feed._fetch_alpaca_hist("QQQ", "15m", before - 86400, before)
    stats = AlpacaMarketFeedService.deep_hist_error_stats()
    assert stats["count"] == 2 and stats["consecutive"] == 0


def test_deep_hist_walk_symbol_stops_at_floor(feed, monkeypatch):
    """The pre-run walks pages via get_historical_segment until the cursor
    stops moving backwards or has_more turns 0 — never a fixed page count."""
    import time as _time
    feed._running = True

    calls = []

    def fake_seg(sym, tf, before):
        calls.append(before)
        bars, has_more = pages[len(calls) - 1]
        return bars, _time.time(), has_more

    pages = [
        (_bars([800, 900]), True),   # oldest 800, more below
        (_bars([700, 750]), False),  # floor at 700
    ]

    monkeypatch.setattr(feed, "get_historical_segment", fake_seg)
    import asyncio
    asyncio.run(feed._deep_hist_walk_symbol("QQQ", "15m"))
    assert calls == [calls[0], 800]  # first cursor is the init value (cache empty)


def test_deep_hist_walk_symbol_gives_up_after_three_empty_pages(feed, monkeypatch):
    """An upstream outage answers empties: after 3 the walk stops instead of
    spinning forever. The cursor sits BELOW the 59d ceiling (already in IEX
    territory), so the sliding-edge jump does not apply."""
    import asyncio
    import time as _time
    feed._running = True

    calls = []
    deep_cursor = int(_time.time()) - 90 * 86400
    def fake_seg(sym, tf, before):
        calls.append(before)
        return [], _time.time(), True
    monkeypatch.setattr(feed, "get_historical_segment", fake_seg)
    # Speed through the outage backoff (capture the real sleep first).
    real_sleep = asyncio.sleep
    async def fast_sleep(_s):
        await real_sleep(0)
    monkeypatch.setattr("app.engine.alpaca_market_feed.asyncio.sleep", fast_sleep)
    # Start the walk already below the ceiling: the init cursor is the
    # fresh-window oldest bar, so seed one deep bar in the cache.
    feed._bar_cache.put("QQQ", "15m", _bars([deep_cursor]), _time.time())
    asyncio.run(feed._deep_hist_walk_symbol("QQQ", "15m"))
    assert len(calls) == 3  # three empties then give-up
    assert all(c <= deep_cursor for c in calls)


def test_yahoo_segment_empty_window_is_not_cached_as_boundary(feed, monkeypatch):
    """A Yahoo-path page that answers empty at a cursor INSIDE the 59d
    ceiling is the sliding edge, not a terminal boundary: it falls through to
    the archive, and if that fetch FAILS (HTTP error) the key cool downs
    (retryable) instead of caching an empty segment that strands the chart at
    the seed edge. Measured 2026-11-14: cursor 09-16 13:30 asked Yahoo for
    bars it had already slid past."""
    before = NOW - 40 * 86400  # fully inside Yahoo coverage
    with patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]), \
         patch.object(feed, "_fetch_alpaca_hist",
                      side_effect=RuntimeError("HTTP 429")):
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    assert out is None, "archive failure surfaces as None (retryable), not an empty"
    seg_tf = "15m<h%d" % before
    # No fake boundary: the empty segment is NOT a cached entry, and the key
    # is cooling so a retry waits instead of hammering upstream.
    assert feed._bar_cache.get("QQQ", seg_tf) is None
    assert feed._bar_cache.is_cooling("QQQ", seg_tf) is True


def test_deep_hist_walk_jumps_past_yahoo_ceiling_on_empty_page(feed, monkeypatch):
    """When the walker's first cursor sits inside the ceiling and answers
    empty, the walk jumps the cursor a day past the ceiling (routing the
    next page to Alpaca) instead of declaring the floor."""
    import asyncio
    import time as _time

    feed._running = True
    now = int(_time.time())
    calls = []

    def fake_seg(sym, tf, before):
        calls.append(before)
        if len(calls) == 1:
            return [], _time.time(), False  # empty inside the ceiling
        # Second page (routed to the archive past the ceiling): a floor.
        return _bars([now - 100 * 86400]), _time.time(), False

    monkeypatch.setattr(feed, "get_historical_segment", fake_seg)

    real_sleep = asyncio.sleep
    async def fast_sleep(_s):
        await real_sleep(0)
    monkeypatch.setattr("app.engine.alpaca_market_feed.asyncio.sleep", fast_sleep)

    asyncio.run(feed._deep_hist_walk_symbol("QQQ", "15m"))

    ceiling = now - 59 * 86400
    # First call at the init cursor (now, cache empty); second call jumped a
    # day past the ceiling.
    assert len(calls) == 2
    assert calls[0] >= ceiling
    assert calls[1] == ceiling - 86400


def test_deep_hist_walk_parks_on_temporary_empty_then_fills(feed, monkeypatch):
    """F2 (2026-11-14): `([], 0.0, False)` is "couldn't get it right now" —
    _fetch_hist_segment converts upstream failures to exactly this tuple, so
    the old code's 3-failure give-up stranded a hole mid-archive (and its
    except-park branch was unreachable). The walker must park (25s) and
    retry, and resume the walk on the first real answer. The cursor must
    never move during a park."""
    import asyncio
    import time as _time
    feed._running = True

    calls = []
    pause = feed.DEEP_HIST_RETRY_PAUSE
    slept = []
    real_sleep = asyncio.sleep
    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr("app.engine.alpaca_market_feed.asyncio.sleep", fake_sleep)

    def fake_seg(sym, tf, before):
        calls.append(before)
        if len(calls) <= 2:
            return [], 0.0, False  # upstream outage (cooldown / failure tuple)
        return _bars([before - 900, before - 1800]), _time.time(), False

    monkeypatch.setattr(feed, "get_historical_segment", fake_seg)
    asyncio.run(feed._deep_hist_walk_symbol("QQQ", "15m"))

    assert len(calls) == 3
    assert slept == [pause, pause], "each miss parks exactly one retry pause"
    assert all(c == calls[0] for c in calls), "cursor must not move on a miss"


def test_deep_hist_walk_gives_up_after_five_temporary_misses(feed, monkeypatch):
    """Persistent upstream trouble is not a race: after 5 consecutive
    fetched_at==0.0 misses the walk stops for this symbol (disk resume and
    the next hourly pass pick it up), instead of spinning forever."""
    import asyncio
    import time as _time
    feed._running = True

    calls = []
    def fake_seg(sym, tf, before):
        calls.append(before)
        return [], 0.0, False
    monkeypatch.setattr(feed, "get_historical_segment", fake_seg)
    real_sleep = asyncio.sleep
    async def fast_sleep(_s):
        await real_sleep(0)
    monkeypatch.setattr("app.engine.alpaca_market_feed.asyncio.sleep", fast_sleep)

    asyncio.run(feed._deep_hist_walk_symbol("QQQ", "15m"))
    assert len(calls) == 5  # five misses then give-up


def test_deep_hist_prewarm_loop_waits_out_the_boot_grace(feed, monkeypatch):
    """2026-11-14 outage regression pin: the walk's fetch+write loop landing
    on top of the boot preload while swap was warm wedged the 1GB e2 box
    (load 10.6 on 2 cores, agent dead 40+ min). The loop must sleep out
    DEEP_HIST_BOOT_GRACE before its first page."""
    import asyncio
    feed._running = True

    slept = []
    real_sleep = asyncio.sleep
    async def fake_sleep(s):
        slept.append(s)
        if s >= feed.DEEP_HIST_BOOT_GRACE:
            feed._running = False  # stop the loop after the grace sleep
        await real_sleep(0)

    async def never_walk(sym, tf):
        raise AssertionError("walk must not start before the boot grace")

    monkeypatch.setattr("app.engine.alpaca_market_feed.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(feed, "_deep_hist_walk_symbol", never_walk)
    asyncio.run(feed._deep_hist_prewarm_loop())
    assert slept[:2] == [feed.DEEP_HIST_BOOT_GRACE, 3600.0] or \
        feed.DEEP_HIST_BOOT_GRACE in slept[:1]


def test_deep_hist_symbols_covers_every_market_catalog(feed):
    """The prewarm walk must reach every market's chart, not just the US
    list (2026-11-15: HK/JP/CN charts were first-touch crawls — the walker
    skipped any symbol whose market wasn't US). US tracked symbols lead;
    each market's catalog follows; duplicates collapse; the page pace, not
    a shorter list, is what protects the 1GB box's IO.
    """
    syms = feed._deep_hist_symbols()
    assert "QQQ" in syms                       # tracked US symbol
    assert syms.index("QQQ") < syms.index("0700.HK")  # US leads
    for expected in ("0700.HK", "7203.T", "600519.SS"):
        assert expected in syms, f"{expected} (HK/JP/CN catalog) must be walked"
    assert len(syms) == len(set(syms))         # no duplicate walk slots


def test_deep_hist_prewarm_loop_walks_non_us_symbols(feed, monkeypatch):
    """The loop body must not re-add the US-only skip it used to have:
    a HK catalog symbol passed to the walker is walked, not filtered."""
    import asyncio
    feed._running = True

    walked = []
    real_sleep = asyncio.sleep
    async def fake_sleep(s):
        # Pace sleeps yield immediately; the loop is stopped from the walker
        # itself (a boot-grace stop here would exit before the first walk).
        await real_sleep(0)

    async def fake_walk(sym, tf):
        walked.append(sym)
        if sym == "0700.HK":
            feed._running = False  # stop once a non-US symbol has been reached

    monkeypatch.setattr("app.engine.alpaca_market_feed.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(feed, "_deep_hist_walk_symbol", fake_walk)
    asyncio.run(feed._deep_hist_prewarm_loop())
    assert "0700.HK" in walked, "HK catalog symbol must reach the walker"
