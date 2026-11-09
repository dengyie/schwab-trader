"""ORDER_FILLED pushes must never invent an account balance.

GET /portfolio answers 503 when the broker cannot be read. The socket had the
same situation and answered with zeros, which the wallet store applied as
"cash 0, equity 0" -- a fill would have wiped the account on screen.
"""
import asyncio

import app.main as main
from app.models.trading import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Trade,
)


def _sample():
    order = Order(
        order_id="ord-1",
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        status=OrderStatus.FILLED,
    )
    trade = Trade(
        trade_id="trd-1",
        order_id="ord-1",
        symbol="SPY",
        side=OrderSide.BUY,
        price=500.0,
        quantity=1,
    )
    return trade, order


def _capture(monkeypatch):
    sent = []

    async def fake_broadcast(message):
        sent.append(message)

    monkeypatch.setattr(main.ws_manager, "broadcast", fake_broadcast)
    return sent


def test_failed_portfolio_refresh_omits_the_snapshot(monkeypatch):
    trade, order = _sample()
    sent = _capture(monkeypatch)

    def boom():
        raise RuntimeError("broker down")

    monkeypatch.setattr(main.broker, "get_portfolio", boom)
    asyncio.run(main._broadcast_order_filled(trade, order))

    assert len(sent) == 1
    message = sent[0]
    assert message["type"] == "ORDER_FILLED"
    assert "portfolio" not in message
    # The fill itself still has to reach the client.
    assert message["trade"]["symbol"] == "SPY"
    assert message["order"]["order_id"] == "ord-1"


def test_successful_refresh_carries_the_real_snapshot(monkeypatch):
    trade, order = _sample()
    sent = _capture(monkeypatch)

    class Portfolio:
        cash = 12345.67
        total_equity = 13000.0
        market_value = 654.33
        unrealized_pnl = -12.5
        realized_pnl = 3.0

    monkeypatch.setattr(main.broker, "get_portfolio", lambda: Portfolio())
    asyncio.run(main._broadcast_order_filled(trade, order))

    assert sent[0]["portfolio"] == {
        "cash": 12345.67,
        "total_equity": 13000.0,
        "market_value": 654.33,
        "unrealized_pnl": -12.5,
        "realized_pnl": 3.0,
    }
