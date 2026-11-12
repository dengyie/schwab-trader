"""Service-level wiring of the intraday volume reconciliation.

The authority map must come from the cached 1d series plus a *consolidated*
daily_stats entry — never the IEX snapshot fallback — and both Yahoo fetch
paths (fresh window and historical segment) must run bars through it.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.engine.alpaca_market_feed import AlpacaMarketFeedService
from app.models.trading import Bar

ET = ZoneInfo("America/New_York")

# 09:30–11:30 venue-local, 15-minute steps (the measured inflated window).
SESSION_TIMES = [int(datetime.now(ET).replace(hour=9, minute=30, second=0,
                                              microsecond=0).timestamp())
                 + i * 900 for i in range(9)]
INFLATED_VOLS = [5_947_000, 1_970_000, 1_830_000, 11_920_000, 52_530_000,
                 14_660_000, 65_140_000, 67_810_000, 17_940_000]
QQQ_DAILY = 21_000_000.0

TODAY = str(datetime.now(ET).date())
YDAY = str(datetime.now(ET).date() - timedelta(days=1))


def _bars(times, vols):
    return [Bar(time=t, open=680.0, high=686.0, low=679.0, close=685.0, volume=v)
            for t, v in zip(times, vols)]


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync",
                        lambda self: None)
    monkeypatch.setattr(AlpacaMarketFeedService, "refresh_daily_stats",
                        lambda self, *a, **k: None)
    return AlpacaMarketFeedService(initial_symbols=["QQQ"], api_key="k",
                                   api_secret="s")


def _consolidated_today(feed):
    feed.daily_stats["QQQ"] = {"volume": QQQ_DAILY, "consolidated": 1.0}


def test_authority_builds_from_cached_1d_and_consolidated_stats(feed):
    yday_ts = int(datetime.now(ET).replace(hour=14, minute=30, second=0,
                                           microsecond=0).timestamp()) - 86400
    feed._cache_put("QQQ", "1d", [Bar(time=yday_ts, open=680.0, high=686.0,
                                      low=679.0, close=685.0,
                                      volume=20_000_000.0)])
    _consolidated_today(feed)

    authority = feed._volume_authority("QQQ")
    assert authority[YDAY] == 20_000_000.0  # from the cached 1d series
    assert authority[TODAY] == QQQ_DAILY    # fresher stats win for today


def test_iex_fallback_stats_never_become_authority(feed):
    # consolidated absent → the Alpaca single-venue snapshot (~2% of tape).
    feed.daily_stats["QQQ"] = {"volume": 400_000.0}
    assert feed._volume_authority("QQQ") == {}


def test_fetch_upstream_scales_inflated_bars(feed, monkeypatch):
    bars = _bars(SESSION_TIMES, INFLATED_VOLS)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars",
                        lambda *a, **k: [b.model_copy() for b in bars])
    _consolidated_today(feed)

    out = feed._fetch_upstream("QQQ", "15m")
    assert abs(sum(b.volume for b in out) - QQQ_DAILY) <= len(out) / 2
    # The cache holds the reconciled series too.
    cached = feed._bar_cache.get("QQQ", "15m")
    assert abs(sum(b.volume for b in cached.bars) - QQQ_DAILY) <= len(cached.bars) / 2


def test_fetch_upstream_leaves_honest_bars_alone(feed, monkeypatch):
    vols = [v // 18 for v in INFLATED_VOLS]
    bars = _bars(SESSION_TIMES, vols)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)
    _consolidated_today(feed)

    out = feed._fetch_upstream("QQQ", "15m")
    assert [b.volume for b in out] == vols


def test_fetch_upstream_skips_1d_even_when_inflated(feed, monkeypatch):
    bars = _bars(SESSION_TIMES, INFLATED_VOLS)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)
    _consolidated_today(feed)

    out = feed._fetch_upstream("QQQ", "1d")
    assert [b.volume for b in out] == INFLATED_VOLS


def test_fetch_upstream_without_authority_is_passthrough(feed, monkeypatch):
    bars = _bars(SESSION_TIMES, INFLATED_VOLS)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)

    out = feed._fetch_upstream("QQQ", "15m")
    assert [b.volume for b in out] == INFLATED_VOLS


def test_fetch_hist_segment_scales_inflated_bars(feed, monkeypatch):
    bars = _bars(SESSION_TIMES, INFLATED_VOLS)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars",
                        lambda *a, **k: [b.model_copy() for b in bars])
    _consolidated_today(feed)

    before = SESSION_TIMES[-1] + 900
    out = feed._fetch_hist_segment("QQQ", "15m", "15m<%d" % before, before)
    assert abs(sum(b.volume for b in out) - QQQ_DAILY) <= len(out) / 2
