"""
Routes orders and account queries to the broker that owns the symbol's market.

US symbols go to the configured execution broker (Alpaca paper by default).
HK / JP / CN are executed by the virtual matching engine with full per-market
rule enforcement. The facade merges the two sides into one account view so the
API and websocket never need to know how many brokers are behind `broker`.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from app.core.markets import MARKETS, market_clock, resolve_symbol
from app.models.trading import (
    Order,
    OrderSide,
    OrderType,
    Position,
    Portfolio,
    Quote,
    Trade,
)
from app.engine.base_broker import BaseBroker

logger = logging.getLogger(__name__)


class MarketRouterBroker(BaseBroker):
    def __init__(
        self,
        us_broker: BaseBroker,
        other_broker: BaseBroker,
        us_market_code: str = "US",
    ):
        self._us = us_broker
        self._other = other_broker
        self._us_code = us_market_code.upper()

    # -- routing ----------------------------------------------------------
    def _broker_for(self, symbol: object) -> Optional[tuple[str, BaseBroker]]:
        ref = resolve_symbol(symbol)
        if ref is None:
            return None
        if ref.market.code == self._us_code:
            return ref.symbol, self._us
        return ref.symbol, self._other

    def _all_brokers(self) -> List[BaseBroker]:
        if self._us is self._other:
            return [self._us]
        return [self._us, self._other]

    # -- portfolio --------------------------------------------------------
    def get_portfolio(self) -> Portfolio:
        portfolios = []
        for broker in self._all_brokers():
            try:
                portfolios.append(broker.get_portfolio())
            except Exception as exc:
                logger.warning("Portfolio refresh failed for %s: %s", type(broker).__name__, exc)

        if not portfolios:
            raise RuntimeError("No broker reported a portfolio")

        # First portfolio is the reference for cash/initial capital; every
        # position from any broker is folded in, keyed by canonical symbol.
        merged = Portfolio(
            cash=sum(p.cash for p in portfolios),
            initial_capital=sum(p.initial_capital for p in portfolios),
            positions={},
            realized_pnl=sum(p.realized_pnl for p in portfolios),
            fx_rates=dict(portfolios[0].fx_rates),
        )
        for portfolio in portfolios:
            for symbol, pos in portfolio.positions.items():
                merged.positions[symbol] = pos
        # Currency rates are process-wide; take the most complete set available.
        for portfolio in portfolios:
            for currency, rate in portfolio.fx_rates.items():
                merged.fx_rates.setdefault(currency, rate)
        return merged

    def get_position(self, symbol: str) -> Optional[Position]:
        route = self._broker_for(symbol)
        if route is None:
            return None
        canonical, broker = route
        return broker.get_position(canonical)

    # -- orders -----------------------------------------------------------
    def get_orders(self) -> List[Order]:
        orders: List[Order] = []
        for broker in self._all_brokers():
            try:
                orders.extend(broker.get_orders())
            except Exception as exc:
                logger.warning("Order list failed for %s: %s", type(broker).__name__, exc)
        return sorted(orders, key=lambda o: o.created_at, reverse=True)

    def get_order(self, order_id: str) -> Optional[Order]:
        for broker in self._all_brokers():
            order = broker.get_order(order_id)
            if order is not None:
                return order
        return None

    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Order:
        route = self._broker_for(symbol)
        if route is None:
            raise ValueError(f"Invalid symbol: {symbol!r}")
        canonical, broker = route
        return broker.submit_order(
            symbol=canonical,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
        )

    def cancel_order(self, order_id: str) -> Optional[Order]:
        for broker in self._all_brokers():
            order = broker.cancel_order(order_id)
            if order is not None:
                return order
        return None

    # -- feed-through -----------------------------------------------------
    def on_quote(self, quote: Quote) -> List[Trade]:
        trades: List[Trade] = []
        for broker in self._all_brokers():
            try:
                trades.extend(broker.on_quote(quote))
            except Exception as exc:
                logger.warning("on_quote failed for %s: %s", type(broker).__name__, exc)
        return trades

    def register_fill_listener(self, listener: Callable[[Trade, Order], None]) -> None:
        for broker in self._all_brokers():
            broker.register_fill_listener(listener)

    def reset(self, initial_capital: float = 100000.0) -> None:
        for broker in self._all_brokers():
            try:
                broker.reset(initial_capital)
            except Exception as exc:
                logger.warning("Reset failed for %s: %s", type(broker).__name__, exc)

    def get_fx_rates(self) -> dict:
        """USD-per-unit rates for every currency the account can hold."""
        ledger = getattr(self._other, "ledger", None)
        if ledger is not None:
            try:
                ledger.refresh_fx()
                return ledger.fx_rates()
            except Exception as exc:
                logger.warning("FX refresh failed: %s", exc)
        return {"USD": 1.0}

    def get_market_clock(self, market: Optional[str] = None) -> Optional[dict]:
        """Clock for a specific market; US consults Alpaca when available."""
        code = (market or "").strip().upper()
        if not code:
            code = self._us_code
        try:
            market_def = MARKETS.require(code)
        except ValueError:
            return None

        if code == self._us_code and self._us != self._other:
            live = self._us.get_market_clock()
            if live:
                return {**market_clock(market_def), **live}
        return market_clock(market_def)
