"""
High-precision portfolio ledger managing positions, cash balances, and P&L.
"""
from __future__ import annotations
from typing import Dict, List, Optional
from datetime import datetime
from app.models.trading import Portfolio, Position, Trade, OrderSide, Quote


class PortfolioLedger:
    def __init__(self, initial_capital: float = 10000.0):
        self.portfolio = Portfolio(cash=initial_capital, initial_capital=initial_capital)
        self.trades: List[Trade] = []

    def get_portfolio(self) -> Portfolio:
        return self.portfolio

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.portfolio.positions.get(symbol.upper())

    def update_quote(self, quote: Quote) -> None:
        """Mark-to-market revaluation on fresh quote."""
        symbol = quote.symbol.upper()
        if symbol in self.portfolio.positions:
            pos = self.portfolio.positions[symbol]
            pos.current_price = quote.last

    def apply_trade(self, trade: Trade) -> bool:
        """
        Execute trade against portfolio cash and position.
        Returns True if successfully settled, False if insufficient buying power.
        """
        symbol = trade.symbol.upper()
        trade_total = trade.price * trade.quantity
        total_debit = trade_total + trade.commission

        if trade.side == OrderSide.BUY:
            if self.portfolio.cash < total_debit:
                return False

            self.portfolio.cash = round(self.portfolio.cash - total_debit, 4)
            if symbol not in self.portfolio.positions:
                self.portfolio.positions[symbol] = Position(
                    symbol=symbol,
                    quantity=trade.quantity,
                    avg_cost=trade.price,
                    current_price=trade.price
                )
            else:
                pos = self.portfolio.positions[symbol]
                new_qty = pos.quantity + trade.quantity
                if new_qty > 0:
                    pos.avg_cost = round(((pos.quantity * pos.avg_cost) + trade_total) / new_qty, 4)
                pos.quantity = new_qty
                pos.current_price = trade.price

        elif trade.side == OrderSide.SELL:
            pos = self.portfolio.positions.get(symbol)
            if not pos or pos.quantity < trade.quantity:
                return False  # Disallow naked shorting in standard paper cash account

            net_proceeds = round(trade_total - trade.commission, 4)
            self.portfolio.cash = round(self.portfolio.cash + net_proceeds, 4)

            # Realized P&L calculation
            trade_cost_basis = pos.avg_cost * trade.quantity
            trade_realized = (trade.price * trade.quantity) - trade_cost_basis - trade.commission

            pos.realized_pnl = round(pos.realized_pnl + trade_realized, 4)
            self.portfolio.realized_pnl = round(self.portfolio.realized_pnl + trade_realized, 4)

            pos.quantity -= trade.quantity
            pos.current_price = trade.price

            # If fully closed, retain record with 0 qty for historical PnL
            if pos.quantity == 0:
                pos.avg_cost = 0.0

        self.trades.append(trade)
        return True

    def reset(self, initial_capital: Optional[float] = None) -> None:
        cap = initial_capital if initial_capital is not None else self.portfolio.initial_capital
        self.portfolio = Portfolio(cash=cap, initial_capital=cap)
        self.trades.clear()
