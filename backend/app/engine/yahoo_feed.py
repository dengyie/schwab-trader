"""
Quote and bar feed for non-US markets, backed by Yahoo Finance.

The US path keeps Alpaca's streaming websocket; every other venue is served by
this polling feed. It only polls symbols that a client is actually looking at
(websocket subscription or an on-demand REST read), so the number of upstream
requests stays proportional to the open screens rather than the catalog size.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.core.markets import (
    is_market_open,
    market_of,
    resolve_symbol,
    round_to_tick,
    tick_size_for,
)
from app.core.timeframes import TIMEFRAME_TTLS
from app.models.trading import Bar, Quote, utc_now

logger = logging.getLogger(__name__)


class YahooQuoteFeed:
    """Polling feed for HK / JP / CN (and any symbol outside the US stream)."""

    POLL_SECONDS = 6.0
    IDLE_POLL_SECONDS = 30.0
    CACHE_SECONDS = 4.0
    BAR_CACHE_SECONDS = 20.0
    # A REST read keeps a symbol warm for this long, then the poller drops it.
    ON_DEMAND_TTL = 180.0

    def __init__(self, symbols: Optional[List[str]] = None, enabled: bool = True):
        self._catalog = {s.upper() for s in (symbols or [])}
        # Sticky watch set (websocket subscriptions) vs. short-lived reads.
        self._active: Dict[str, float] = {}
        self._on_demand: Dict[str, float] = {}
        self._fetched_at: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._enabled = enabled
        self._running = False
        self._task: Optional[asyncio.Task] = None

        self.latest_quotes: Dict[str, Quote] = {}
        self.last_trade_prices: Dict[str, float] = {}
        self.historical_bars: Dict[str, List[Bar]] = {}
        self._bars_cache: Dict[str, tuple[float, List[Bar]]] = {}

        self.quote_listeners: List[Callable[[Quote], None]] = []

    # -- introspection ----------------------------------------------------
    @property
    def symbols(self) -> List[str]:
        with self._lock:
            return sorted(self._catalog | set(self._active) | set(self._on_demand))

    def activate(self, symbol: str) -> None:
        """Watch a symbol until ``deactivate`` (websocket subscription)."""
        ref = resolve_symbol(symbol)
        if ref is None:
            return
        with self._lock:
            self._active[ref.symbol] = time.time()

    def deactivate(self, symbol: str) -> None:
        ref = resolve_symbol(symbol)
        if ref is None:
            return
        with self._lock:
            self._active.pop(ref.symbol, None)

    def _touch(self, symbol: str) -> None:
        """Keep a symbol warm for one on-demand TTL after a REST read."""
        if symbol in self._active:
            return
        with self._lock:
            self._on_demand[symbol] = time.time()

    # -- quote fetch ------------------------------------------------------
    def _fetch_and_store(self, symbol: str) -> Optional[Quote]:
        ref = resolve_symbol(symbol)
        if ref is None:
            return None
        market = ref.market
        meta = YahooFinanceAdapter.fetch_meta(ref.symbol)
        if not meta:
            return None

        price = None
        for key in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
            value = meta.get(key)
            if isinstance(value, (int, float)) and value > 0:
                price = float(value)
                break
        if price is None:
            return None

        prev_raw = meta.get("previousClose") or meta.get("chartPreviousClose")
        prev_close = float(prev_raw) if isinstance(prev_raw, (int, float)) else None

        tick = tick_size_for(market, price)
        spread = max(tick, price * 0.0004)
        bid = round_to_tick(max(tick, price - spread / 2.0), tick)
        ask = round_to_tick(price + spread / 2.0, tick)

        trade_time = meta.get("regularMarketTime")
        if isinstance(trade_time, (int, float)) and trade_time > 0:
            stamp = datetime.fromtimestamp(float(trade_time), tz=timezone.utc)
        else:
            stamp = utc_now()

        volume = meta.get("regularMarketVolume")
        quote = Quote(
            symbol=ref.symbol,
            bid=bid,
            ask=ask,
            last=price,
            volume=int(volume) if isinstance(volume, (int, float)) else 0,
            timestamp=stamp,
            market=market.code,
            currency=market.currency,
            prev_close=prev_close,
            day_high=float(meta["regularMarketDayHigh"])
            if isinstance(meta.get("regularMarketDayHigh"), (int, float))
            else None,
            day_low=float(meta["regularMarketDayLow"])
            if isinstance(meta.get("regularMarketDayLow"), (int, float))
            else None,
            source="yahoo",
        )

        with self._lock:
            self.latest_quotes[ref.symbol] = quote
            self.last_trade_prices[ref.symbol] = price
            self._fetched_at[ref.symbol] = time.time()
        return quote

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        ref = resolve_symbol(symbol)
        if ref is None:
            return None
        self._touch(ref.symbol)
        with self._lock:
            cached = self.latest_quotes.get(ref.symbol)
            fetched_at = self._fetched_at.get(ref.symbol, 0.0)
        if cached is not None and time.time() - fetched_at < self.CACHE_SECONDS:
            return cached
        if not self._enabled:
            # Offline / test mode: never reach the network, never invent a quote.
            return cached
        quote = self._fetch_and_store(ref.symbol)
        return quote or cached

    # -- bars -------------------------------------------------------------
    def get_bars(self, symbol: str, timeframe: str = "15m") -> List[Bar]:
        ref = resolve_symbol(symbol)
        if ref is None:
            return []
        self._touch(ref.symbol)
        key = f"{ref.symbol}:{timeframe.lower()}"
        now = time.time()
        with self._lock:
            cached = self._bars_cache.get(key)
        ttl = max(self.BAR_CACHE_SECONDS, TIMEFRAME_TTLS.get(timeframe.lower(), 30.0))
        if cached and now - cached[0] < ttl:
            return cached[1]
        if not self._enabled:
            return cached[1] if cached else []

        tz = ZoneInfo(ref.market.timezone)
        bars = YahooFinanceAdapter.fetch_bars(ref.symbol, timeframe=timeframe, tz=tz)
        if bars:
            with self._lock:
                self._bars_cache[key] = (now, bars)
            return bars
        return cached[1] if cached else []

    def get_historical_bars(self, symbol: str) -> List[Bar]:
        return self.get_bars(symbol, timeframe="1m")

    # -- listeners --------------------------------------------------------
    def subscribe_quote(self, callback: Callable[[Quote], None]) -> None:
        self.quote_listeners.append(callback)

    def subscribe_bar(self, callback: Callable[[str, Bar], None]) -> None:
        # Non-US bars are pulled on demand; the feed never pushes closed bars.
        return None

    # -- loop -------------------------------------------------------------
    async def start(self) -> None:
        if self._running or not self._enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                active = self._pick_watched()
                if not active:
                    await asyncio.sleep(self.IDLE_POLL_SECONDS)
                    continue

                any_open = any(is_market_open(market_of(sym)) for sym in active)
                interval = self.POLL_SECONDS if any_open else self.IDLE_POLL_SECONDS
                quotes = await asyncio.gather(
                    *(asyncio.to_thread(self._fetch_if_stale, sym) for sym in active),
                    return_exceptions=True,
                )
                for quote in quotes:
                    if isinstance(quote, Quote):
                        for listener in list(self.quote_listeners):
                            try:
                                listener(quote)
                            except Exception:
                                pass
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Yahoo poll loop error: %s", exc)
                await asyncio.sleep(self.IDLE_POLL_SECONDS)

    def _pick_watched(self) -> List[str]:
        """Sticky subscriptions plus unexpired on-demand reads."""
        now = time.time()
        with self._lock:
            self._on_demand = {
                sym: ts
                for sym, ts in self._on_demand.items()
                if now - ts < self.ON_DEMAND_TTL
            }
            return sorted(set(self._active) | set(self._on_demand))

    def _fetch_if_stale(self, symbol: str) -> Optional[Quote]:
        """One upstream call per symbol per cache window, even across pollers."""
        with self._lock:
            fetched_at = self._fetched_at.get(symbol, 0.0)
        if time.time() - fetched_at < self.CACHE_SECONDS:
            return None
        return self._fetch_and_store(symbol)
