"""
Unit and Integration tests for Alpaca Adapter, Paper Broker, and Real Market Feed.
"""
import os
import pytest
from datetime import datetime, timezone
from app.models.trading import (
    Quote, Bar, Order, OrderSide, OrderType, OrderStatus
)
from app.adapters.alpaca_adapter import AlpacaDataAdapter, _parse_alpaca_timestamp
from app.engine.alpaca_broker import AlpacaPaperBroker
from app.engine.alpaca_market_feed import AlpacaMarketFeedService
from app.engine.base_broker import BaseBroker
from app.engine.matching_engine import VirtualMatchingEngine
from app.engine.portfolio_ledger import PortfolioLedger


def test_alpaca_timestamp_parser():
    # RFC3339 with nanoseconds
    ts_str = "2026-11-02T19:48:19.218325244Z"
    dt = _parse_alpaca_timestamp(ts_str)
    assert dt.year == 2026
    assert dt.month == 11
    assert dt.day == 2
    assert dt.hour == 19
    assert dt.minute == 48
    assert dt.tzinfo == timezone.utc

    # Standard ISO string
    ts_str2 = "2026-11-02T13:30:00Z"
    dt2 = _parse_alpaca_timestamp(ts_str2)
    assert int(dt2.timestamp()) > 0


def test_alpaca_parse_stream_quote():
    raw_msg = {
        "T": "q",
        "S": "SPY",
        "bp": 753.47,
        "bs": 40,
        "ap": 753.51,
        "as": 80,
        "t": "2026-11-02T19:48:19.218325Z"
    }
    quote = AlpacaDataAdapter.parse_stream_quote(raw_msg)
    assert quote is not None
    assert quote.symbol == "SPY"
    assert quote.bid == 753.47
    assert quote.ask == 753.51
    # A quote is not a trade: with no print known, ``last`` stays unknown. The
    # old midpoint here was shown as a price SPY never traded at.
    assert quote.last == 0.0
    assert quote.volume == 120

    # With prior trade price
    quote_with_last = AlpacaDataAdapter.parse_stream_quote(raw_msg, last_price=753.50)
    assert quote_with_last.last == 753.50


def test_alpaca_parse_stream_bar():
    raw_bar = {
        "T": "b",
        "S": "QQQ",
        "o": 510.5,
        "h": 511.2,
        "l": 510.1,
        "c": 511.0,
        "v": 1500,
        "t": "2026-11-02T14:00:00Z"
    }
    bar = AlpacaDataAdapter.parse_stream_bar(raw_bar)
    assert bar is not None
    assert bar.open == 510.5
    assert bar.high == 511.2
    assert bar.low == 510.1
    assert bar.close == 511.0
    assert bar.volume == 1500
    assert isinstance(bar.time, int)


def test_alpaca_parse_stream_trade():
    raw = {
        "T": "t",
        "S": "SPY",
        "i": 12345,
        "p": 753.49,
        "s": 200,
        "t": "2026-11-02T19:48:19.218325Z",
    }
    trade = AlpacaDataAdapter.parse_stream_trade(raw)
    assert trade is not None
    assert trade.symbol == "SPY"
    assert trade.price == 753.49
    assert trade.quantity == 200
    assert trade.trade_id == "12345"

    # Invalid payloads are dropped rather than fabricated.
    assert AlpacaDataAdapter.parse_stream_trade({"T": "t", "S": "SPY", "p": 0, "s": 10}) is None
    assert AlpacaDataAdapter.parse_stream_trade({"T": "t", "S": "", "p": 10, "s": 1}) is None


def test_alpaca_parse_rest_bars_response():
    resp_dict = {
        "bars": {
            "SPY": [
                {"t": "2026-11-02T13:30:00Z", "o": 585.0, "h": 586.0, "l": 584.5, "c": 585.8, "v": 2000},
                {"t": "2026-11-02T13:31:00Z", "o": 585.8, "h": 586.5, "l": 585.5, "c": 586.2, "v": 3000}
            ]
        }
    }
    bars = AlpacaDataAdapter.parse_rest_bars_response(resp_dict, symbol="SPY")
    assert len(bars) == 2
    assert bars[0].close == 585.8
    assert bars[1].close == 586.2
    assert bars[0].time < bars[1].time


def test_alpaca_parse_account_and_positions():
    account_json = {
        "cash": "98500.50",
        "equity": "100250.00"
    }
    positions_json = [
        {
            "symbol": "AAPL",
            "qty": "10",
            "avg_entry_price": "230.50",
            "current_price": "235.00"
        }
    ]
    portfolio = AlpacaDataAdapter.parse_account_to_portfolio(account_json, positions_json)
    assert portfolio.cash == 98500.50
    assert "AAPL" in portfolio.positions
    pos = portfolio.positions["AAPL"]
    assert pos.quantity == 10
    assert pos.avg_cost == 230.50
    assert pos.current_price == 235.00
    assert pos.market_value == 2350.00


def test_alpaca_parse_order_to_domain():
    order_json = {
        "id": "alpaca_ord_12345",
        "symbol": "NVDA",
        "qty": "20",
        "filled_qty": "20",
        "filled_avg_price": "142.50",
        "side": "buy",
        "type": "market",
        "status": "filled",
        "created_at": "2026-11-02T15:00:00Z"
    }
    order = AlpacaDataAdapter.parse_order_to_domain(order_json)
    assert order.order_id == "alpaca_ord_12345"
    assert order.symbol == "NVDA"
    assert order.side == OrderSide.BUY
    assert order.order_type == OrderType.MARKET
    assert order.status == OrderStatus.FILLED
    assert order.quantity == 20
    assert order.filled_quantity == 20
    assert order.filled_avg_price == 142.50


def test_alpaca_order_timestamps_come_from_the_venue():
    # Order history is replayed from this lookup on every poll, so the timestamps
    # must be the venue's own. Stamping updated_at = now() on each fetch made
    # every historical fill look like it executed seconds ago: 1H volume totals
    # included two-day-old trades and FIFO trade stats reordered themselves.
    order_json = {
        "id": "alpaca_ord_hist",
        "symbol": "SPY",
        "qty": "10",
        "filled_qty": "10",
        "filled_avg_price": "500.00",
        "side": "buy",
        "type": "market",
        "status": "filled",
        "created_at": "2026-11-02T19:59:00.123456789Z",
        "updated_at": "2026-11-02T20:00:01.987654321Z",
        "filled_at": "2026-11-02T20:00:00.500000Z",
    }
    order = AlpacaDataAdapter.parse_order_to_domain(order_json)

    assert order.created_at == datetime(2026, 11, 2, 19, 59, 0, 123456, tzinfo=timezone.utc)
    assert order.updated_at == datetime(2026, 11, 2, 20, 0, 1, 987654, tzinfo=timezone.utc)
    assert order.filled_at == datetime(2026, 11, 2, 20, 0, 0, 500000, tzinfo=timezone.utc)


def test_alpaca_order_without_venue_timestamps_stays_stable():
    # Older/simplified payloads: no updated_at and no filled_at. The row must
    # still describe the order's creation, not the moment it was read.
    order_json = {
        "id": "alpaca_ord_min",
        "symbol": "AAPL",
        "qty": "1",
        "filled_qty": "0",
        "side": "buy",
        "type": "limit",
        "limit_price": "200.00",
        "status": "new",
        "created_at": "2026-11-02T15:00:00Z",
    }
    first = AlpacaDataAdapter.parse_order_to_domain(order_json)
    second = AlpacaDataAdapter.parse_order_to_domain(order_json)

    assert first.filled_at is None
    assert first.updated_at == first.created_at == second.updated_at
    assert first.status == OrderStatus.PENDING


def test_base_broker_inheritance():
    ledger = PortfolioLedger(initial_capital=50000.0)
    engine = VirtualMatchingEngine(ledger)
    assert isinstance(engine, BaseBroker)
    p = engine.get_portfolio()
    assert p.cash == 50000.0

    # Test reset
    engine.reset(20000.0)
    assert engine.get_portfolio().cash == 20000.0


def test_alpaca_broker_initialization():
    broker = AlpacaPaperBroker(
        api_key="TEST_KEY",
        api_secret="TEST_SECRET"
    )
    assert isinstance(broker, BaseBroker)
    assert broker.api_key == "TEST_KEY"
    assert "APCA-API-KEY-ID" in broker.headers


class _FakeClockResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _broker_with_clock(monkeypatch, response):
    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    monkeypatch.setattr(broker.client, "get", lambda *a, **kw: response)
    return broker


def test_market_clock_parses_alpaca_payload(monkeypatch):
    payload = {
        "timestamp": "2026-11-06T21:00:00.1Z",
        "is_open": False,
        "next_open": "2026-11-09T14:30:00Z",
        "next_close": "2026-11-09T21:00:00Z",
    }
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, payload))
    clock = broker.get_market_clock()
    assert clock["is_open"] is False
    assert clock["next_open"] == "2026-11-09T14:30:00Z"
    assert clock["next_close"] == "2026-11-09T21:00:00Z"


def test_market_clock_unavailable_returns_none(monkeypatch):
    """A broker hiccup must mean 'unknown session', never 'market open'."""
    # Non-200
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(403, {}))
    assert broker.get_market_clock() is None

    # Transport failure
    def boom(*a, **kw):
        raise ConnectionError("network down")
    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    monkeypatch.setattr(broker.client, "get", boom)
    assert broker.get_market_clock() is None

    # Malformed JSON body
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, ValueError("bad json")))
    assert broker.get_market_clock() is None


def test_market_clock_is_cached_across_calls(monkeypatch):
    """Every open terminal polls the clock; one poll must not be one API call."""
    payload = {"is_open": True, "next_open": "o", "next_close": "c"}
    calls = []

    def counting_get(*a, **kw):
        calls.append(a)
        return _FakeClockResponse(200, payload)

    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    monkeypatch.setattr(broker.client, "get", counting_get)
    assert broker.get_market_clock()["is_open"] is True
    assert broker.get_market_clock()["is_open"] is True
    assert len(calls) == 1


def test_market_clock_cache_expires(monkeypatch):
    payload = {"is_open": True, "next_open": "o", "next_close": "c"}
    calls = []

    def counting_get(*a, **kw):
        calls.append(a)
        return _FakeClockResponse(200, payload)

    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    monkeypatch.setattr(broker.client, "get", counting_get)
    assert broker.get_market_clock()["is_open"] is True
    broker._clock_cached_at -= broker._clock_ttl + 1.0
    assert broker.get_market_clock()["is_open"] is True
    assert len(calls) == 2


def test_market_clock_serves_last_known_on_failure(monkeypatch):
    """A refused call must not make a closed session look unknown."""
    payload = {"is_open": False, "next_open": "o", "next_close": "c"}
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, payload))
    assert broker.get_market_clock()["is_open"] is False

    def boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(broker.client, "get", boom)
    broker._clock_cached_at -= broker._clock_ttl + 1.0
    stale = broker.get_market_clock()
    assert stale is not None
    assert stale["is_open"] is False


def test_market_clock_returns_a_copy(monkeypatch):
    """Callers format the payload; they must not be able to poison the cache."""
    payload = {"is_open": True, "next_open": "o", "next_close": "c"}
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, payload))
    broker.get_market_clock()["is_open"] = "mutated"
    assert broker.get_market_clock()["is_open"] is True


@pytest.mark.skipif(
    os.getenv("ALPACA_LIVE_TESTS") != "1",
    reason="Live Alpaca test; opt in with ALPACA_LIVE_TESTS=1",
)
def test_alpaca_live_account_and_market_data():
    """Verify live connectivity with Alpaca Paper Trading REST API."""
    import httpx
    from app.core.config import settings

    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        pytest.skip("Alpaca API credentials not configured")

    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret
    }
    with httpx.Client(headers=headers, timeout=10.0) as client:
        # 1. Verify account status
        acc_resp = client.get(f"{settings.alpaca_paper_base_url}/account")
        assert acc_resp.status_code == 200, f"Account fetch failed: {acc_resp.text}"
        acc_data = acc_resp.json()
        assert acc_data.get("status") == "ACTIVE"
        assert float(acc_data.get("cash", 0)) > 50000.0

        # 2. Verify market data latest quote
        quote_resp = client.get(
            f"{settings.alpaca_data_base_url}/stocks/SPY/quotes/latest",
            params={"feed": settings.alpaca_feed}
        )
        assert quote_resp.status_code == 200, f"Quote fetch failed: {quote_resp.text}"
        quote_data = quote_resp.json()
        assert "quote" in quote_data
        assert float(quote_data["quote"].get("bp", 0)) > 0



# -- account cache invalidation --------------------------------------------
def _order_json(order_id="ord-1"):
    return {
        "id": order_id,
        "client_order_id": f"cli-{order_id}",
        "symbol": "SPY",
        "side": "buy",
        "type": "limit",
        "qty": "1",
        "filled_qty": "0",
        "status": "new",
        "limit_price": "600.00",
        "created_at": "2026-11-10T15:00:00Z",
        "updated_at": "2026-11-10T15:00:00Z",
    }


def test_submitting_an_order_invalidates_the_account_caches(monkeypatch):
    """A fresh order must show up on the next portfolio/orders read.

    The 5s TTL is what makes the panel cheap to poll; without this
    invalidation the user would submit a trade and watch an unchanged book
    for up to five seconds.
    """
    import time as _time

    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    broker._portfolio_cached_at = _time.time()
    broker._orders_cached_at = _time.time()

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return _order_json()

    monkeypatch.setattr(broker.client, "post", lambda *a, **kw: _Resp())

    order = broker.submit_order("SPY", OrderSide.BUY, OrderType.LIMIT, 1, price=600.0)
    assert order.order_id == "ord-1"
    assert broker._portfolio_cached_at == 0.0
    assert broker._orders_cached_at == 0.0


def test_cancelling_an_order_invalidates_the_order_cache(monkeypatch):
    import time as _time

    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    order = AlpacaDataAdapter.parse_order_to_domain(_order_json())
    broker.cached_orders[order.order_id] = order
    broker._orders_cached_at = _time.time()

    class _Resp:
        status_code = 204

    monkeypatch.setattr(broker.client, "delete", lambda *a, **kw: _Resp())

    returned = broker.cancel_order(order.order_id)
    assert returned is not None
    assert returned.status == OrderStatus.CANCELLED
    assert broker._orders_cached_at == 0.0


# -- Equity history ---------------------------------------------------------
def _history_payload():
    return {
        "timestamp": [1791849600, 1791936000, 1792022400, 1792108800],
        "equity": [100000.0, 100500.0, None, 101200.0],
        "base_value": 100000.0,
    }


def test_equity_history_parses_and_skips_null_days(monkeypatch):
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, _history_payload()))
    history = broker.get_equity_history(period="1M")
    assert history is not None
    # The venue reports a null equity for non-trading days; zero-filling those
    # would draw a fake crash to $0 in the middle of the curve.
    assert [p["equity"] for p in history["points"]] == [100000.0, 100500.0, 101200.0]
    assert history["base_value"] == 100000.0


def test_equity_history_is_cached_and_returns_a_copy(monkeypatch):
    calls = []
    broker = AlpacaPaperBroker(api_key="K", api_secret="S")

    def _counting(url, **kwargs):
        if "portfolio/history" not in url:
            return _FakeClockResponse(200, {})
        calls.append(1)
        return _FakeClockResponse(200, _history_payload())

    monkeypatch.setattr(broker.client, "get", _counting)

    first = broker.get_equity_history()
    first["points"][0]["equity"] = 999.0  # a caller mutating its copy...
    second = broker.get_equity_history()
    assert calls == [1], "the 5-minute TTL must collapse repeated polls"
    assert second["points"][0]["equity"] == 100000.0, "...must not poison the cache"


def test_equity_history_fresh_account_maps_to_none(monkeypatch):
    # A brand-new account gets an all-null or empty series from Alpaca;
    # two real points are the minimum a chart can draw.
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, {"timestamp": [1, 2], "equity": [None, None]}))
    assert broker.get_equity_history() is None


def test_equity_history_upstream_failure_maps_to_none(monkeypatch):
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(500, {}))
    assert broker.get_equity_history() is None


def test_equity_history_serves_stale_curve_on_transient_failure(monkeypatch):
    broker = AlpacaPaperBroker(api_key="K", api_secret="S")
    monkeypatch.setattr(broker.client, "get", lambda *a, **kw: _FakeClockResponse(200, _history_payload()))
    assert broker.get_equity_history() is not None

    monkeypatch.setattr(broker.client, "get", lambda *a, **kw: _FakeClockResponse(503, {}))
    still = broker.get_equity_history()
    assert still is not None
    assert still["points"][-1]["equity"] == 101200.0


def test_equity_history_skips_alpacas_zero_padding(monkeypatch):
    # Observed live: Alpaca pads days before the account existed with 0.0
    # equity instead of null. Zero-filling those draws a fake "from $0"
    # rocket; they are pre-account padding, not real closes.
    broker = _broker_with_clock(
        monkeypatch,
        _FakeClockResponse(200, {
            "timestamp": [1793840400, 1793926800, 1794013200, 1794272400],
            "equity": [0.0, 0.0, 0.0, 100000.0],
            "base_value": 100000.0,
        }),
    )
    assert broker.get_equity_history() is None  # one real point < 2


def test_equity_history_truncates_padding_by_account_creation(monkeypatch):
    # Live probing also caught intraday series padding pre-account entries
    # with base_value instead of 0.0, so a value filter alone cannot tell
    # padding from a real (flat) session close. The boundary is midnight ET
    # of the account's first trading day: the venue starts an intraday
    # series at that day's 09:30 open even for a mid-session signup.
    created = "2026-11-10T22:39:58.895409Z"  # 17:39 ET, mid-session
    et_midnight = "2026-11-10T04:00:00+00:00"
    et_midnight_ts = 1794321600
    before, first_open, later = et_midnight_ts - 86400, et_midnight_ts + 1800, et_midnight_ts + 86400
    broker = AlpacaPaperBroker(api_key="K", api_secret="S")

    def _route(url, **kwargs):
        if url.endswith("/account"):
            return _FakeClockResponse(200, {"created_at": created})
        return _FakeClockResponse(200, {
            "timestamp": [before, first_open, later],
            "equity": [100000.0, 100000.0, 100500.0],  # padding is NOT 0 here
            "base_value": 100000.0,
        })

    monkeypatch.setattr(broker.client, "get", _route)
    history = broker.get_equity_history()
    assert history is not None
    # The pre-day point dies; the first-session open survives.
    assert [p["time"] for p in history["points"]] == [first_open, later]

    # The boundary is cached for an hour, not refetched per call.
    account_calls = []
    real_route = _route

    def _counting_route(url, **kwargs):
        if url.endswith("/account"):
            account_calls.append(1)
        return real_route(url, **kwargs)

    monkeypatch.setattr(broker.client, "get", _counting_route)
    broker.get_equity_history(period="1W")
    assert account_calls == [], "created_at must be TTL-cached"


def test_equity_history_unknown_timeframe_collapses_to_default(monkeypatch):
    # Arbitrary strings must neither reach the venue nor grow the cache:
    # each unknown key collapses onto the default instead of becoming a
    # permanent cache entry.
    sent = []
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, _history_payload()))
    monkeypatch.setattr(broker.client, "get", lambda url, **kw: sent.append(kw.get("params")) or _FakeClockResponse(200, _history_payload()) if "portfolio/history" in url else _FakeClockResponse(200, {}))
    broker.get_equity_history(timeframe="; DROP TABLE users")
    assert sent[-1] == {"period": "1M", "timeframe": "1D"}


def test_equity_history_intraday_timeframe_maps_to_venue_spelling(monkeypatch):
    # Alpaca spells intraday timeframes "1Min"/"5Min"/"15Min"; anything else
    # is rejected with 40010001. Case-insensitive input maps onto that.
    sent = []
    broker = _broker_with_clock(monkeypatch, _FakeClockResponse(200, _history_payload()))
    monkeypatch.setattr(broker.client, "get", lambda url, **kw: sent.append(kw.get("params")) or _FakeClockResponse(200, _history_payload()) if "portfolio/history" in url else _FakeClockResponse(200, {}))
    broker.get_equity_history(timeframe="5min")
    assert sent[-1] == {"period": "1M", "timeframe": "5Min"}
    broker.get_equity_history(timeframe="15Min")
    assert sent[-1] == {"period": "1M", "timeframe": "15Min"}


def test_equity_history_empty_result_is_cached_briefly(monkeypatch):
    # A fresh account would otherwise hit the venue on every page view;
    # the empty answer gets a short TTL so recovery is quick.
    calls = []
    broker = AlpacaPaperBroker(api_key="K", api_secret="S")

    def _counting(url, **kwargs):
        if "portfolio/history" not in url:
            return _FakeClockResponse(200, {})
        calls.append(1)
        return _FakeClockResponse(200, {"timestamp": [1, 2], "equity": [None, None]})

    monkeypatch.setattr(broker.client, "get", _counting)
    assert broker.get_equity_history() is None
    assert broker.get_equity_history() is None
    assert calls == [1], "an empty result must be served from cache"
    assert broker._equity_history_cache["1M:1D"] == (None, broker._equity_history_cache["1M:1D"][1])
