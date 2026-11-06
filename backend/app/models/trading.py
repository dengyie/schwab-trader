"""
Core domain models for US stock trading, paper simulation, and market data.
"""
from __future__ import annotations
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    TRAILING_STOP = "TRAILING_STOP"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"
    EXT = "EXT"  # Extended hours (pre/post market)


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int = 0
    timestamp: datetime = Field(default_factory=utc_now)

    @property
    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2.0, 4)
        return self.last


class Bar(BaseModel):
    time: int  # UNIX timestamp (seconds)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Trade(BaseModel):
    trade_id: str
    order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: int
    commission: float = 0.0
    timestamp: datetime = Field(default_factory=utc_now)


class Order(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_avg_price: float = 0.0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    reject_reason: Optional[str] = None


class Position(BaseModel):
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return round(self.quantity * self.current_price, 2)

    @property
    def total_cost(self) -> float:
        return round(self.quantity * self.avg_cost, 2)

    @property
    def unrealized_pnl(self) -> float:
        if self.quantity == 0:
            return 0.0
        return round((self.current_price - self.avg_cost) * self.quantity, 2)

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.quantity == 0 or self.avg_cost == 0:
            return 0.0
        return round(((self.current_price - self.avg_cost) / self.avg_cost) * 100.0, 2)


class Portfolio(BaseModel):
    cash: float = 10000.0
    initial_capital: float = 10000.0
    positions: Dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return round(sum(pos.market_value for pos in self.positions.values()), 2)

    @property
    def total_equity(self) -> float:
        return round(self.cash + self.market_value, 2)

    @property
    def unrealized_pnl(self) -> float:
        return round(sum(pos.unrealized_pnl for pos in self.positions.values()), 2)

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)

    @property
    def total_pnl_pct(self) -> float:
        if self.initial_capital == 0:
            return 0.0
        return round(((self.total_equity - self.initial_capital) / self.initial_capital) * 100.0, 2)
