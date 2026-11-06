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
    assert quote.last == round((753.47 + 753.51) / 2.0, 4)
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

