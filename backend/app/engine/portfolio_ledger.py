"""
Portfolio ledger for a multi-market, multi-currency cash account.

Positions keep their prices and costs in the exchange's native currency (what a
trader sees on the ticket), while cash, realized P&L and every account total are
kept in the base currency (USD). A trade in a foreign currency is converted at
the FX rate effective when it executes, so the account never silently gains or
loses money to a rounding shortcut.
"""
from __future__ import annotations

from typing import List, Optional

from app.core.markets import local_date, market_of, within_day_range
from app.models.trading import Portfolio, Position, Trade, OrderSide, Quote


class PortfolioLedger:
    def __init__(self, initial_capital: float = 10000.0, fx=None):
        self.portfolio = Portfolio(cash=initial_capital, initial_capital=initial_capital)
        self.trades: List[Trade] = []
        self._fx = fx

    # -- FX ---------------------------------------------------------------
    def _rate(self, currency: str, explicit: Optional[float] = None) -> float:
        if explicit and explicit > 0:
            return explicit
        if self._fx is not None:
            return self._fx.usd_per_unit(currency)
        return 1.0

    def refresh_fx(self) -> None:
        """Stamp the portfolio with the current USD-per-unit rates."""
        if self._fx is not None:
            self.portfolio.fx_rates = self._fx.all_rates()
        else:
            self.portfolio.fx_rates = {"USD": 1.0}

    # -- reads ------------------------------------------------------------
    def get_portfolio(self) -> Portfolio:
        self.refresh_fx()
        return self.portfolio

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.portfolio.positions.get(symbol.upper())

    def update_quote(self, quote: Quote) -> None:
        """Mark-to-market revaluation on fresh quote.

        A lone stale bid (thin IEX moment) can contradict the session range;
        do not let it pull the mark outside the real day high/low. Prefer a
        clean in-range print, else keep the previous mark.
        """
        symbol = quote.symbol.upper()
        pos = self.portfolio.positions.get(symbol)
        if pos is None:
            return
        # A lone stale bid (thin IEX moment) can contradict the session range;
        # do not let it pull the mark outside the real day high/low.
        if not within_day_range(quote.last, quote.day_low, quote.day_high):
            return
        pos.current_price = quote.last

    # -- writes -----------------------------------------------------------
    def apply_trade(self, trade: Trade) -> bool:
        """
        Execute trade against portfolio cash and position.
        Returns True if successfully settled, False if insufficient buying power.
        """
        symbol = trade.symbol.upper()
        market = market_of(symbol)
        currency = trade.currency or market.currency
        rate = self._rate(currency, trade.fx_rate)

        trade_total = trade.price * trade.quantity
        trade_total_usd = trade_total * rate
        # Commission is booked in USD.
        total_debit_usd = trade_total_usd + trade.commission

        if trade.side == OrderSide.BUY:
            if self.portfolio.cash < total_debit_usd:
                return False

            self.portfolio.cash = round(self.portfolio.cash - total_debit_usd, 4)
            if symbol not in self.portfolio.positions:
                self.portfolio.positions[symbol] = Position(
                    symbol=symbol,
                    quantity=trade.quantity,
                    avg_cost=trade.price,
                    current_price=trade.price,
                    market=market.code,
                    currency=currency,
                    cost_basis_usd=round(trade_total_usd, 4),
                    bought_on=local_date(market),
                )
            else:
                pos = self.portfolio.positions[symbol]
                new_qty = pos.quantity + trade.quantity
                if new_qty > 0:
                    pos.avg_cost = round(
                        ((pos.quantity * pos.avg_cost) + trade_total) / new_qty, 6
                    )
                pos.quantity = new_qty
                pos.current_price = trade.price
                pos.cost_basis_usd = round(pos.cost_basis_usd + trade_total_usd, 4)
                pos.market = market.code
                pos.currency = currency
                pos.bought_on = local_date(market)

        elif trade.side == OrderSide.SELL:
            pos = self.portfolio.positions.get(symbol)
            if not pos or pos.quantity < trade.quantity:
                return False  # Disallow naked shorting in a paper cash account

            net_proceeds_usd = round(trade_total_usd - trade.commission, 4)
            self.portfolio.cash = round(self.portfolio.cash + net_proceeds_usd, 4)

            # Realized P&L: prorate the USD cost basis of the shares sold.
            portion = trade.quantity / pos.quantity if pos.quantity else 1.0
            cost_basis_sold = pos.cost_basis_usd * portion
            realized_usd = trade_total_usd - cost_basis_sold - trade.commission

            # Native-currency realized P&L, for display next to the position.
            native_commission = trade.commission / rate if rate else trade.commission
            native_realized = (trade.price - pos.avg_cost) * trade.quantity - native_commission

            pos.realized_pnl = round(pos.realized_pnl + native_realized, 4)
            pos.realized_pnl_usd = round(pos.realized_pnl_usd + realized_usd, 4)
            self.portfolio.realized_pnl = round(
                self.portfolio.realized_pnl + realized_usd, 4
            )

            pos.cost_basis_usd = round(max(0.0, pos.cost_basis_usd - cost_basis_sold), 4)
            pos.quantity -= trade.quantity
            pos.current_price = trade.price

            # If fully closed, retain record with 0 qty for historical PnL
            if pos.quantity == 0:
                pos.avg_cost = 0.0
                pos.cost_basis_usd = 0.0
                pos.bought_on = None

        self.trades.append(trade)
        return True

    def reset(self, initial_capital: Optional[float] = None) -> None:
        cap = initial_capital if initial_capital is not None else self.portfolio.initial_capital
        self.portfolio = Portfolio(cash=cap, initial_capital=cap)
        self.trades.clear()

    # -- helpers ----------------------------------------------------------
    def symbols(self) -> List[str]:
        return list(self.portfolio.positions)
