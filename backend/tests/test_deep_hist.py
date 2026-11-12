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

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.engine.alpaca_market_feed import (
    ALPACA_DEEP_HIST_TIMEFRAMES,
    AlpacaMarketFeedService,
)
from app.models.trading import Bar

ET = ZoneInfo("America/New_York")

NOW = int(datetime.now(ET).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
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
    # Clamped to the ceiling: period1 may not precede now-59d.
    assert yahoo.call_args.kwargs["period1"] >= NOW - CEILING
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
    assert yahoo.call_args.kwargs["period1"] >= NOW - 7 * 86400


def test_intl_symbol_never_routes_to_alpaca(feed):
    """The archive is US-only; HK/JP/CN pages keep the Yahoo clamp (the chart
    shows its truthful 2-month edge instead of pretending to more)."""
    before = NOW - 90 * 86400
    with patch.object(feed, "_fetch_alpaca_hist") as alpaca, \
         patch.object(YahooFinanceAdapter, "fetch_bars", return_value=[]) as yahoo:
        feed._fetch_hist_segment("0700.HK", "15m", "15m<h%d" % before, before)
    alpaca.assert_not_called()
    assert yahoo.call_args.kwargs["period1"] >= NOW - CEILING


def test_alpaca_page_keeps_has_more_contract(feed):
    """A window deeper than one page is trimmed to page_size so
    X-Bars-Has-More stays a function of the *contract*, not of Alpaca's
    HTTP page size."""
    before = NOW - 90 * 86400
    many = _bars(list(range(before - 2500 * 900, before, 900)))  # 2500 x 15m
    with patch.object(feed, "_fetch_alpaca_hist", return_value=many):
        out = feed._fetch_hist_segment("QQQ", "15m", "15m<h%d" % before, before)
    assert len(out) == 2000
    assert feed._has_more(out, 2000) is True


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
