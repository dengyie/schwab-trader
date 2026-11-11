"""
Market Data Feed Service supporting both realistic live simulation and live Schwab Streamer.
"""
from __future__ import annotations
import asyncio
import random
import time
from typing import Dict, List, Callable, Optional, Set, Tuple
from datetime import datetime, timezone
from app.models.trading import Quote, Bar, Trade, OrderSide, utc_now
from app.core.timeframes import TIMEFRAME_SECONDS


class MarketFeedService:
    def __init__(self, initial_symbols: Optional[List[str]] = None):
        self.symbols = set(sym.upper() for sym in (initial_symbols or ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"]))
        self.quote_listeners: List[Callable[[Quote], None]] = []
        self.bar_listeners: List[Callable[[str, Bar], None]] = []
        self.trade_listeners: List[Callable[[Trade], None]] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._trade_seq = 0

        # Base price anchors for realistic simulation
        self.base_prices: Dict[str, float] = {
            "SPY": 585.50,
            "QQQ": 510.20,
            "AAPL": 235.80,
            "MSFT": 430.40,
            "NVDA": 142.60,
            "TSLA": 260.10,
        }
        self.current_prices: Dict[str, float] = dict(self.base_prices)
        self.historical_bars: Dict[str, List[Bar]] = {}

        # Pre-seed realistic historical 1-minute bars
        self._generate_seed_bars()

    def _generate_seed_bars(self, count: int = 120) -> None:
        """Pre-populate 2 hours of 1-minute bars for TradingView initialization."""
        now_ts = int(time.time())
        # Align to 60-second boundaries
        start_ts = now_ts - (count * 60)

        for sym in self.symbols:
            price = self.base_prices.get(sym, 150.0)
            bars: List[Bar] = []
            for i in range(count):
                bar_time = start_ts + (i * 60)
                # Small random fluctuation ±0.15%
                pct_change = random.gauss(0, 0.0015)
                open_p = price
                close_p = round(open_p * (1.0 + pct_change), 2)
                high_p = round(max(open_p, close_p) + abs(random.gauss(0, 0.0008) * open_p), 2)
                low_p = round(min(open_p, close_p) - abs(random.gauss(0, 0.0008) * open_p), 2)
                vol = random.randint(1000, 25000)

                bars.append(Bar(
                    time=bar_time,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol
                ))
                price = close_p

            self.historical_bars[sym] = bars
            self.current_prices[sym] = price

    def get_historical_bars(self, symbol: str) -> List[Bar]:
        return self.historical_bars.get(symbol.upper(), [])

    def get_historical_segment(
        self, symbol: str, timeframe: str, before: int
    ) -> Tuple[List[Bar], float, bool]:
        """Synthetic history for the simulation feed: a deterministic random
        walk strictly older than the cursor. Lets local --sim development
        exercise the chart's paging UI without any upstream session. A short
        page (has_more=False) after a few segments keeps the synthetic past
        finite, like a real venue's data start."""
        sym = symbol.upper()
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe.lower(), 900)
        base = self.base_prices.get(sym, 150.0)
        # Deterministic per (symbol, cursor): the same page refetched during a
        # session shows the same candles.
        rng = random.Random(f"{sym}:{before}")
        count = 30
        bars: List[Bar] = []
        price = base * (1.0 + rng.gauss(0, 0.002))
        for i in range(count):
            bar_time = before - (count - i) * tf_seconds
            close_p = round(price * (1.0 + rng.gauss(0, 0.0015)), 2)
            bars.append(Bar(
                time=bar_time,
                open=round(price, 2),
                high=round(max(price, close_p) * 1.0008, 2),
                low=round(min(price, close_p) * 0.9992, 2),
                close=close_p,
                volume=rng.randint(1000, 25000),
            ))
            price = close_p
        # Keep the synthetic past bounded: the third page back is the edge.
        has_more = before > int(time.time()) - 4 * 30 * tf_seconds * 30
        return bars, time.time(), has_more

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        sym = symbol.upper()
        price = self.current_prices.get(sym)
        if not price:
            return None
        spread = round(price * 0.0002, 2)  # ~2 bps spread
        return Quote(
            symbol=sym,
            bid=round(price - spread / 2.0, 2),
            ask=round(price + spread / 2.0, 2),
            last=round(price, 2),
            volume=random.randint(500000, 10000000),
            timestamp=utc_now()
        )

    def subscribe_quote(self, callback: Callable[[Quote], None]) -> None:
        self.quote_listeners.append(callback)

    def subscribe_bar(self, callback: Callable[[str, Bar], None]) -> None:
        self.bar_listeners.append(callback)

    def subscribe_trade(self, callback: Callable[[Trade], None]) -> None:
        self.trade_listeners.append(callback)

    async def start(self) -> None:
        """Starts asynchronous tick and bar generation loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._feed_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _feed_loop(self) -> None:
        """Simulates continuous millisecond-level tick updates and 1-minute bar closes."""
        current_minute = int(time.time() / 60)

        while self._running:
            try:
                # 1. Pick a random active symbol and generate tick
                sym = random.choice(list(self.symbols))
                old_price = self.current_prices[sym]
                # Tiny Brownian step
                drift = random.gauss(0, 0.0004)
                new_price = round(old_price * (1.0 + drift), 2)
                self.current_prices[sym] = new_price

                quote = self.get_latest_quote(sym)
                if quote:
                    for listener in self.quote_listeners:
                        try:
                            listener(quote)
                        except Exception:
                            pass

                    # Simulation also emits an executed trade so downstream
                    # trade-tape/chart logic exercises a real event channel.
                    self._trade_seq += 1
                    trade = Trade(
                        trade_id=f"sim-{self._trade_seq}",
                        order_id="",
                        symbol=sym,
                        side=OrderSide.BUY if drift >= 0 else OrderSide.SELL,
                        price=new_price,
                        quantity=random.randint(1, 300),
                        timestamp=utc_now(),
                    )
                    for t_listener in self.trade_listeners:
                        try:
                            t_listener(trade)
                        except Exception:
                            pass

                # 2. Check if a new minute bar has closed
                now_sec = int(time.time())
                min_ts = int(now_sec / 60) * 60
                if int(now_sec / 60) != current_minute:
                    current_minute = int(now_sec / 60)
                    for s in self.symbols:
                        bars = self.historical_bars.setdefault(s, [])
                        last_p = self.current_prices[s]
                        prev_close = bars[-1].close if bars else last_p
                        new_bar = Bar(
                            time=min_ts,
                            open=prev_close,
                            high=max(prev_close, last_p),
                            low=min(prev_close, last_p),
                            close=last_p,
                            volume=random.randint(2000, 15000)
                        )
                        bars.append(new_bar)
                        # Keep max 500 bars in memory
                        if len(bars) > 500:
                            bars.pop(0)

                        for b_listener in self.bar_listeners:
                            try:
                                b_listener(s, new_bar)
                            except Exception:
                                pass

                # Sleep 200ms between ticks for realistic fluid updates
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)
