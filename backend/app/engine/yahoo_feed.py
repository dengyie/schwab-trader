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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.core.markets import (
    SymbolRef,
    freshness_updates,
    is_market_open,
    market_of,
    resolve_symbol,
)
from app.core.timeframes import (
    HIST_PAGE_BARS,
    HIST_PAGE_SPAN_SECONDS,
    HIST_SEGMENT_BARS,
    TIMEFRAME_SPAN_DAYS,
    TIMEFRAME_TTLS,
)
from app.engine.bar_cache import BarCache
from app.models.trading import Bar, Quote, utc_now

logger = logging.getLogger(__name__)


class YahooQuoteFeed:
    """Polling feed for HK / JP / CN (and any symbol outside the US stream)."""

    POLL_SECONDS = 6.0
    IDLE_POLL_SECONDS = 30.0
    CACHE_SECONDS = 4.0
    # Fallback TTL for a timeframe the shared table does not know. The US feed
    # and this one read the same ``TIMEFRAME_TTLS``; a second, different number
    # here is how the two venues drifted apart before.
    BAR_CACHE_SECONDS = 20.0
    # RAM ceiling in (symbol, timeframe) keys. Sized for ~20 watched symbols
    # across all eight timeframes; older keys fall back to their disk copy, so
    # an eviction costs one JSON parse, never an upstream call.
    MAX_BAR_ENTRIES = 160
    # A REST read keeps a symbol warm for this long, then the poller drops it.
    ON_DEMAND_TTL = 180.0

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        enabled: bool = True,
        cache_dir: Optional[str] = None,
    ):
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
            # Bars use the same cache as the US path: persisted, stale-served,
        # single-flight. A HK chart then survives a deploy and reports a real
        # ``fetched_at`` so the API can emit ETag/Cache-Control for it too.
        self._bar_cache = BarCache(
            cache_dir,
            max_entries=self.MAX_BAR_ENTRIES,
            ttl_for=lambda tf: TIMEFRAME_TTLS.get(tf, self.BAR_CACHE_SECONDS),
        )
        self._refresh_pool: Optional[ThreadPoolExecutor] = None
        self._refresh_pool_lock = threading.Lock()

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

        # Yahoo publishes a last price only, never a book. A bid/ask built
        # around it would be an estimate presented as a quote, so both stay 0
        # and ``book_available`` (default False) tells the UI to say "no book"
        # rather than draw a ladder nobody quoted.
        bid = 0.0
        ask = 0.0

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
            **freshness_updates(stamp, market),
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
    def _refresh_pool_executor(self) -> ThreadPoolExecutor:
        with self._refresh_pool_lock:
            if self._refresh_pool is None:
                self._refresh_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="yahoo-bars"
                )
            return self._refresh_pool

    def _fetch_bars(self, symbol: str, timeframe: str) -> Optional[List[Bar]]:
        """Fetch one key and store it. The caller owns the refresh slot."""
        try:
            ref = resolve_symbol(symbol)
            if ref is None:
                return None
            tz = ZoneInfo(ref.market.timezone)
            bars = YahooFinanceAdapter.fetch_bars(ref.symbol, timeframe=timeframe, tz=tz)
            if bars:
                self._bar_cache.put(ref.symbol, timeframe, bars)
                return bars
            return None
        except Exception as exc:
            logger.warning("Yahoo bars failed for %s (%s): %s", symbol, timeframe, exc)
            self._bar_cache.cool_down(symbol, timeframe)
            return None
        finally:
            self._bar_cache.end_refresh(symbol, timeframe)

    def _fetch_cold_bars(self, symbol: str, timeframe: str) -> Optional[List[Bar]]:
        """Lead a cold fetch, or wait for whoever is already leading it."""
        if not self._bar_cache.begin_refresh(symbol, timeframe):
            self._bar_cache.wait_for_refresh(symbol, timeframe)
            entry = self._bar_cache.get(symbol, timeframe)
            return entry.bars if entry else None
        return self._fetch_bars(symbol, timeframe)

    def _schedule_refresh(self, symbol: str, timeframe: str) -> None:
        # A disabled feed still serves what it has on disk, but it must never
        # reach upstream: "disabled" is an operator switch, not a hint.
        if not self._enabled:
            return
        if self._bar_cache.is_cooling(symbol, timeframe):
            return
        if not self._bar_cache.begin_refresh(symbol, timeframe):
            return
        try:
            self._refresh_pool_executor().submit(self._fetch_bars, symbol, timeframe)
        except RuntimeError:
            self._bar_cache.end_refresh(symbol, timeframe)

    def get_bars_with_meta(
        self, symbol: str, timeframe: str = "15m"
    ) -> Tuple[List[Bar], float]:
        """Bars plus their fetch time, mirroring the US feed's contract."""
        ref = resolve_symbol(symbol)
        if ref is None:
            return [], 0.0
        self._touch(ref.symbol)
        tf = timeframe.lower()
        entry = self._bar_cache.get(ref.symbol, tf)

        if entry:
            if not self._bar_cache.is_fresh(entry, tf):
                self._schedule_refresh(ref.symbol, tf)
            return entry.bars, entry.fetched_at

        if self._enabled and not self._bar_cache.is_cooling(ref.symbol, tf):
            bars = self._fetch_cold_bars(ref.symbol, tf)
            if bars:
                return bars, self._bar_cache.fetched_at_of(ref.symbol, tf)

        return [], 0.0

    def get_bars(self, symbol: str, timeframe: str = "15m") -> List[Bar]:
        bars, _ = self.get_bars_with_meta(symbol, timeframe)
        return bars

    def get_historical_segment(
        self, symbol: str, timeframe: str, before: int
    ) -> Tuple[List[Bar], float, bool]:
        """One immutable page of history strictly older than ``before``.

        Returns (bars, fetched_at, has_more). Served from the segment cache
        when present; otherwise a single-flight upstream fetch via
        ``period1``/``period2``. Failures return ([], 0.0, False) after
        arming the segment's cooldown — paging a dead edge must not storm.
        """
        ref = resolve_symbol(symbol)
        if ref is None:
            return [], 0.0, False
        tf = timeframe.lower()
        page_size = HIST_PAGE_BARS.get(tf, HIST_SEGMENT_BARS)
        seg_tf = self._bar_cache.hist_timeframe(tf, before)
        entry = self._bar_cache.get(ref.symbol, seg_tf)
        if entry:
            if not self._bar_cache.is_fresh(entry, seg_tf):
                self._schedule_hist_refresh(ref.symbol, tf, seg_tf, before)
            return entry.bars, entry.fetched_at, self._has_more(entry.bars, page_size)

        if not self._enabled or self._bar_cache.is_cooling(ref.symbol, seg_tf):
            return [], 0.0, False
        if not self._bar_cache.begin_refresh(ref.symbol, seg_tf):
            self._bar_cache.wait_for_refresh(ref.symbol, seg_tf)
            entry = self._bar_cache.get(ref.symbol, seg_tf)
            if entry:
                return entry.bars, entry.fetched_at, self._has_more(entry.bars, page_size)
            return [], 0.0, False
        try:
            bars = self._fetch_hist_segment(ref, tf, seg_tf, before)
        finally:
            self._bar_cache.end_refresh(ref.symbol, seg_tf)
        if bars is None:
            return [], 0.0, False
        fetched = self._bar_cache.fetched_at_of(ref.symbol, seg_tf)
        return bars, fetched, self._has_more(bars, page_size)

    @staticmethod
    def _has_more(bars: List[Bar], page_size: int) -> bool:
        # A full page implies history continues beyond it; a short page means
        # the physical limit of this timeframe was reached (or the market is
        # younger than the window). Empty = terminal edge.
        return len(bars) >= page_size

    def _fetch_hist_segment(
        self, ref: SymbolRef, tf: str, seg_tf: str, before: int
    ) -> Optional[List[Bar]]:
        try:
            tz = ZoneInfo(ref.market.timezone)
            page_size = HIST_PAGE_BARS.get(tf, HIST_SEGMENT_BARS)
            span_days = TIMEFRAME_SPAN_DAYS.get(tf)
            oldest_allowed = int(time.time()) - span_days * 86400 if span_days else 0
            period1 = max(0, before - HIST_PAGE_SPAN_SECONDS)
            if oldest_allowed and period1 < oldest_allowed:
                period1 = oldest_allowed
            bars = YahooFinanceAdapter.fetch_bars(
                ref.symbol, timeframe=tf, tz=tz,
                period1=period1, period2=before,
            )
            # Segment contract: strictly older than the cursor, newest N kept.
            bars = [b for b in bars if b.time < before]
            if len(bars) > page_size:
                bars = bars[-page_size:]
            if not bars:
                # Nothing in this window: the true edge. Store the emptiness
                # (with a fresh timestamp so the segment is served without
                # re-hitting upstream) — a terminal state, not a failure.
                self._bar_cache.put(ref.symbol, seg_tf, [], time.time())
                return []
            self._bar_cache.put(ref.symbol, seg_tf, bars)
            return bars
        except Exception as exc:
            logger.warning("Yahoo hist segment failed for %s %s<%s: %s", ref.symbol, tf, before, exc)
            self._bar_cache.cool_down(ref.symbol, seg_tf)
            return None

    def _schedule_hist_refresh(self, symbol: str, tf: str, seg_tf: str, before: int) -> None:
        # Disabled feeds serve what is on disk but never reach upstream.
        if not self._enabled or self._bar_cache.is_cooling(symbol, seg_tf):
            return
        if not self._bar_cache.begin_refresh(symbol, seg_tf):
            return
        ref = resolve_symbol(symbol)
        if ref is None:
            self._bar_cache.end_refresh(symbol, seg_tf)
            return
        try:
            self._refresh_pool_executor().submit(
                self._fetch_hist_segment_and_close, ref, tf, seg_tf, before
            )
        except RuntimeError:
            self._bar_cache.end_refresh(symbol, seg_tf)

    def _fetch_hist_segment_and_close(self, ref: SymbolRef, tf: str, seg_tf: str, before: int) -> None:
        try:
            self._fetch_hist_segment(ref, tf, seg_tf, before)
        finally:
            self._bar_cache.end_refresh(ref.symbol, seg_tf)

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
        with self._refresh_pool_lock:
            pool, self._refresh_pool = self._refresh_pool, None
        if pool is not None:
            # An in-flight Yahoo call must not hold up a shutdown.
            pool.shutdown(wait=False, cancel_futures=True)

    def health(self) -> Dict[str, object]:
        """Polling state, for the health probe and for ops questions.

        There is no socket to report: this feed is a poller, so the useful
        facts are whether it is running and how many symbols it is watching.
        ``rate_limit`` carries Yahoo's IP throttling signal (real 429s and the
        "possibly delisted" disguise): on a shared-VPS exit IP this is the one
        upstream failure mode that silences the whole feed at once, so it is
        counted rather than left as a log line. ``http_errors`` carries the
        same story for non-429 HTTP statuses — notably the day Yahoo tightens
        a measured span contract (e.g. the 59d 5m/15m ceiling) and every
        fresh fetch for those timeframes starts failing at once.
        """
        with self._lock:
            watching = len(set(self._active) | set(self._on_demand))
        return {
            "provider": "yahoo",
            "enabled": bool(self._enabled),
            "running": bool(self._running),
            "watching": watching,
            "rate_limit": YahooFinanceAdapter.rate_limit_stats(),
            "http_errors": YahooFinanceAdapter.http_error_stats(),
        }

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
