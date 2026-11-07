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
    # Multi-market context. Defaults keep every existing US-only caller valid.
    market: str = "US"
    currency: str = "USD"
    prev_close: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    source: str = "us"

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
    market: str = "US"
    currency: str = "USD"
    # USD per unit of the traded currency at execution time.
    fx_rate: float = 1.0


class Order(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    market: str = "US"
    currency: str = "USD"
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
    # Multi-market context. ``avg_cost``/``current_price``/``realized_pnl`` are
    # always in the position's native currency; the *_usd figures are the base
    # currency view used for account totals.
    market: str = "US"
    currency: str = "USD"
    cost_basis_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    # Exchange-local date of the most recent buy, used for T+1 restrictions.
    bought_on: Optional[str] = None

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

    def market_value_usd(self, usd_per_unit: float = 1.0) -> float:
        rate = 1.0 if self.currency == "USD" else usd_per_unit
        return round(self.market_value * rate, 2)

    def unrealized_pnl_usd(self, usd_per_unit: float = 1.0) -> float:
        if self.currency == "USD":
            return self.unrealized_pnl
        rate = usd_per_unit
        if self.quantity == 0:
            return 0.0
        return round(self.market_value * rate - self.cost_basis_usd, 2)


class Portfolio(BaseModel):
    cash: float = 10000.0
    initial_capital: float = 10000.0
    positions: Dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    # USD per unit of each held currency, stamped before serialization so the
    # client can render converted totals consistently with the server.
    fx_rates: Dict[str, float] = Field(default_factory=lambda: {"USD": 1.0})

    def position_usd(self, position: Position) -> float:
        return position.market_value_usd(self.fx_rates.get(position.currency, 1.0))

    @property
    def market_value(self) -> float:
        return round(sum(self.position_usd(pos) for pos in self.positions.values()), 2)

    @property
    def total_equity(self) -> float:
        return round(self.cash + self.market_value, 2)

    @property
    def unrealized_pnl(self) -> float:
        return round(
            sum(
                pos.unrealized_pnl_usd(self.fx_rates.get(pos.currency, 1.0))
                for pos in self.positions.values()
            ),
            2,
        )

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)

    @property
    def total_pnl_pct(self) -> float:
        if self.initial_capital == 0:
            return 0.0
        return round(((self.total_equity - self.initial_capital) / self.initial_capital) * 100.0, 2)
