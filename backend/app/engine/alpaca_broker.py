"""
Alpaca Paper Trading Broker implementation.
Routes orders to Alpaca Paper Trading REST API and syncs cloud portfolio state.
"""
from typing import Dict, List, Optional, Callable
import time
import httpx
from app.models.trading import (
    Order, OrderSide, OrderType, OrderStatus, Trade, Quote, Position, Portfolio, utc_now
)
from app.adapters.alpaca_adapter import AlpacaDataAdapter
from app.engine.base_broker import BaseBroker
from app.core.config import settings


class AlpacaPaperBroker(BaseBroker):
    """
    Broker implementation delegating execution and account bookkeeping
    directly to Alpaca Markets Paper Trading REST endpoints.
    Includes in-memory TTL caching to avoid blocking HTTP event loops.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None
    ):
        self.api_key = api_key or settings.alpaca_api_key or ""
        self.api_secret = api_secret or settings.alpaca_api_secret or ""
        self.base_url = (base_url or settings.alpaca_paper_base_url).rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json"
        }
        self.client = httpx.Client(headers=self.headers, timeout=10.0)
        self.fill_listeners: List[Callable[[Trade, Order], None]] = []
        # Local quote cache for fast mark-to-market
        self.latest_quotes: Dict[str, Quote] = {}
        # Cached orders for tracking
        self.cached_orders: Dict[str, Order] = {}
        self.orders = self.cached_orders
        self.cached_portfolio: Optional[Portfolio] = None

        # TTL cache trackers (5-second freshness)
        self._portfolio_cached_at: float = 0.0
        self._portfolio_ttl: float = 5.0
        self._orders_cached_at: float = 0.0
        self._orders_ttl: float = 5.0

        # Fill detection state: last observed filled quantity per order
        self._last_order_status: Dict[str, OrderStatus] = {}
        self._last_filled_qty: Dict[str, int] = {}

    def register_fill_listener(self, listener: Callable[[Trade, Order], None]) -> None:
        self.fill_listeners.append(listener)

    def get_portfolio(self, force_refresh: bool = False) -> Portfolio:
        """Fetch remote account equity and open positions from Alpaca REST API with TTL cache."""
        now = time.time()
        if not force_refresh and self.cached_portfolio and (now - self._portfolio_cached_at < self._portfolio_ttl):
            # Update position mark-to-market prices from latest streaming quotes
            for sym, pos in self.cached_portfolio.positions.items():
                if sym in self.latest_quotes:
                    pos.current_price = self.latest_quotes[sym].last
            return self.cached_portfolio

        try:
            acc_resp = self.client.get(f"{self.base_url}/account")
            acc_resp.raise_for_status()
            account_data = acc_resp.json()

            pos_resp = self.client.get(f"{self.base_url}/positions")
            pos_resp.raise_for_status()
            positions_data = pos_resp.json()

            portfolio = AlpacaDataAdapter.parse_account_to_portfolio(account_data, positions_data)

            # Apply latest quotes if available for real-time unrealized PnL
            for sym, pos in portfolio.positions.items():
                if sym in self.latest_quotes:
                    pos.current_price = self.latest_quotes[sym].last

            self.cached_portfolio = portfolio
            self._portfolio_cached_at = now
            return portfolio
        except Exception as exc:
            # Serve the last known good snapshot if we have one...
            if self.cached_portfolio:
                return self.cached_portfolio
            # ...otherwise fail loudly: a fabricated $100k account must never be shown.
            raise RuntimeError(f"Alpaca account unavailable and no cached portfolio: {exc}")

    def get_position(self, symbol: str) -> Optional[Position]:
        p = self.get_portfolio()
        return p.positions.get(symbol.upper())

    def get_orders(self, force_refresh: bool = False) -> List[Order]:
        """Fetch all recent orders from Alpaca with TTL cache.

        Also detects newly filled quantity and fires fill listeners so the
        frontend receives ORDER_FILLED pushes in near-real-time instead of
        waiting for the 30s portfolio poll.
        """
        now = time.time()
        if not force_refresh and self.cached_orders and (now - self._orders_cached_at < self._orders_ttl):
            return list(self.cached_orders.values())

        try:
            resp = self.client.get(f"{self.base_url}/orders", params={"status": "all", "limit": 50})
            resp.raise_for_status()
            orders_data = resp.json()
            domain_orders = [AlpacaDataAdapter.parse_order_to_domain(o) for o in orders_data]
            for o in domain_orders:
                self._detect_fill(o)
                self.cached_orders[o.order_id] = o
            self._orders_cached_at = now
            return domain_orders
        except Exception:
            return list(self.cached_orders.values())

    def _detect_fill(self, order: Order) -> None:
        """Emit a fill event for newly filled quantity since the last poll."""
        was_known = order.order_id in self._last_order_status
        prev_filled = self._last_filled_qty.get(order.order_id, 0)
        newly = order.filled_quantity - prev_filled

        self._last_order_status[order.order_id] = order.status
        self._last_filled_qty[order.order_id] = order.filled_quantity

        if not was_known or newly <= 0 or order.filled_avg_price <= 0:
            return
        trade = Trade(
            trade_id=f"{order.order_id}-{order.filled_quantity}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.filled_avg_price,
            quantity=newly,
        )
        for listener in list(self.fill_listeners):
            try:
                listener(trade, order)
            except Exception:
                pass

    def get_order(self, order_id: str) -> Optional[Order]:
        try:
            resp = self.client.get(f"{self.base_url}/orders/{order_id}")
            if resp.status_code == 200:
                ord_obj = AlpacaDataAdapter.parse_order_to_domain(resp.json())
                self.cached_orders[ord_obj.order_id] = ord_obj
                return ord_obj
        except Exception:
            pass
        return self.cached_orders.get(order_id)

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

        payload = {
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": "buy" if side == OrderSide.BUY else "sell",
            "type": order_type.value.lower(),
            "time_in_force": "day"
        }
        if order_type == OrderType.LIMIT:
            if price is None:
                raise ValueError("Limit order requires price")
            payload["limit_price"] = str(round(price, 2))
        elif order_type == OrderType.STOP:
            if stop_price is None:
                raise ValueError("Stop order requires stop_price")
            payload["stop_price"] = str(round(stop_price, 2))

        try:
            resp = self.client.post(f"{self.base_url}/orders", json=payload)
            resp.raise_for_status()
            order = AlpacaDataAdapter.parse_order_to_domain(resp.json())
            self.cached_orders[order.order_id] = order
            # Prime fill tracking; any filled quantity reported later is a new fill.
            self._last_order_status[order.order_id] = order.status
            self._last_filled_qty[order.order_id] = 0
            # Invalidate caches to reflect new trade immediately
            self._portfolio_cached_at = 0.0
            self._orders_cached_at = 0.0
            return order
        except httpx.HTTPStatusError as e:
            try:
                err_detail = e.response.json().get("message", e.response.text)
            except Exception:
                err_detail = str(e)
            raise ValueError(f"Alpaca Order Submission Failed: {err_detail}")
        except Exception as e:
            raise ValueError(f"Alpaca Order Submission Error: {str(e)}")

    def cancel_order(self, order_id: str) -> Optional[Order]:
        try:
            resp = self.client.delete(f"{self.base_url}/orders/{order_id}")
            if resp.status_code in (200, 204):
                self._orders_cached_at = 0.0
                if order_id in self.cached_orders:
                    self.cached_orders[order_id].status = OrderStatus.CANCELLED
                    self.cached_orders[order_id].updated_at = utc_now()
                    return self.cached_orders[order_id]
                return self.get_order(order_id)
        except Exception:
            pass
        return self.cached_orders.get(order_id)

    def on_quote(self, quote: Quote) -> List[Trade]:
        """Update local quote cache and mark-to-market prices for positions."""
        self.latest_quotes[quote.symbol.upper()] = quote
        if self.cached_portfolio and quote.symbol.upper() in self.cached_portfolio.positions:
            self.cached_portfolio.positions[quote.symbol.upper()].current_price = quote.last
        return []

    def reset(self, initial_capital: float = 100000.0) -> None:
        """Cancel all open orders and close positions via Alpaca REST."""
        try:
            self.client.delete(f"{self.base_url}/orders")
            self.client.delete(f"{self.base_url}/positions")
        except Exception:
            pass
        self.cached_orders.clear()
        self.cached_portfolio = None
        self._last_order_status.clear()
        self._last_filled_qty.clear()

    def get_market_clock(self) -> Optional[dict]:
        """Alpaca trading clock: tells the UI 'market closed' vs 'feed degraded'."""
        try:
            resp = self.client.get(f"{self.base_url}/clock")
            if resp.status_code != 200:
                return None
            data = resp.json()
            return {
                "is_open": bool(data.get("is_open")),
                "next_open": data.get("next_open"),
                "next_close": data.get("next_close"),
            }
        except Exception:
            return None
