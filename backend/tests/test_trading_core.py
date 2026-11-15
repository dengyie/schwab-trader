"""
Unit tests for core trading domain, virtual matching engine, ledger, and data adapters.
"""
import pytest
from datetime import datetime
from app.models.trading import (
    OrderSide, OrderType, OrderStatus, Quote, Bar
)
from app.engine.portfolio_ledger import PortfolioLedger
from app.engine.matching_engine import VirtualMatchingEngine
from app.adapters.schwab_adapter import SchwabDataAdapter
from app.adapters.model_exporter import ModelFeatureExporter


def test_portfolio_ledger_buy_and_sell():
    ledger = PortfolioLedger(initial_capital=10000.0)
    p = ledger.get_portfolio()
    assert p.cash == 10000.0
    assert p.total_equity == 10000.0

    # Submit virtual engine
    engine = VirtualMatchingEngine(ledger)

    # 1. Provide initial quote
    quote_aapl = Quote(symbol="AAPL", bid=150.0, ask=150.2, last=150.1, volume=1000)
    engine.on_quote(quote_aapl)

    # 2. Market Buy 10 shares of AAPL
    order = engine.submit_order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10
    )
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == 150.2  # Filled at ask
    # The fill's execution time is recorded separately from updated_at.
    assert order.filled_at is not None
    assert order.filled_at >= order.created_at
    assert ledger.portfolio.cash == round(10000.0 - (10 * 150.2), 4)

    pos = ledger.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 10
    assert pos.avg_cost == 150.2

    # 3. Price increases, test mark-to-market
    quote_aapl_up = Quote(symbol="AAPL", bid=160.0, ask=160.2, last=160.0, volume=1500)
    engine.on_quote(quote_aapl_up)
    # get_position returns a snapshot; re-read it after the book moves.
    pos = ledger.get_position("AAPL")
    assert pos.current_price == 160.0
    assert pos.unrealized_pnl == round((160.0 - 150.2) * 10, 2)

    # 4. Market Sell 5 shares
    sell_order = engine.submit_order(
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=5
    )
    assert sell_order.status == OrderStatus.FILLED
    assert sell_order.filled_avg_price == 160.0  # Filled at bid
    assert ledger.get_position("AAPL").quantity == 5
    assert ledger.portfolio.realized_pnl == round((160.0 - 150.2) * 5, 2)


def test_limit_order_matching():
    ledger = PortfolioLedger(initial_capital=10000.0)
    engine = VirtualMatchingEngine(ledger)

    # Initial quote
    quote_spy = Quote(symbol="SPY", bid=500.0, ask=500.5, last=500.2, volume=10000)
    engine.on_quote(quote_spy)

    # Submit Buy Limit order below market price ($495.0)
    order = engine.submit_order(
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=2,
        price=495.0
    )
    assert order.status == OrderStatus.PENDING

    # Market moves down to $496.0 (not yet matched)
    quote_spy_mid = Quote(symbol="SPY", bid=495.8, ask=496.2, last=496.0, volume=12000)
    trades = engine.on_quote(quote_spy_mid)
    assert len(trades) == 0
    assert order.status == OrderStatus.PENDING

    # Market drops to $494.5 (triggered!)
    quote_spy_drop = Quote(symbol="SPY", bid=494.2, ask=494.6, last=494.5, volume=15000)
    trades = engine.on_quote(quote_spy_drop)
    assert len(trades) == 1
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == 494.5
    assert ledger.get_position("SPY").quantity == 2


def test_stop_order_matching():
    ledger = PortfolioLedger(initial_capital=10000.0)
    engine = VirtualMatchingEngine(ledger)

    # Buy initial position at $100
    engine.on_quote(Quote(symbol="MSFT", bid=100.0, ask=100.0, last=100.0))
    engine.submit_order(symbol="MSFT", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10)

    # Place stop loss at $95.0
    stop_order = engine.submit_order(
        symbol="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        quantity=10,
        stop_price=95.0
    )
    assert stop_order.status == OrderStatus.PENDING

    # Quote drops to $94.0 -> triggers stop
    trades = engine.on_quote(Quote(symbol="MSFT", bid=93.8, ask=94.2, last=94.0))
    assert len(trades) == 1
    assert stop_order.status == OrderStatus.FILLED
    assert stop_order.filled_avg_price == 94.0
    assert ledger.get_position("MSFT").quantity == 0


def test_insufficient_buying_power():
    ledger = PortfolioLedger(initial_capital=500.0)
    engine = VirtualMatchingEngine(ledger)

    engine.on_quote(Quote(symbol="NVDA", bid=1000.0, ask=1000.0, last=1000.0))
    order = engine.submit_order(symbol="NVDA", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    assert order.status == OrderStatus.REJECTED
    assert "Insufficient" in (order.reject_reason or "")


def test_schwab_adapter_parsing():
    # Test raw numeric indices from Streamer
    raw_payload = {
        "key": "QQQ",
        "1": 480.25,
        "2": 480.35,
        "3": 480.30,
        "8": 5420000,
        "50": 1700000000000
    }
    quote = SchwabDataAdapter.parse_streamer_level_one_equity(raw_payload)
    assert quote is not None
    assert quote.symbol == "QQQ"
    assert quote.bid == 480.25
    assert quote.ask == 480.35
    assert quote.last == 480.30
    assert quote.volume == 5420000

    # Test REST price history
    rest_data = {
        "candles": [
            {"open": 478.0, "high": 481.0, "low": 477.5, "close": 480.3, "volume": 100000, "datetime": 1700000000000}
        ]
    }
    bars = SchwabDataAdapter.parse_rest_price_history(rest_data)
    assert len(bars) == 1
    assert bars[0].close == 480.3
    assert bars[0].time == 1700000000


def test_model_exporter_and_agent_context():
    bars = [
        Bar(time=i*60, open=100.0+i, high=101.0+i, low=99.0+i, close=100.5+i, volume=500)
        for i in range(25)
    ]
    records = ModelFeatureExporter.to_records(bars)
    assert len(records) == 25
    assert records[0]["close"] == 100.5

    features = ModelFeatureExporter.extract_features(bars)
    assert "sma_20" in features
    assert "rsi_14" in features
    assert len(features["sma_20"]) == 25

    quote = Quote(symbol="SPY", bid=520.0, ask=520.1, last=520.05)
    context = ModelFeatureExporter.format_agent_market_context("SPY", quote, bars)
    assert context["symbol"] == "SPY"
    assert context["technical_indicators"]["trend"] == "BULLISH"
    assert context["real_time_quote"]["last"] == 520.05


def test_stop_limit_order_matching():
    """2026-11-16: a STOP carrying a limit price is a stop-limit — trigger at
    stop_price, fill at the limit (the same fill-at-improvement
    simplification the LIMIT branch makes). Without a price it stays a
    stop-market at the trigger quote."""
    ledger = PortfolioLedger(initial_capital=10000.0)
    engine = VirtualMatchingEngine(ledger)

    engine.on_quote(Quote(symbol="MSFT", bid=100.0, ask=100.0, last=100.0))
    engine.submit_order(symbol="MSFT", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10)

    # Stop-limit sell: trigger 95, protect at 94.5
    stop = engine.submit_order(
        symbol="MSFT", side=OrderSide.SELL, order_type=OrderType.STOP,
        quantity=10, stop_price=95.0, price=94.5,
    )
    assert stop.status == OrderStatus.PENDING

    # Quote drops through the trigger -> fills at the limit, not the quote
    trades = engine.on_quote(Quote(symbol="MSFT", bid=93.8, ask=94.2, last=94.0))
    assert len(trades) == 1
    assert stop.status == OrderStatus.FILLED
    assert stop.filled_avg_price == 94.5
