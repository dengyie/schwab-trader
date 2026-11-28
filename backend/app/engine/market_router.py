"""
Routes orders and account queries to the broker that owns the symbol's market.

US symbols go to the configured execution broker (Alpaca paper by default).
HK / JP / CN are executed by the virtual matching engine with full per-market
rule enforcement. The facade merges the two sides into one account view so the
API and websocket never need to know how many brokers are behind `broker`.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

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
            # Sum of venue purchasing powers: the Alpaca margin account
            # contributes its RegT buying power, cash-only ledgers contribute
            # their cash (Portfolio.buying_power defaults to 0 when unset).
            buying_power=sum(max(p.buying_power, p.cash) for p in portfolios),
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
        trail_value: Optional[float] = None,
        trail_type: Optional[str] = None,
        time_in_force: Optional[str] = None,
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
            trail_value=trail_value,
            trail_type=trail_type,
            time_in_force=time_in_force,
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
        """Reset every broker so the *merged* starting capital equals the request.

        Passing the full amount to each broker would make the account look
        twice as large as requested (one Alpaca wallet plus one virtual
        ledger). Split it in proportion to each broker's current starting
        capital instead, keeping the existing US / non-US split.
        """
        brokers = self._all_brokers()
        weights: List[float] = []
        for broker in brokers:
            try:
                weights.append(max(0.0, float(broker.get_portfolio().initial_capital)))
            except Exception as exc:
                logger.warning("Initial capital unavailable for %s: %s", type(broker).__name__, exc)
                weights.append(0.0)

        total_weight = sum(weights)
        if total_weight <= 0:
            weights = [1.0 for _ in brokers]
            total_weight = float(len(brokers))

        # Two decimals keeps the merged total exact to the cent.
        shares = [round(initial_capital * weight / total_weight, 2) for weight in weights]
        shares[-1] = round(initial_capital - sum(shares[:-1]), 2)

        for broker, share in zip(brokers, shares):
            try:
                broker.reset(share)
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

    def get_equity_history(self, period: str = "1M", timeframe: str = "1D") -> Optional[dict]:
        """Merged equity curve across both ledgers.

        The /assets card sums both accounts, so the curve must too —
        forwarding only the US venue used to chart $100k under a $200k card
        (2026-11-28 GUI test F6). Venues report on different grids (Alpaca:
        ET trading days; virtual ledger: now-based steps), so per-timestamp
        summing on raw stamps never overlaps and stays at $100k — each curve
        is therefore resampled onto the union grid by carry-forward before
        summing. A venue without history falls back to the other one, and
        both None stays None (explicit empty state, never a fabricated curve).
        """
        curves = []
        for broker in self._all_brokers():
            try:
                curves.append(broker.get_equity_history(period=period, timeframe=timeframe))
            except Exception as exc:
                logger.warning("Equity history failed for %s: %s", type(broker).__name__, exc)
        valid = [c for c in curves if c and c.get("points")]
        if not valid:
            return None
        if len(valid) == 1:
            return {**valid[0], "points": [dict(p) for p in valid[0]["points"]]}

        # Union grid: every timestamp any venue reported, ascending.
        grid = sorted({float(p["time"]) for c in valid for p in c["points"]})

        def resample(points: list) -> Dict[float, float]:
            """Carry each venue's last known equity forward across the grid."""
            out: Dict[float, float] = {}
            idx = 0
            last: Optional[float] = None
            for ts in grid:
                while idx < len(points) and float(points[idx]["time"]) <= ts:
                    last = float(points[idx]["equity"])
                    idx += 1
                if last is not None:
                    out[ts] = last
            return out

        resampled = [resample(c["points"]) for c in valid]
        base_sum = round(sum(float(c.get("base_value") or 0.0) for c in valid), 2)
        points = [
            {"time": ts, "equity": round(sum(r[ts] for r in resampled if ts in r), 2)}
            for ts in grid
        ]
        # Timestamps a venue never reached (its series starts later) are only
        # summed over the venues that cover them — the earliest stretch shows
        # the venues that already had history rather than a fabricated zero.
        return {"points": points, "base_value": base_sum}
