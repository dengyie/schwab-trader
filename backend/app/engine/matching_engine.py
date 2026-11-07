"""
Virtual Paper Trading Matching Engine simulating exchange fills and order routing.

The engine enforces the same market rules a real venue would (board lots, tick
sizes, daily price bands, T+1) before an order is accepted, and books foreign
currency fills through the ledger's FX conversion.
"""
from __future__ import annotations

import uuid
from typing import Callable, Dict, List, Optional

from app.core.markets import (
    is_market_open,
    market_of,
    resolve_symbol,
    tick_size_for,
    validate_order,
)
from app.models.trading import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Trade,
    Quote,
    Position,
    Portfolio,
    utc_now,
)
from app.engine.portfolio_ledger import PortfolioLedger
from app.engine.base_broker import BaseBroker


class VirtualMatchingEngine(BaseBroker):
    def __init__(
        self,
        ledger: PortfolioLedger,
        default_slippage_bps: float = 0.0,
        default_commission_per_order: float = 0.0,
        fx=None,
        enforce_sessions: bool = False,
        enforce_sessions_for: frozenset | None = None,
    ):
        self.ledger = ledger
        self.default_slippage_bps = default_slippage_bps
        self.default_commission_per_order = default_commission_per_order
        self._fx = fx
        self._enforce_sessions = enforce_sessions
        # When enforce_sessions is off, non-US venues may still be guarded.
        self._enforce_sessions_for = enforce_sessions_for or frozenset()
        self.orders: Dict[str, Order] = {}
        self.latest_quotes: Dict[str, Quote] = {}
        self.fill_listeners: List[Callable[[Trade, Order], None]] = []

    # -- reads ------------------------------------------------------------
    def get_portfolio(self) -> Portfolio:
        return self.ledger.get_portfolio()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.ledger.get_position(symbol)

    def get_orders(self) -> List[Order]:
        return list(self.orders.values())

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    # -- lifecycle --------------------------------------------------------
    def reset(self, initial_capital: float = 100000.0) -> None:
        self.ledger.reset(initial_capital)
        self.orders.clear()
        self.latest_quotes.clear()

    def register_fill_listener(self, listener: Callable[[Trade, Order], None]) -> None:
        self.fill_listeners.append(listener)

    # -- execution --------------------------------------------------------
    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Order:
        ref = resolve_symbol(symbol)
        if ref is None:
            raise ValueError(f"Invalid symbol: {symbol!r}")
        market = ref.market
        canonical = ref.symbol

        quote = self.latest_quotes.get(canonical)
        prev_close = quote.prev_close if quote else None

        validate_order(
            market,
            canonical,
            side,
            order_type,
            quantity,
            price=price,
            stop_price=stop_price,
            prev_close=prev_close,
            position=self.ledger.get_position(canonical),
        )

        # A market order cannot be filled while the venue is shut; accepting it
        # would silently book a trade at a stale price.
        session_guarded = self._enforce_sessions or market.code in self._enforce_sessions_for
        if (
            session_guarded
            and order_type == OrderType.MARKET
            and not is_market_open(market)
        ):
            raise ValueError(
                f"{market.code} market is closed; use a limit order or retry "
                "when the session opens"
            )

        order = Order(
            order_id=f"ord_{uuid.uuid4().hex[:12]}",
            symbol=canonical,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            status=OrderStatus.PENDING,
            created_at=utc_now(),
            updated_at=utc_now(),
            market=market.code,
            currency=market.currency,
        )
        self.orders[order.order_id] = order

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
        pending_orders = [
            order
            for order in self.orders.values()
            if order.symbol == symbol
            and order.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]

        for order in pending_orders:
            trade = self._try_match_order(order, quote)
            if trade:
                generated_trades.append(trade)

        return generated_trades

    # -- matching ---------------------------------------------------------
    def _fx_rate(self, currency: str) -> float:
        if self._fx is not None:
            return self._fx.usd_per_unit(currency)
        return 1.0

    def _try_match_order(self, order: Order, quote: Quote) -> Optional[Trade]:
        if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            return None

        market = market_of(order.symbol)
        tick = tick_size_for(market, quote.last)
        fill_price: Optional[float] = None
        slippage_factor = self.default_slippage_bps / 10000.0

        if order.order_type == OrderType.MARKET:
            if order.side == OrderSide.BUY:
                base_price = quote.ask if quote.ask > 0 else quote.last
                fill_price = base_price * (1.0 + slippage_factor)
            else:
                base_price = quote.bid if quote.bid > 0 else quote.last
                fill_price = base_price * (1.0 - slippage_factor)

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
                fill_price = quote.last * (1.0 + slippage_factor)
            elif order.side == OrderSide.SELL and quote.last <= order.stop_price:
                fill_price = quote.last * (1.0 - slippage_factor)

        if fill_price is not None and fill_price > 0:
            if tick > 0:
                fill_price = round(round(fill_price / tick) * tick, 6)
            trade_qty = order.quantity - order.filled_quantity
            trade = Trade(
                trade_id=f"trd_{uuid.uuid4().hex[:12]}",
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                price=fill_price,
                quantity=trade_qty,
                commission=self.default_commission_per_order,
                timestamp=utc_now(),
                market=market.code,
                currency=market.currency,
                fx_rate=self._fx_rate(market.currency),
            )

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

            order.status = OrderStatus.REJECTED
            order.reject_reason = "Insufficient buying power or invalid position"
            order.updated_at = utc_now()

        return None

    def get_market_clock(self, market: Optional[str] = None) -> Optional[dict]:
        from app.core.markets import MARKETS, market_clock

        return market_clock(MARKETS.require(market or "US"))
