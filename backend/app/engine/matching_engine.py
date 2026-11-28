"""
Virtual Paper Trading Matching Engine simulating exchange fills and order routing.

The engine enforces the same market rules a real venue would (board lots, tick
sizes, daily price bands, T+1) before an order is accepted, and books foreign
currency fills through the ledger's FX conversion.

Threading: the book is written by the feed thread (``on_quote``) and read by
HTTP worker threads (FastAPI runs the sync order routes in its threadpool).
Every read and write of ``orders`` / ``latest_quotes`` happens under one
re-entrant book lock, which also guards the ledger snapshot written to disk.
Without it an order insert could make a concurrent iteration raise
``dictionary changed size during iteration`` — and because the order row is
inserted *before* it is matched, that exception reached the client as a 500
after the fill had already been booked, inviting a duplicate retry.
"""
from __future__ import annotations

import logging
import threading
import time
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
from app.engine.state_store import JsonStateStore

logger = logging.getLogger(__name__)

# Bump when the snapshot layout changes incompatibly; older files are ignored.
# v1 files may also carry a ``trades`` key, which is no longer written (fills
# are served through ``order.fills``) and is deliberately ignored on restore.
STATE_VERSION = 1

# A failing write must not be retried at quote rate (that re-serializes the
# whole book and fsyncs on the feed thread for every tick).
SAVE_BACKOFF_INITIAL = 1.0
SAVE_BACKOFF_MAX = 30.0


class VirtualMatchingEngine(BaseBroker):
    def __init__(
        self,
        ledger: PortfolioLedger,
        default_slippage_bps: float = 0.0,
        default_commission_per_order: float = 0.0,
        fx=None,
        enforce_sessions: bool = False,
        enforce_sessions_for: frozenset | None = None,
        state_store: Optional[JsonStateStore] = None,
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
        # Snapshot of the book across process restarts (None = in-memory only).
        self._store = state_store
        # Lock order is always book -> ledger; the ledger never calls back here.
        self._book_lock = threading.RLock()
        self._dirty = False
        self._save_retry_at = 0.0
        self._save_backoff = 0.0
        self._restore()

    # -- persistence ------------------------------------------------------
    def _restore(self) -> None:
        """Rebuild the book from the last snapshot, if one exists.

        Restore is all-or-nothing: a snapshot whose orders fail validation must
        not leave the positions of those orders behind, because the split book
        would then be flushed back and repeat on every boot.
        """
        if self._store is None:
            return
        try:
            snapshot = self._store.load()
        except Exception as exc:  # never let a bad snapshot stop the service
            logger.warning("Virtual book restore skipped: %s", exc)
            return
        if not snapshot:
            return
        if snapshot.get("version") != STATE_VERSION:
            logger.warning(
                "Virtual book snapshot version %r != %s; starting empty",
                snapshot.get("version"), STATE_VERSION,
            )
            return

        initial_capital = self.ledger.portfolio_snapshot().initial_capital
        try:
            raw_portfolio = snapshot.get("portfolio")
            portfolio = (
                Portfolio.model_validate(raw_portfolio) if raw_portfolio else None
            )
            if portfolio is not None:
                portfolio.positions = {
                    str(sym).upper(): pos
                    for sym, pos in portfolio.positions.items()
                }
            orders = {
                order.order_id: order
                for order in (
                    Order.model_validate(raw) for raw in snapshot.get("orders", [])
                )
            }
        except Exception as exc:
            # A structurally invalid snapshot is worse than none: quarantine it
            # so it is not re-read every boot, then start from a clean book.
            logger.warning("Virtual book snapshot rejected: %s", exc)
            self._store.quarantine("rejected", detail=str(exc))
            self.ledger.reset(initial_capital)
            self.orders = {}
            self._mark_dirty()
            self._flush()
            return

        if portfolio is not None:
            self.ledger.replace_portfolio(portfolio)
        self.orders = orders
        logger.info(
            "Virtual book restored: %d positions, %d orders",
            len(portfolio.positions) if portfolio else 0,
            len(self.orders),
        )

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _flush(self) -> None:
        """Write the book if it changed since the last successful save."""
        if self._store is None or not self._dirty:
            return
        with self._book_lock:
            if not self._dirty:
                return
            now = time.monotonic()
            if now < self._save_retry_at:
                return
            snapshot = {
                "version": STATE_VERSION,
                "saved_at": utc_now().isoformat(),
                "portfolio": self.ledger.portfolio_snapshot().model_dump(mode="json"),
                "orders": [o.model_dump(mode="json") for o in self.orders.values()],
            }
            if self._store.save(snapshot):
                self._dirty = False
                self._save_backoff = 0.0
                self._save_retry_at = 0.0
                return
            # Back off instead of re-serializing the book on every quote.
            self._save_backoff = min(
                max(self._save_backoff * 2.0, SAVE_BACKOFF_INITIAL), SAVE_BACKOFF_MAX
            )
            self._save_retry_at = now + self._save_backoff

    # -- reads ------------------------------------------------------------
    def get_portfolio(self) -> Portfolio:
        return self.ledger.get_portfolio()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.ledger.get_position(symbol)

    def get_orders(self) -> List[Order]:
        # Copies: a caller serializing a live Order could otherwise observe the
        # feed thread half-way through a status/fill update.
        with self._book_lock:
            return [order.model_copy(deep=True) for order in self.orders.values()]

    def get_order(self, order_id: str) -> Optional[Order]:
        with self._book_lock:
            order = self.orders.get(order_id)
            return order.model_copy(deep=True) if order is not None else None

    def get_equity_history(self, period: str = "1M", timeframe: str = "1D") -> Optional[dict]:
        """Flat synthetic equity curve for the virtual ledger.

        The venue ledger records no time series, so the curve is the current
        total_equity sampled on the same period/timeframe grid Alpaca uses —
        honest as long as no trades happened, which is exactly the state in
        which the merged /assets chart would otherwise show only the US
        account (START $100k vs card $200k mismatch, 2026-11-28 GUI test).
        """
        equity = self.ledger.get_portfolio().total_equity
        if equity <= 0:
            return None
        seconds = self._equity_history_period_seconds(period)
        step = self._equity_history_step_seconds(timeframe)
        if seconds <= 0 or step <= 0:
            return None
        now = int(time.time())
        count = max(2, min(int(seconds // step) + 1, 400))
        points = [
            {"time": now - (count - 1 - i) * step, "equity": equity}
            for i in range(count)
        ]
        return {"points": points, "base_value": equity}

    @staticmethod
    def _equity_history_period_seconds(period: str) -> int:
        table = {"1W": 7, "1M": 30, "3M": 90, "1A": 365, "ALL": 730}
        return int(table.get(period.upper(), 30) * 86400)

    @staticmethod
    def _equity_history_step_seconds(timeframe: str) -> int:
        table = {"5MIN": 300, "15MIN": 900, "1H": 3600, "1D": 86400}
        return int(table.get(timeframe.upper(), 86400))

    # -- lifecycle --------------------------------------------------------
    def reset(self, initial_capital: float = 100000.0) -> None:
        with self._book_lock:
            self.ledger.reset(initial_capital)
            self.orders.clear()
            self.latest_quotes.clear()
            self._mark_dirty()
            self._flush()

    def register_fill_listener(self, listener: Callable[[Trade, Order], None]) -> None:
        self.fill_listeners.append(listener)

    # -- execution --------------------------------------------------------
    def _session_guarded(self, market) -> bool:
        return self._enforce_sessions or market.code in self._enforce_sessions_for

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
        # The virtual engine has no expiry semantics — time_in_force is
        # accepted for signature parity and ignored.
        ref = resolve_symbol(symbol)
        if ref is None:
            raise ValueError(f"Invalid symbol: {symbol!r}")
        market = ref.market
        canonical = ref.symbol

        with self._book_lock:
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

            # A market order depends on a live session: it would otherwise fill
            # against the previous close as soon as a quote arrived.
            if (
                self._session_guarded(market)
                and order_type == OrderType.MARKET
                and not is_market_open(market)
            ):
                raise ValueError(
                    f"{market.code} market is closed; submit a limit order "
                    "(it will rest until the session opens) or retry when the "
                    "session opens"
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
            self._mark_dirty()

            if quote:
                self._try_match_order(order, quote)

            self._flush()

            return order

    def cancel_order(self, order_id: str) -> Optional[Order]:
        with self._book_lock:
            order = self.orders.get(order_id)
            if not order:
                return None
            if order.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
                order.status = OrderStatus.CANCELLED
                order.updated_at = utc_now()
                self._mark_dirty()
                self._flush()
            return order

    def on_quote(self, quote: Quote) -> List[Trade]:
        """
        Process quote update:
        1. Cache latest quote
        2. Update ledger valuation
        3. Match active pending orders
        """
        symbol = quote.symbol.upper()
        with self._book_lock:
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

            # Quotes arrive continuously; only touch the disk when the book moved.
            self._flush()

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
        # While the venue is shut the only quote available is the previous
        # session's close. Matching against it would book a trade at a price
        # that never traded, so let the order rest until the session opens.
        if self._session_guarded(market) and not is_market_open(market):
            return None

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
                self._mark_dirty()
                return None

            if order.side == OrderSide.BUY and quote.last <= order.price:
                fill_price = min(quote.last, order.price)
            elif order.side == OrderSide.SELL and quote.last >= order.price:
                fill_price = max(quote.last, order.price)

        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "Stop order requires stop_price"
                self._mark_dirty()
                return None

            # A STOP carrying a limit price is a stop-limit: trigger at
            # stop_price, then fill like a limit (same fill-at-improvement
            # simplification the LIMIT branch makes). Without a price it is
            # a stop-market: fill at the trigger quote ± slippage.
            if order.side == OrderSide.BUY and quote.last >= order.stop_price:
                fill_price = (
                    min(quote.last, order.price)
                    if order.price
                    else quote.last * (1.0 + slippage_factor)
                )
            elif order.side == OrderSide.SELL and quote.last <= order.stop_price:
                fill_price = (
                    max(quote.last, order.price)
                    if order.price
                    else quote.last * (1.0 - slippage_factor)
                )

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
                order.filled_at = order.filled_at or utc_now()
                order.updated_at = utc_now()
                self._mark_dirty()

                for listener in self.fill_listeners:
                    try:
                        listener(trade, order)
                    except Exception:
                        pass
                return trade

            order.status = OrderStatus.REJECTED
            order.reject_reason = "Insufficient buying power or invalid position"
            order.updated_at = utc_now()
            self._mark_dirty()

        return None

    def resting_order_symbols(self) -> List[str]:
        """Symbols with orders still waiting to execute.

        The feed-activation loop in ``main`` polls these on the intl venues:
        a resting stop-limit for a symbol nobody is watching would otherwise
        sit without quotes and never trigger (2026-11-16 review).
        """
        with self._book_lock:
            return sorted({
                order.symbol
                for order in self.orders.values()
                if order.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
            })

    def get_market_clock(self, market: Optional[str] = None) -> Optional[dict]:
        from app.core.markets import MARKETS, market_clock

        return market_clock(MARKETS.require(market or "US"))
