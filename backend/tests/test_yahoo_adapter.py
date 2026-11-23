"""
Unit tests for YahooFinanceAdapter, 4h aggregation, client lifecycle, and multi-timeframe fallback.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import time
from app.models.trading import Bar
from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.core.timeframes import FRESH_WINDOW_SPAN_SECONDS, TIMEFRAME_SPAN_DAYS
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


def test_yahoo_aggregate_bars_4h_survives_a_dropped_bar():
    """2026-11-23 review P2: the old sequence-based bucketing shifted EVERY
    later bucket of the day after one dropped bar — a dropped 10:30 made
    bucket0 span 09:30-13:30 and cascaded the rest. Time-anchored bucketing
    (fetch_bars passes the market session open) contains the damage to its
    own bucket."""
    t = _et_ts(2026, 11, 2, 9, 30)
    bars = [
        Bar(time=t + 0 * 3600, open=100.0, high=105.0, low=99.0, close=104.0, volume=1000.0),
        # 10:30 print missing (Yahoo null)
        Bar(time=t + 2 * 3600, open=107.0, high=109.0, low=106.0, close=108.0, volume=800.0),
        Bar(time=t + 3 * 3600, open=108.0, high=110.0, low=107.0, close=109.5, volume=1200.0),
        Bar(time=t + 4 * 3600, open=110.0, high=112.0, low=109.0, close=111.0, volume=2000.0),
    ]
    aggregated = YahooFinanceAdapter._aggregate_bars(
        bars, bucket_seconds=14400, session_open_seconds=9 * 3600 + 30 * 60,
    )
    assert len(aggregated) == 2
    assert aggregated[0].time == t
    assert aggregated[0].volume == 3000.0  # 09:30/11:30/12:30 in bucket 0
    assert aggregated[1].time == t + 4 * 3600  # 13:30 starts bucket 1 on TIME
    assert aggregated[1].volume == 2000.0


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


def test_long_horizon_timeframes_are_mapped_and_registered():
    """Weekly / monthly candles are what let a chart reach back years.

    The adapter map and the API whitelist must never drift: a timeframe the
    chart offers but the registry lacks would 400 at the route, and one the
    registry has but the adapter lacks would silently fetch nothing.
    """
    from app.core.timeframes import TIMEFRAME_SECONDS, YAHOO_TIMEFRAME_MAP

    assert set(YAHOO_TIMEFRAME_MAP) == set(TIMEFRAME_SECONDS)

    # Weekly reaches a decade; monthly reaches the whole listed history —
    # via explicit epoch bounds, because Yahoo coarsens range=max server-side
    # (measured 2026-11-12: interval=1mo + range=max returns 4 bars/year,
    # quarterly buckets mislabeled as monthly, while every explicit range —
    # 5y/10y/20y/40y — and period1=0 stay true monthly).
    assert YAHOO_TIMEFRAME_MAP["1wk"] == ("1wk", "10y")
    assert YAHOO_TIMEFRAME_MAP["1mo"] == ("1mo", None)
    assert YAHOO_TIMEFRAME_MAP["1d"] == ("1d", "5y")

    # The case trap: the API lowercases input, so a client sending "1M" lands
    # on the 1-minute key. Monthly is spelled "1mo" and never collides.
    assert {"1m", "1wk", "1mo"} <= set(TIMEFRAME_SECONDS)


def test_fallback_contract_maps_stay_in_sync():
    """The fallback's three contract structures must agree: what the adapter
    can answer (SUPPORTED_TIMEFRAMES), what window it trims to
    (WHOLE_WINDOW_SPAN_SECONDS), and what window Yahoo serves
    (YAHOO_TIMEFRAME_MAP). One key added to one map without the others would
    trim silently wrong or not at all — the maps are bound here, not just by
    adjacent comments."""
    from app.adapters import defeatbeta_adapter
    from app.core.timeframes import WHOLE_WINDOW_SPAN_SECONDS, YAHOO_TIMEFRAME_MAP

    keys = {"1d", "1wk", "1mo"}
    assert set(defeatbeta_adapter.SUPPORTED_TIMEFRAMES) == keys
    assert set(WHOLE_WINDOW_SPAN_SECONDS) == keys
    for key in keys:
        # Unbounded span ⇔ unbounded Yahoo range (1mo whole history).
        assert (WHOLE_WINDOW_SPAN_SECONDS[key] is None) == (YAHOO_TIMEFRAME_MAP[key][1] is None)


def test_bar_cache_is_bounded(monkeypatch):
    feed = _make_feed(monkeypatch)
    for i in range(MAX_CACHE_ENTRIES + 20):
        feed._cache_put(f"SYM{i}", "1m", [Bar(time=i, open=1, high=1, low=1, close=1, volume=1)])
    total = sum(len(tfs) for tfs in feed.multi_tf_bars.values())
    assert total == MAX_CACHE_ENTRIES


# -- IP throttling observability --------------------------------------------
class _RateLimitedClient:
    """Yahoo answering HTTP 429 — the explicit throttle."""
    status_code = 429
    def get(self, *a, **k):
        return self
    def raise_for_status(self):
        import httpx
        req = httpx.Request("GET", "https://query1.finance.yahoo.com/x")
        import httpx as _h
        self._resp = _h.Response(429, request=req)
        raise _h.HTTPStatusError("429", request=req, response=self._resp)
    def json(self):
        return {}

class _EdgeDisguiseClient:
    """Yahoo answering HTTP 200 with the 'Edge: too many requests' chart.error
    disguise (yfinance #2284/#2297/#2721)."""
    status_code = 200
    def get(self, *a, **k):
        return self
    def raise_for_status(self):
        pass
    def json(self):
        return {"chart": {"result": None, "error": {
            "code": "Bad Request", "description": "Edge: Too Many Requests"}}}

class _DelistedClient:
    """A genuinely dead symbol: HTTP 200, chart.error 'Not Found'."""
    status_code = 200
    def get(self, *a, **k):
        return self
    def raise_for_status(self):
        pass
    def json(self):
        return {"chart": {"result": None, "error": {
            "code": "Not Found", "description": "No data found, symbol may be delisted"}}}


class _OkChartClient:
    """HTTP 200 with one daily bar, for the explicit-window (period1/period2) path."""
    status_code = 200
    def get(self, *a, **k):
        return self
    def raise_for_status(self):
        pass
    def json(self):
        return {"chart": {"result": [{
            "meta": {},
            "timestamp": [1794371400],
            "indicators": {"quote": [{
                "open": [100.0], "high": [101.0], "low": [99.0],
                "close": [100.5], "volume": [123.0],
            }]},
        }], "error": None}}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    YahooFinanceAdapter._rate_limit = {"count": 0, "consecutive": 0, "last_ts": 0.0}
    YahooFinanceAdapter._http_errors = {"count": 0, "consecutive": 0, "last_ts": 0.0}
    yield
    YahooFinanceAdapter._rate_limit = {"count": 0, "consecutive": 0, "last_ts": 0.0}
    YahooFinanceAdapter._http_errors = {"count": 0, "consecutive": 0, "last_ts": 0.0}


def test_http_429_is_counted():
    stats_before = YahooFinanceAdapter.rate_limit_stats()["count"]
    YahooFinanceAdapter.fetch_meta("0700.HK", client=_RateLimitedClient())
    YahooFinanceAdapter.fetch_bars("0700.HK", timeframe="15m", client=_RateLimitedClient())
    stats = YahooFinanceAdapter.rate_limit_stats()
    assert stats["count"] == stats_before + 2
    assert stats["consecutive"] == 2
    assert stats["last_ts"] > 0


def test_edge_disguise_is_counted_but_delisted_is_not():
    YahooFinanceAdapter.fetch_bars("0700.HK", timeframe="15m", client=_EdgeDisguiseClient())
    assert YahooFinanceAdapter.rate_limit_stats()["count"] == 1, "the Edge disguise is a throttle signal"

    # A real delisting must not pollute the throttle counter: the chart edge
    # and an ops alarm would otherwise fire for a dead ticker.
    YahooFinanceAdapter.fetch_bars("DEAD.SYM", timeframe="15m", client=_DelistedClient())
    stats = YahooFinanceAdapter.rate_limit_stats()
    assert stats["count"] == 1
    assert stats["consecutive"] == 0, "a non-throttle response resets the streak"


def test_success_resets_consecutive_streak():
    YahooFinanceAdapter.fetch_bars("0700.HK", timeframe="15m", client=_EdgeDisguiseClient())
    assert YahooFinanceAdapter.rate_limit_stats()["consecutive"] == 1

    YahooFinanceAdapter.fetch_bars("0700.HK", timeframe="15m",
                                   client=_DelistedClient())
    assert YahooFinanceAdapter.rate_limit_stats()["consecutive"] == 0
    assert YahooFinanceAdapter.rate_limit_stats()["count"] == 1, "total counts history, streak counts the fire"


class _ServerErrorClient:
    """Yahoo answering HTTP 500 — a non-throttle HTTP contract break."""
    status_code = 500
    def get(self, *a, **k):
        return self
    def raise_for_status(self):
        import httpx
        req = httpx.Request("GET", "https://query1.finance.yahoo.com/x")
        resp = httpx.Response(500, request=req)
        raise httpx.HTTPStatusError("500", request=req, response=resp)
    def json(self):
        return {}


def test_http_status_error_is_counted_and_success_resets_it():
    """The fresh-window spans are a *measured* contract (59d ceiling). The day
    Yahoo tightens it, every 5m/15m fresh fetch 422s at once — that drift must
    surface in health, not die in a log line (F3, review of f59ea03)."""
    YahooFinanceAdapter.fetch_bars("SPY", "15m", client=_ServerErrorClient())
    stats = YahooFinanceAdapter.http_error_stats()
    assert stats["count"] == 1 and stats["consecutive"] == 1 and stats["last_ts"] > 0

    # A 429 belongs to the throttle counter, not this one.
    YahooFinanceAdapter.fetch_bars("0700.HK", timeframe="15m", client=_RateLimitedClient())
    assert YahooFinanceAdapter.http_error_stats()["count"] == 1
    assert YahooFinanceAdapter.rate_limit_stats()["count"] == 1

    YahooFinanceAdapter.fetch_bars("SPY", "15m", client=_ServerErrorClient())
    assert YahooFinanceAdapter.http_error_stats()["consecutive"] == 2

    # Empty-but-200 is a data question (delisting), not a contract break;
    # and a success resets the streak.
    YahooFinanceAdapter.fetch_bars("DEAD.SYM", timeframe="15m", client=_DelistedClient())
    assert YahooFinanceAdapter.http_error_stats()["count"] == 2
    assert YahooFinanceAdapter.http_error_stats()["consecutive"] == 0


def test_fetch_bars_explicit_period1_defaults_period2_to_now():
    """The segment path may pass only ``period1``; the ``period2=None`` branch
    evaluates ``time.time()`` while building params. A missing module import
    there used to NameError into the generic except and silently return [] —
    which the defeatbeta fallback would then read as "Yahoo has nothing"."""
    bars = YahooFinanceAdapter.fetch_bars(
        "SPY", "1d", client=_OkChartClient(), period1=0, period2=None)
    assert len(bars) == 1
    assert bars[0].close == 100.5


def test_whole_history_timeframes_fetch_via_explicit_periods():
    """range=max silently degrades the interval server-side (KO 1mo+max
    returned 4 bars/year — quarterly mislabeled as monthly), so a whole-
    history key must request an explicit epoch window instead of "max"."""
    captured = {}

    class _CaptureClient:
        status_code = 200
        def get(self, url, params=None, **k):
            captured.update(params or {})
            return self
        def raise_for_status(self):
            pass
        def json(self):
            return _OkChartClient().json()

    bars = YahooFinanceAdapter.fetch_bars("SPY", "1mo", client=_CaptureClient())
    assert len(bars) == 1
    assert "range" not in captured, "range=max coarsens the interval; whole history uses epochs"
    assert captured.get("period1") == 0
    assert captured.get("period2", 0) > time.time() - 60

    # Explicit-range keys must keep the range form, untouched.
    captured.clear()
    YahooFinanceAdapter.fetch_bars("SPY", "1wk", client=_CaptureClient())
    assert captured.get("range") == "10y" and "period1" not in captured


def test_intraday_seed_windows_fetch_the_full_ceiling_span():
    """Yahoo's range enum undershoots the 15m/5m coverage ceiling ("1mo" ≈
    31d, "3mo" 422s, measured 59d works — 2026-11-13), so those fresh windows
    must be explicit epoch windows spanning the whole available history. The
    chart's first paint then covers ~2 months with no truncated seed."""
    captured = {}

    class _CaptureClient:
        status_code = 200
        def get(self, url, params=None, **k):
            captured.update(params or {})
            return self
        def raise_for_status(self):
            pass
        def json(self):
            return _OkChartClient().json()

    bars = YahooFinanceAdapter.fetch_bars("SPY", "15m", client=_CaptureClient())
    assert len(bars) == 1
    assert "range" not in captured
    # Literal, not the constant: the point is to catch the constant itself
    # drifting past the measured ceiling (60d already 422s).
    assert captured["period2"] - captured["period1"] == 59 * 86400
    assert FRESH_WINDOW_SPAN_SECONDS == {"5m": 59 * 86400, "15m": 59 * 86400}
    assert captured["period2"] > time.time() - 60

    # Other range keys are untouched.
    captured.clear()
    YahooFinanceAdapter.fetch_bars("SPY", "1h", client=_CaptureClient())
    assert captured.get("range") == "6mo" and "period1" not in captured


def test_span_days_match_the_measured_yahoo_ceiling():
    # The segment clamp (oldest_allowed) must sit at the real upstream
    # ceiling: 60 whole days is rejected with HTTP 422, 59 is the largest
    # accepted span. A 60d clamp would 422 any segment whose page reaches
    # near "now".
    assert TIMEFRAME_SPAN_DAYS["15m"] == 59
    assert TIMEFRAME_SPAN_DAYS["5m"] == 59


def test_yahoo_symbol_dot_class_share_maps_to_yahoo_convention():
    # BRK.B resolved through markets.py (dot input accepted) must still hit
    # Yahoo's BRK-B URL — before the mapping the dotted form passed
    # resolution, then 404'd on every fetch while the virtual broker rested
    # an order no quote would ever reach (2026-11-23 second review P2-A).
    assert YahooFinanceAdapter._yahoo_symbol("BRK.B") == "BRK-B"
    assert YahooFinanceAdapter._yahoo_symbol("brk.b") == "BRK-B"
    assert YahooFinanceAdapter._yahoo_symbol("BRK-B") == "BRK-B"  # already Yahoo form
    assert YahooFinanceAdapter._yahoo_symbol("7203.T") == "7203.T"  # suffix dot is Yahoo's own
    assert YahooFinanceAdapter._yahoo_symbol("RDS/A") == "RDS-A"  # legacy slash form
