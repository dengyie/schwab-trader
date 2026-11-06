"""
Virtual Paper Trading Matching Engine simulating exchange fills and order routing.
"""
from __future__ import annotations
import uuid
from typing import Dict, List, Optional, Callable
from datetime import datetime, timezone
from app.models.trading import (
    Order, OrderSide, OrderType, OrderStatus, Trade, Quote, Position, Portfolio, utc_now
)
from app.engine.portfolio_ledger import PortfolioLedger
from app.engine.base_broker import BaseBroker


class VirtualMatchingEngine(BaseBroker):
    def __init__(
        self,
        ledger: PortfolioLedger,
        default_slippage_bps: float = 0.0,
        default_commission_per_order: float = 0.0
    ):
        self.ledger = ledger
        self.default_slippage_bps = default_slippage_bps
        self.default_commission_per_order = default_commission_per_order
        self.orders: Dict[str, Order] = {}
        self.latest_quotes: Dict[str, Quote] = {}
        self.fill_listeners: List[Callable[[Trade, Order], None]] = []

    def get_portfolio(self) -> Portfolio:
        return self.ledger.get_portfolio()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.ledger.get_position(symbol)

    def get_orders(self) -> List[Order]:
        return list(self.orders.values())

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    def reset(self, initial_capital: float = 100000.0) -> None:
        self.ledger.reset(initial_capital)
        self.orders.clear()

    def register_fill_listener(self, listener: Callable[[Trade, Order], None]) -> None:
        self.fill_listeners.append(listener)

    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        stop_price: Optional[float] = None
    ) -> Order:
        if quantity <= 0:
            raise ValueError("Order quantity must be positive")

        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            symbol=symbol.upper(),
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            status=OrderStatus.PENDING,
            created_at=utc_now(),
            updated_at=utc_now()
        )
        self.orders[order_id] = order

        # Immediate matching attempt if quote available
        quote = self.latest_quotes.get(order.symbol)
        if quote:
            self._try_match_order(order, quote)

        return order

    def cancel_order(self, order_id: str) -> Optional[Order]:
        order = self.orders.get(order_id)
        if not order:
            return None
        if order.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            order.status = OrderStatus.CANCELLED
            order.updated_at = utc_now()
        return order

    def on_quote(self, quote: Quote) -> List[Trade]:
        """
        Process quote update:
        1. Cache latest quote
        2. Update ledger valuation
        3. Match active pending orders
        """
        symbol = quote.symbol.upper()
        self.latest_quotes[symbol] = quote
        self.ledger.update_quote(quote)

        generated_trades: List[Trade] = []
        # Copy values to safely iterate while modifying
        pending_orders = [
            ord for ord in self.orders.values()
            if ord.symbol == symbol and ord.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]

        for order in pending_orders:
            trade = self._try_match_order(order, quote)
            if trade:
                generated_trades.append(trade)

        return generated_trades

    def _try_match_order(self, order: Order, quote: Quote) -> Optional[Trade]:
        if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            return None

        fill_price: Optional[float] = None
        slippage_factor = self.default_slippage_bps / 10000.0

        if order.order_type == OrderType.MARKET:
            if order.side == OrderSide.BUY:
                base_price = quote.ask if quote.ask > 0 else quote.last
                fill_price = round(base_price * (1.0 + slippage_factor), 4)
            else:
                base_price = quote.bid if quote.bid > 0 else quote.last
                fill_price = round(base_price * (1.0 - slippage_factor), 4)

        elif order.order_type == OrderType.LIMIT:
            if order.price is None:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "Limit order requires price"
                return None

            if order.side == OrderSide.BUY and quote.last <= order.price:
                fill_price = min(quote.last, order.price)
            elif order.side == OrderSide.SELL and quote.last >= order.price:
                fill_price = max(quote.last, order.price)

        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "Stop order requires stop_price"
                return None

            if order.side == OrderSide.BUY and quote.last >= order.stop_price:
                fill_price = round(quote.last * (1.0 + slippage_factor), 4)
            elif order.side == OrderSide.SELL and quote.last <= order.stop_price:
                fill_price = round(quote.last * (1.0 - slippage_factor), 4)

        if fill_price is not None and fill_price > 0:
            trade_qty = order.quantity - order.filled_quantity
            trade = Trade(
                trade_id=f"trd_{uuid.uuid4().hex[:12]}",
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                price=fill_price,
                quantity=trade_qty,
                commission=self.default_commission_per_order,
                timestamp=utc_now()
            )

            # Apply to portfolio ledger
            success = self.ledger.apply_trade(trade)
            if success:
                order.filled_quantity += trade_qty
                order.filled_avg_price = fill_price
                order.status = OrderStatus.FILLED
                order.updated_at = utc_now()

                for listener in self.fill_listeners:
                    try:
                        listener(trade, order)
                    except Exception:
                        pass
                return trade
            else:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "Insufficient buying power or invalid position"
                order.updated_at = utc_now()

        return None
