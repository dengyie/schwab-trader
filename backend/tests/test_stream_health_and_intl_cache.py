"""
Tests for the intl bar cache and the stream-health surface.

Two contracts are pinned here:

* A HK/JP/CN chart uses the same cache the US path does — persisted, served
  while stale, refreshed exactly once — and reports a real ``fetched_at`` so
  the API can answer with ETag/304 instead of ``no-store``.
* A rejected IEX session is *visible*: the health payload says the stream is
  degraded and names the quota, and the retry cadence stops shouting every
  30 seconds about a condition we cannot fix locally.
"""
import asyncio
import time

import pytest

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.engine.alpaca_market_feed import (
    AlpacaMarketFeedService,
    QUOTA_MAX_BACKOFF,
    is_quota_rejection,
)
from app.engine.yahoo_feed import YahooQuoteFeed
from app.models.trading import Bar


def _bars(close=100.0, count=3):
    return [
        Bar(time=1000 + i * 60, open=close, high=close + 1, low=close - 1,
            close=close + i, volume=10.0 * (i + 1))
        for i in range(count)
    ]


@pytest.fixture
def yahoo(monkeypatch):
    """A Yahoo feed whose bars come from ``fetch_bars`` only."""
    def _make(cache_dir=None, enabled=True):
        return YahooQuoteFeed(symbols=["0700.HK"], enabled=enabled, cache_dir=cache_dir)
    return _make


# -- intl bar cache --------------------------------------------------------
def test_intl_fresh_bars_do_not_touch_upstream(yahoo, monkeypatch):
    feed = yahoo()
    expected = _bars()
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: expected)

    first = feed.get_bars("0700.HK", "15m")
    assert first == expected

    def boom(*a, **k):
        raise AssertionError("a fresh intl series must not be refetched")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)
    assert feed.get_bars("0700.HK", "15m") == expected


def test_intl_stale_bars_are_served_immediately_and_refreshed_once(yahoo, monkeypatch):
    feed = yahoo()
    stale = _bars(close=50.0)
    feed._bar_cache.put("0700.HK", "1m", stale, fetched_at=time.time() - 3600)

    calls = []
    fresh = _bars(close=90.0)

    def fetch(sym, timeframe="15m", tz=None):
        calls.append(sym)
        time.sleep(0.05)
        return fresh

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", fetch)

    started = time.time()
    for _ in range(5):
        assert feed.get_bars("0700.HK", "1m") == stale
    assert time.time() - started < 0.5, "stale intl reads must not block"

    pool = feed._refresh_pool
    assert pool is not None
    pool.shutdown(wait=True)
    assert calls == ["0700.HK"], "five stale reads must schedule one fetch"
    assert feed.get_bars("0700.HK", "1m") == fresh


def test_intl_bars_report_fetched_at_for_http_validators(yahoo, monkeypatch):
    feed = yahoo()
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: _bars())
    bars, fetched_at = feed.get_bars_with_meta("0700.HK", "15m")
    assert bars
    assert fetched_at > 0, "a real fetch time is what makes ETag/304 possible"


def test_intl_bars_survive_a_restart(tmp_path, monkeypatch):
    bars = _bars(close=88.0)
    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", lambda *a, **k: bars)

    first = YahooQuoteFeed(symbols=["0700.HK"], enabled=True, cache_dir=str(tmp_path))
    assert first.get_bars("0700.HK", "15m") == bars

    def boom(*a, **k):
        raise AssertionError("a restart must not refetch a cached intl series")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)
    reborn = YahooQuoteFeed(symbols=["0700.HK"], enabled=True, cache_dir=str(tmp_path))
    assert reborn.get_bars("0700.HK", "15m") == bars


def test_intl_disabled_feed_never_reaches_upstream(yahoo, monkeypatch):
    feed = yahoo(enabled=False)

    def boom(*a, **k):
        raise AssertionError("a disabled feed must not call Yahoo")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)
    assert feed.get_bars("0700.HK", "15m") == []


def test_intl_disabled_feed_serves_stale_from_disk_but_does_not_refresh(yahoo, monkeypatch):
    """Disabled means 'stop fetching', not 'forget': the last series is still
    served, and the SWR path must not quietly schedule the fetch it refuses
    on the cold path."""
    feed = yahoo(enabled=False)
    stale = _bars(close=42.0)
    feed._bar_cache.put("0700.HK", "1m", stale, fetched_at=time.time() - 3600)

    def boom(*a, **k):
        raise AssertionError("a disabled feed must not refresh in the background")

    monkeypatch.setattr(YahooFinanceAdapter, "fetch_bars", boom)
    assert feed.get_bars("0700.HK", "1m") == stale
    assert feed._refresh_pool is None, "no background pool should even be created"


# -- stream health ---------------------------------------------------------
def test_quota_rejection_is_recognised():
    assert is_quota_rejection(
        "Alpaca stream auth rejected: connection limit exceeded (code=406)"
    )
    assert is_quota_rejection("code=406")
    assert not is_quota_rejection("no close frame received or sent")
    assert not is_quota_rejection("timed out during opening handshake")


def test_health_starts_idle_and_reports_degradation(monkeypatch):
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync", lambda self: None)
    monkeypatch.setattr(AlpacaMarketFeedService, "refresh_daily_stats", lambda self, *a, **k: None)
    feed = AlpacaMarketFeedService(
        initial_symbols=["SPY"], api_key="k", api_secret="s",
    )
    assert feed.stream_health()["state"] == "idle"
    assert feed.stream_health()["connected"] is False

    feed._record_stream_event("connected")
    live = feed.stream_health()
    assert live["connected"] is True
    assert live["consecutive_failures"] == 0

    feed._record_stream_event(
        "degraded",
        "Alpaca stream auth rejected: connection limit exceeded (code=406)",
    )
    degraded = feed.stream_health()
    assert degraded["connected"] is False
    assert degraded["quota_exceeded"] is True
    assert degraded["consecutive_failures"] == 1
    assert "connection limit" in degraded["last_error"]


def test_a_successful_session_clears_the_failure_count(monkeypatch):
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync", lambda self: None)
    monkeypatch.setattr(AlpacaMarketFeedService, "refresh_daily_stats", lambda self, *a, **k: None)
    feed = AlpacaMarketFeedService(initial_symbols=["SPY"], api_key="k", api_secret="s")

    for _ in range(3):
        feed._record_stream_event("degraded", "connection limit exceeded (code=406)")
    assert feed.stream_health()["consecutive_failures"] == 3

    feed._record_stream_event("connected")
    health = feed.stream_health()
    assert health["consecutive_failures"] == 0
    assert health["quota_exceeded"] is False


def test_failures_survive_the_connecting_step_of_each_retry(monkeypatch):
    """The reconnect loop starts every attempt with a ``connecting`` event.

    If that event reset the counter, a two-hour rejection storm would read as
    one failure: connecting (reset) → degraded (+1) → connecting (reset) …
    """
    monkeypatch.setattr(AlpacaMarketFeedService, "_preload_historical_bars_sync", lambda self: None)
    monkeypatch.setattr(AlpacaMarketFeedService, "refresh_daily_stats", lambda self, *a, **k: None)
    feed = AlpacaMarketFeedService(initial_symbols=["SPY"], api_key="k", api_secret="s")

    error = "Alpaca stream auth rejected: connection limit exceeded (code=406)"
    for _ in range(3):  # one full reconnect attempt, as the loop runs it
        feed._record_stream_event("connecting")
        feed._record_stream_event("degraded", error)

    health = feed.stream_health()
    assert health["state"] == "degraded"
    assert health["consecutive_failures"] == 3
    assert health["quota_exceeded"] is True

    # A failed attempt that is NOT a quota issue flips the flag back.
    feed._record_stream_event("connecting")
    feed._record_stream_event("degraded", "no close frame received or sent")
    health = feed.stream_health()
    assert health["consecutive_failures"] == 4
    assert health["quota_exceeded"] is False

    # Only the real success clears the count; stopping must not either.
    feed._record_stream_event("stopped")
    assert feed.stream_health()["consecutive_failures"] == 4
    feed._record_stream_event("connected")
    assert feed.stream_health()["consecutive_failures"] == 0


def test_quota_backoff_ceiling_is_well_above_the_network_cadence():
    # The point of the separate schedule: a condition we cannot fix must not
    # be retried at the same pace as a dropped socket.
    assert QUOTA_MAX_BACKOFF > 30.0


# -- composite feed probe --------------------------------------------------
def _feed_health(us, intl, timeout=0.2):
    import app.engine.multi_market_feed as mmf

    prev = mmf.MultiMarketFeed.HEALTH_PROBE_TIMEOUT
    mmf.MultiMarketFeed.HEALTH_PROBE_TIMEOUT = timeout
    # Run on a loop that stays open, like ASGI does. ``asyncio.run`` would also
    # wait for the default executor's hung thread during teardown, which an
    # always-alive server never does.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(mmf.MultiMarketFeed(us, intl).health())
    finally:
        loop.close()
        mmf.MultiMarketFeed.HEALTH_PROBE_TIMEOUT = prev


def test_a_feed_that_raises_is_reported_not_swallowed():
    """Yahoo living behind a dead session raises; the payload must say so."""
    class _US:
        def stream_health(self):
            return {"connected": True, "state": "connected"}

    class _Intl:
        def health(self):
            raise ConnectionResetError("yahoo session died")

    result = _feed_health(_US(), _Intl())
    assert result["us"]["connected"] is True
    assert result["intl"]["state"] == "unreachable"
    assert result["intl"]["connected"] is False
    assert "yahoo session died" in result["intl"]["error"]


def test_a_hung_feed_probe_times_out_instead_of_hanging_health():
    class _US:
        def stream_health(self):
            return {"connected": True}

    class _Intl:
        def health(self):
            time.sleep(5)
            return {"connected": True}

    started = time.time()
    result = _feed_health(_US(), _Intl(), timeout=0.1)
    assert time.time() - started < 2.0
    assert result["intl"]["state"] == "unreachable"
    assert "timed out" in result["intl"]["error"]


def test_a_feed_without_a_probe_is_named():
    class _US:
        def stream_health(self):
            return {}

    class _Intl:
        pass

    result = _feed_health(_US(), _Intl())
    assert result["intl"]["state"] == "unavailable"
    assert "health" in result["intl"]["error"]


def test_probes_run_concurrently_not_serialised():
    """Two 0.6s probes must finish together (~0.6s), not one after the other
    (1.2s): serialised probes would add up against the route's HEALTH_BUDGET
    and a pair of slow feeds would wipe the payload to a timeout error."""
    class _SlowUS:
        def stream_health(self):
            time.sleep(0.6)
            return {"connected": True}

    class _SlowIntl:
        def health(self):
            time.sleep(0.6)
            return {"watching": 1}

    started = time.time()
    result = _feed_health(_SlowUS(), _SlowIntl(), timeout=2.0)
    elapsed = time.time() - started

    assert result["us"]["connected"] is True
    assert result["intl"]["watching"] == 1
    assert elapsed < 1.0, f"probes look serialised: {elapsed:.2f}s for two 0.6s probes"


def test_health_payload_includes_feed_state():
    """``/api/health`` must name the degraded feed, not just say "ok"."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import create_api_router

    class _Feed:
        def health(self):
            return {
                "us": {
                    "state": "degraded",
                    "connected": False,
                    "quota_exceeded": True,
                    "consecutive_failures": 4,
                },
                "intl": None,
            }

    class _Broker:
        def get_fx_rates(self):
            return {"USD": 1.0}

    app = FastAPI()
    app.include_router(create_api_router(_Broker(), _Feed()))
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", "liveness must not flap on an upstream stream"
    assert body["feeds"]["us"]["quota_exceeded"] is True
    assert body["degraded"] == ["market_stream_quota_exceeded"]


def test_health_payload_omits_feeds_for_a_plain_feed():
    """A non-composite feed has nothing to report; the key must not appear."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import create_api_router

    class _Feed:
        pass

    class _Broker:
        def get_fx_rates(self):
            return {"USD": 1.0}

    app = FastAPI()
    app.include_router(create_api_router(_Broker(), _Feed()))
    body = TestClient(app).get("/api/health").json()
    assert body["status"] == "ok"
    assert "feeds" not in body


def test_health_clips_error_strings_on_the_public_route():
    """/api/health is unauthenticated; a verbose upstream exception must not
    be echoed to anonymous visitors verbatim."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import HEALTH_ERROR_CLIP, create_api_router

    long_error = "x" * (HEALTH_ERROR_CLIP + 200)

    class _Feed:
        def health(self):
            return {
                "us": {"state": "degraded", "last_error": long_error, "quota_exceeded": True},
                "intl": {"error": long_error},
            }

    class _Broker:
        def get_fx_rates(self):
            return {"USD": 1.0}

    app = FastAPI()
    app.include_router(create_api_router(_Broker(), _Feed()))
    body = TestClient(app).get("/api/health").json()
    assert len(body["feeds"]["us"]["last_error"]) <= HEALTH_ERROR_CLIP
    assert len(body["feeds"]["intl"]["error"]) <= HEALTH_ERROR_CLIP
    # The degraded flag must survive the clipping.
    assert body["degraded"] == ["market_stream_quota_exceeded"]


def test_health_probe_separates_healthy_from_empty():
    """A healthy probe ({}) is real information; it must survive the router.

    ``if not feed_health`` would treat "feed answered, all good" and "feed
    returned nothing" identically and silently drop the answer.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import create_api_router

    class _Feed:
        def health(self):
            return {}

    class _Broker:
        def get_fx_rates(self):
            return {"USD": 1.0}

    app = FastAPI()
    app.include_router(create_api_router(_Broker(), _Feed()))
    body = TestClient(app).get("/api/health").json()
    assert body["feeds"] == {}
    assert "error" not in body
    assert "degraded" not in body


def test_health_survives_a_feed_that_explodes():
    """A broken probe must not turn liveness into a 500 for the deploy gate."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import create_api_router

    class _Feed:
        def health(self):
            raise RuntimeError("probe blew up")

    class _Broker:
        def get_fx_rates(self):
            return {"USD": 1.0}

    app = FastAPI()
    app.include_router(create_api_router(_Broker(), _Feed()))
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_payload_runs_the_real_composite_probe():
    """The async composite feed must render through the router, or the
    degraded-flag wiring below it never fires in production."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import create_api_router

    from app.engine.multi_market_feed import MultiMarketFeed

    class _US:
        def stream_health(self):
            return {
                "state": "degraded",
                "connected": False,
                "quota_exceeded": True,
                "consecutive_failures": 3,
            }

    class _Intl:
        def health(self):
            return {"enabled": True, "watching": 2}

    broker_feed = MultiMarketFeed(_US(), _Intl())

    class _Broker:
        def get_fx_rates(self):
            return {"USD": 1.0}

    app = FastAPI()
    app.include_router(create_api_router(_Broker(), broker_feed))
    body = TestClient(app).get("/api/health").json()
    assert body["status"] == "ok"
    assert body["feeds"]["us"]["quota_exceeded"] is True
    assert body["feeds"]["intl"]["enabled"] is True
    assert body["degraded"] == ["market_stream_quota_exceeded"]


# -- Yahoo IP-throttle observability ----------------------------------------
def test_intl_health_reports_yahoo_rate_limit_stats(yahoo):
    """The throttle signal must reach the health payload, not die in the log.

    Yahoo throttles per exit IP and disguises 429s as "possibly delisted";
    when the VPS IP gets limited the whole intl feed goes quiet at once, so
    feeds.intl.health() is where ops looks first — and the counter must be
    there (§7.12 待办的落地).
    """
    feed = yahoo()
    assert feed.health()["rate_limit"] == {"count": 0, "consecutive": 0, "last_ts": 0.0}

    YahooFinanceAdapter.record_rate_limit("bars 0700.HK 15m: HTTP 429")
    stats = feed.health()["rate_limit"]
    assert stats["count"] == 1 and stats["consecutive"] == 1 and stats["last_ts"] > 0

    YahooFinanceAdapter.record_success()
    assert feed.health()["rate_limit"]["consecutive"] == 0, "streak clears, total keeps history"
