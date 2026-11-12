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


def test_authority_prefers_the_larger_of_today_report(feed):
    # A session total only grows. Intraday the stats entry is fresher and
    # larger than the cached 1d bar; after the close a stale pre-close stats
    # entry must not drag the 1d bar's final figure down — in both directions
    # the larger of the two is the better authority.
    day_ts = int(datetime.now(ET).replace(hour=14, minute=30, second=0,
                                           microsecond=0).timestamp())
    feed._cache_put("QQQ", "1d", [Bar(time=day_ts, open=680.0, high=686.0,
                                      low=679.0, close=685.0,
                                      volume=12_000_000.0)])
    feed.daily_stats["QQQ"] = {"volume": 15_000_000.0, "consolidated": 1.0}
    assert feed._volume_authority("QQQ")[TODAY] == 15_000_000.0

    # Post-close: the 1d cache refetched the final figure while stats froze
    # at its pre-close value.
    feed._cache_put("QQQ", "1d", [Bar(time=day_ts, open=680.0, high=686.0,
                                      low=679.0, close=685.0,
                                      volume=21_000_000.0)])
    assert feed._volume_authority("QQQ")[TODAY] == 21_000_000.0


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


def test_fetch_upstream_scales_aggregated_4h(feed, monkeypatch):
    # 4h is aggregated from 1h inside the adapter; the day sum lands on one
    # fat bar and must still be reconciled on the fresh-window path.
    bars = [Bar(time=SESSION_TIMES[0], open=680.0, high=686.0, low=679.0,
                close=685.0, volume=352_400_000.0)]
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)
    _consolidated_today(feed)

    out = feed._fetch_upstream("QQQ", "4h")
    assert out[0].volume == QQQ_DAILY


def test_stale_1d_cache_still_anchors_authority(feed, monkeypatch):
    # A stale 1d series is still the per-day truth for past days (a close
    # does not move); freshness must not gate the authority. Today's running
    # total comes from stats instead.
    old_ts = int(datetime.now(ET).replace(hour=14, minute=30, second=0,
                                          microsecond=0).timestamp()) - 86400
    feed._cache_put("QQQ", "1d", [Bar(time=old_ts, open=680.0, high=686.0,
                                      low=679.0, close=685.0,
                                      volume=20_000_000.0)],
                    ts=0.0)  # fetched_at 0 → stale by any TTL
    _consolidated_today(feed)

    authority = feed._volume_authority("QQQ")
    assert authority[YDAY] == 20_000_000.0
    assert authority[TODAY] == QQQ_DAILY


def test_unregistered_symbol_is_passthrough(feed, monkeypatch):
    # A symbol with no 1d series and no daily_stats carries no authority —
    # its bars must be passed through untouched (market_of falls back to US).
    bars = _bars(SESSION_TIMES, INFLATED_VOLS)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)

    assert feed._volume_authority("NOTAMARKET") == {}
    out = feed._fetch_upstream("NOTAMARKET", "15m")
    assert [b.volume for b in out] == INFLATED_VOLS
