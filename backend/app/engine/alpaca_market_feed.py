"""
Alpaca Real-time Market Data Feed Service.
Connects to Alpaca IEX WebSocket Stream and REST API for real 1-minute bars, quotes and trades.
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
import logging
from collections import OrderedDict
from typing import Dict, List, Callable, Optional, Set
from datetime import datetime, timezone
import httpx
import websockets

from app.models.trading import Quote, Bar, Trade, OrderSide, utc_now
from app.adapters.alpaca_adapter import AlpacaDataAdapter
from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.core.config import settings
from app.core.markets import within_day_range
from app.core.timeframes import TIMEFRAME_TTLS as CORE_TIMEFRAME_TTLS

logger = logging.getLogger(__name__)

# Upper bound on cached (symbol, timeframe) bar entries; evicts oldest first.
MAX_CACHE_ENTRIES = 300


class AlpacaMarketFeedService:
    """
    Dual-engine market data feed:
    - Alpaca Markets IEX WebSocket Stream for sub-second live ticks and trades.
    - Yahoo Finance Rapid Chart Adapter for high-depth multi-timeframe K-lines (1m/5m/15m/1h/4h/1d).
    - Multi-timeframe in-memory TTL caching with background preheating.
    """

    TIMEFRAME_TTLS: Dict[str, float] = dict(CORE_TIMEFRAME_TTLS)
    # Daily context (previous close, day range, volume) is refreshed at most
    # this often, with one batched call for the whole catalog.
    SNAPSHOT_TTL = 45.0

    def __init__(
        self,
        initial_symbols: Optional[List[str]] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        stream_url: Optional[str] = None,
        data_base_url: Optional[str] = None,
        feed: Optional[str] = None
    ):
        self.symbols: Set[str] = set(
            sym.upper() for sym in (initial_symbols or settings.default_symbols)
        )
        self.api_key = api_key or settings.alpaca_api_key or ""
        self.api_secret = api_secret or settings.alpaca_api_secret or ""
        self.stream_url = stream_url or settings.alpaca_data_stream_url
        self.data_base_url = (data_base_url or settings.alpaca_data_base_url).rstrip("/")
        self.feed = feed or settings.alpaca_feed

        self.quote_listeners: List[Callable[[Quote], None]] = []
        self.bar_listeners: List[Callable[[str, Bar], None]] = []
        self.trade_listeners: List[Callable[[Trade], None]] = []

        self.historical_bars: Dict[str, List[Bar]] = {}
        self.latest_quotes: Dict[str, Quote] = {}
        self.last_trade_prices: Dict[str, float] = {}
        # Per-symbol daily context from the batched snapshot endpoint:
        # {symbol: {"prev_close", "day_open", "day_high", "day_low", "volume", "last"}}
        self.daily_stats: Dict[str, Dict[str, float]] = {}
        self._snapshot_ts: float = 0.0
        self._snapshot_lock = threading.Lock()

        # Multi-timeframe RAM cache: {symbol: {timeframe: [Bar, ...]}}
        # OrderedDict enables oldest-first eviction when the cache grows unbounded.
        self.multi_tf_bars: Dict[str, Dict[str, List[Bar]]] = {}
        self._bars_cache_ts: Dict[str, Dict[str, float]] = {}
        self._cache_order: "OrderedDict[str, None]" = OrderedDict()

        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._prewarm_task: Optional[asyncio.Task] = None

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

        # Initialize snapshot data
        self._preload_historical_bars_sync()

    def _preload_historical_bars_sync(self) -> None:
        """Fetch latest historical 1-minute bars and quotes via REST on initialization."""
        try:
            with httpx.Client(headers=self.headers, timeout=15.0) as client:
                for sym in self.symbols:
                    # 1. Fetch individual symbol bars
                    try:
                        bars_resp = client.get(
                            f"{self.data_base_url}/stocks/{sym}/bars",
                            params={
                                "timeframe": "1Min",
                                "limit": 120,
                                "feed": self.feed
                            }
                        )
                        if bars_resp.status_code == 200:
                            parsed = AlpacaDataAdapter.parse_rest_bars_response(bars_resp.json(), symbol=sym)
                            if parsed:
                                self.historical_bars[sym.upper()] = parsed
                            # NOTE: do not seed last_trade_prices from a bar
                            # close — that window can reach back into an older
                            # session and masquerade as a live trade. The first
                            # real trade, or the snapshot's latestTrade via
                            # _decorate_daily, is the authoritative price.
                    except Exception as err:
                        logger.warning("Alpaca feed bar error for %s: %s", sym, err)

                    # 2. Fetch latest quote
                    try:
                        q_resp = client.get(
                            f"{self.data_base_url}/stocks/{sym}/quotes/latest",
                            params={"feed": self.feed}
                        )
                        if q_resp.status_code == 200:
                            q = AlpacaDataAdapter.parse_rest_quote_response(sym, q_resp.json())
                            if q:
                                self.latest_quotes[sym.upper()] = q
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("AlpacaMarketFeedService: Failed to preload historical bars: %s", e)

    def _cache_put(self, sym: str, tf: str, bars: List[Bar], ts: Optional[float] = None) -> None:
        """Store bars in the bounded multi-timeframe cache (oldest entry evicted)."""
        key = f"{sym}:{tf}"
        self.multi_tf_bars.setdefault(sym, {})[tf] = bars
        self._bars_cache_ts.setdefault(sym, {})[tf] = time.time() if ts is None else ts
        self._cache_order[key] = None
        self._cache_order.move_to_end(key)
        while len(self._cache_order) > MAX_CACHE_ENTRIES:
            old_key, _ = self._cache_order.popitem(last=False)
            old_sym, old_tf = old_key.split(":", 1)
            self.multi_tf_bars.get(old_sym, {}).pop(old_tf, None)
            self._bars_cache_ts.get(old_sym, {}).pop(old_tf, None)
            if not self.multi_tf_bars.get(old_sym):
                self.multi_tf_bars.pop(old_sym, None)
                self._bars_cache_ts.pop(old_sym, None)

    def get_bars(self, symbol: str, timeframe: str = "15m") -> List[Bar]:
        """
        Retrieves historical K-line bars for a given symbol and timeframe.
        Leverages RAM TTL cache to achieve sub-millisecond response time.
        Falls back to Yahoo Finance Rapid Adapter for deep multi-timeframe candles,
        and to Alpaca historical bars.
        """
        sym = symbol.upper()
        tf = timeframe.lower()
        now = time.time()

        # Check RAM cache first
        cached = self.multi_tf_bars.get(sym, {}).get(tf)
        cache_ts = self._bars_cache_ts.get(sym, {}).get(tf, 0.0)
        ttl = self.TIMEFRAME_TTLS.get(tf, 60.0)

        if cached and (now - cache_ts < ttl):
            return cached

        # Fetch fresh bars from Yahoo Rapid Adapter
        try:
            bars = YahooFinanceAdapter.fetch_bars(sym, timeframe=tf)
            if bars:
                self._cache_put(sym, tf, bars, now)
                # NOTE: never write last_trade_prices here — a historical close must
                # not overwrite a fresher live trade price.
                return bars
        except Exception as err:
            logger.warning("Yahoo Finance fetch failed for %s (%s): %s", sym, tf, err)

        # 1. Fallback to cached bars of requested timeframe even if stale
        if cached:
            return cached

        # 2. If 1m requested, fallback to local 1m bars if available
        if tf == "1m" and sym in self.historical_bars and self.historical_bars[sym]:
            return self.historical_bars[sym]

        return []

    def get_historical_bars(self, symbol: str) -> List[Bar]:
        return self.get_bars(symbol, timeframe="1m")

    def _decorate_daily(self, quote: Optional[Quote]) -> Optional[Quote]:
        """Attach the cached daily context (prev close, session high/low).

        Alpaca's quote payloads carry no day range, so a lone bid-only print can
        look like a valid price to every consumer downstream. Stamping the
        range the snapshots already know lets those consumers reject a
        self-contradicting quote instead of each re-deriving the truth.

        A quote whose ``last`` contradicts that range is healed from the
        snapshot's latest trade, the same value the tickers endpoint shows.
        Otherwise a poisoned mark (e.g. seeded from an older session's bar)
        survives every revaluation, because the guard keeps rejecting the only
        quote on record instead of replacing it.
        """
        if quote is None:
            return None
        stats = self.daily_stats.get(quote.symbol.upper())
        if not stats:
            return quote

        day_low = quote.day_low or stats.get("day_low")
        day_high = quote.day_high or stats.get("day_high")
        updates = {
            "prev_close": quote.prev_close or stats.get("prev_close"),
            "day_high": day_high,
            "day_low": day_low,
        }

        trusted_last = stats.get("last")
        if (
            trusted_last
            and not within_day_range(quote.last, day_low, day_high)
            and within_day_range(trusted_last, day_low, day_high)
        ):
            updates["last"] = trusted_last

        return quote.model_copy(update=updates)

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        sym = symbol.upper()
        if sym in self.latest_quotes:
            return self._decorate_daily(self.latest_quotes[sym])
        try:
            with httpx.Client(headers=self.headers, timeout=5.0) as client:
                resp = client.get(
                    f"{self.data_base_url}/stocks/{sym}/quotes/latest",
                    params={"feed": self.feed}
                )
                if resp.status_code == 200:
                    q = AlpacaDataAdapter.parse_rest_quote_response(sym, resp.json())
                    if q:
                        self.latest_quotes[sym] = q
                        return self._decorate_daily(q)
        except Exception:
            pass
        return self.latest_quotes.get(sym)

    def refresh_daily_stats(self, symbols: Optional[List[str]] = None, force: bool = False) -> None:
        """Refresh per-symbol daily context in one batched snapshot request.

        A watchlist row needs the previous close (for the % change) plus today's
        range. Fetching those per symbol costs seconds each; the snapshot
        endpoint answers for the whole catalog in a single call.
        """
        with self._snapshot_lock:
            now = time.time()
            if not force and self._snapshot_ts and now - self._snapshot_ts < self.SNAPSHOT_TTL:
                return
            self._snapshot_ts = now

        syms = sorted(symbols or self.symbols)
        if not syms:
            return
        try:
            with httpx.Client(headers=self.headers, timeout=10.0) as client:
                resp = client.get(
                    f"{self.data_base_url}/stocks/snapshots",
                    params={"symbols": ",".join(syms), "feed": self.feed},
                )
                if resp.status_code != 200:
                    return
                stats = AlpacaDataAdapter.parse_snapshots_response(resp.json())
                if stats:
                    self.daily_stats.update(stats)
        except Exception as err:
            logger.warning("Alpaca snapshot refresh failed: %s", err)

    def ensure_daily_stats(self, symbols: Optional[List[str]] = None) -> None:
        """Warm the daily context once per TTL (one batched call at most)."""
        self.refresh_daily_stats(symbols)

    def get_daily_stats(self, symbol: str) -> Dict[str, float]:
        """Cached daily context for one symbol. Never touches the network:
        ``ensure_daily_stats`` warms the whole catalog in one call."""
        return self.daily_stats.get(symbol.upper()) or {}

    def subscribe_quote(self, callback: Callable[[Quote], None]) -> None:
        self.quote_listeners.append(callback)

    def subscribe_bar(self, callback: Callable[[str, Bar], None]) -> None:
        self.bar_listeners.append(callback)

    def subscribe_trade(self, callback: Callable[[Trade], None]) -> None:
        self.trade_listeners.append(callback)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._websocket_listener_loop())
        self._sync_task = asyncio.create_task(self._periodic_quote_sync_loop())
        self._prewarm_task = asyncio.create_task(self._prewarm_cache_loop())

    async def stop(self) -> None:
        self._running = False
        for task in (self._ws_task, self._sync_task, self._prewarm_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        YahooFinanceAdapter.close_client()

    async def _websocket_listener_loop(self) -> None:
        """Connects and reconnects to Alpaca IEX stream with exponential backoff."""
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(self.stream_url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Connected to Alpaca IEX Streamer: %s", self.stream_url)
                    backoff = 1.0

                    # 1. Wait for connection confirmation
                    init_msg = await ws.recv()
                    data = json.loads(init_msg)

                    # 2. Authenticate
                    auth_msg = {
                        "action": "auth",
                        "key": self.api_key,
                        "secret": self.api_secret
                    }
                    await ws.send(json.dumps(auth_msg))
                    auth_resp = await ws.recv()
                    auth_data = json.loads(auth_resp)

                    # 3. Subscribe to quotes and bars
                    sub_msg = {
                        "action": "subscribe",
                        "quotes": list(self.symbols),
                        "bars": list(self.symbols),
                        "trades": list(self.symbols)
                    }
                    await ws.send(json.dumps(sub_msg))
                    sub_resp = await ws.recv()

                    # 4. Message processing loop
                    while self._running:
                        msg_str = await ws.recv()
                        items = json.loads(msg_str)
                        if not isinstance(items, list):
                            items = [items]

                        for item in items:
                            t_type = item.get("T")
                            sym = item.get("S", "").upper()
                            if not sym:
                                continue

                            # Trade tick: update last price and forward the real trade
                            if t_type == "t":
                                trade_p = float(item.get("p", 0.0))
                                if trade_p > 0:
                                    self.last_trade_prices[sym] = trade_p
                                trade = AlpacaDataAdapter.parse_stream_trade(item)
                                if trade:
                                    for t_listener in self.trade_listeners:
                                        try:
                                            t_listener(trade)
                                        except Exception:
                                            pass

                            # Quote tick
                            elif t_type == "q":
                                last_p = self.last_trade_prices.get(sym)
                                quote = AlpacaDataAdapter.parse_stream_quote(item, last_price=last_p)
                                if quote:
                                    quote = self._decorate_daily(quote) or quote
                                    self.latest_quotes[sym] = quote
                                    for listener in self.quote_listeners:
                                        try:
                                            listener(quote)
                                        except Exception:
                                            pass

                            # Bar tick
                            elif t_type == "b":
                                bar = AlpacaDataAdapter.parse_stream_bar(item)
                                if bar:
                                    bars = self.historical_bars.setdefault(sym, [])
                                    # Replace bar if timestamp identical, else append
                                    if bars and bars[-1].time == bar.time:
                                        bars[-1] = bar
                                    else:
                                        bars.append(bar)
                                    if len(bars) > 500:
                                        bars.pop(0)

                                    for b_listener in self.bar_listeners:
                                        try:
                                            b_listener(sym, bar)
                                        except Exception:
                                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Alpaca Streamer disconnected (%s), reconnecting in %.1fs...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    async def _periodic_quote_sync_loop(self) -> None:
        """
        Polls latest quote every 15 seconds during off-hours or quiet periods
        to keep frontend alive and heartbeating.
        """
        while self._running:
            try:
                await asyncio.sleep(15)
                if not self._running:
                    break

                # Keep previous closes and day ranges warm for the watchlist.
                try:
                    await asyncio.to_thread(self.refresh_daily_stats)
                except Exception:
                    pass

                for sym in self.symbols:
                    q = self._decorate_daily(self.latest_quotes.get(sym))
                    if q:
                        for listener in self.quote_listeners:
                            try:
                                listener(q)
                            except Exception:
                                pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _prewarm_cache_loop(self) -> None:
        """
        Background task:
        Smoothly pre-warms key candles (15m, 1h, 1d) for core symbols (SPY, QQQ, AAPL, NVDA, TSLA) in RAM.
        Runs gently with rate-limit protection so user requests are served in < 1ms.
        """
        await asyncio.sleep(2.0)
        core_symbols = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
        core_timeframes = ["5m", "15m", "1h", "4h", "1d"]

        while self._running:
            try:
                for sym in core_symbols:
                    if not self._running:
                        break
                    for tf in core_timeframes:
                        if not self._running:
                            break
                        now = time.time()
                        cached_ts = self._bars_cache_ts.get(sym, {}).get(tf, 0.0)
                        ttl = self.TIMEFRAME_TTLS.get(tf, 60.0)

                        if now - cached_ts > (ttl * 0.7):
                            try:
                                bars = await asyncio.to_thread(YahooFinanceAdapter.fetch_bars, sym, tf)
                                if bars:
                                    self._cache_put(sym, tf, bars)
                            except Exception as e:
                                logger.debug("Prewarm failed for %s (%s): %s", sym, tf, e)
                            await asyncio.sleep(0.5)

                await asyncio.sleep(45.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error in prewarm cache loop: %s", e)
                await asyncio.sleep(10.0)

