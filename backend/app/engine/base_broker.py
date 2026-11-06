"""
Abstract Broker interface for multi-engine order routing and account ledger management.
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Callable
from app.models.trading import (
    Order, OrderSide, OrderType, Trade, Quote, Position, Portfolio
)


class BaseBroker(ABC):
    """
    Abstract base class for brokerage execution adapters.
    Unifies local VirtualMatchingEngine and remote AlpacaPaperBroker.
    """

    @abstractmethod
    def get_portfolio(self) -> Portfolio:
        """Returns the current portfolio snapshot."""
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        """Returns open position for a symbol if any."""
        pass

    @abstractmethod
    def get_orders(self) -> List[Order]:
        """Returns list of active and recent orders."""
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """Returns single order by ID."""
        pass

    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        stop_price: Optional[float] = None
    ) -> Order:
        """Submits an order to the execution engine."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> Optional[Order]:
        """Cancels an existing order."""
        pass

    @abstractmethod
    def on_quote(self, quote: Quote) -> List[Trade]:
        """Processes market quote update for mark-to-market or local fills."""
        pass

    @abstractmethod
    def register_fill_listener(self, listener: Callable[[Trade, Order], None]) -> None:
        """Registers callback for order fill events."""
        pass

    @abstractmethod
    def reset(self, initial_capital: float = 100000.0) -> None:
        """Resets the broker state / paper ledger."""
        pass

    def get_market_clock(self) -> Optional[dict]:
        """
        Trading-session clock: {"is_open": bool, "next_open": iso, "next_close": iso}.

        Returns None when the broker cannot be asked. The market-data layer uses
        this so a quiet market is not reported as a broken live feed.
        """
        return None
