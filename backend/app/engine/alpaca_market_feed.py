"""
Alpaca Real-time Market Data Feed Service.
Connects to Alpaca IEX WebSocket Stream and REST API for real 1-minute bars, quotes and trades.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Callable, Optional, Set, Tuple
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import httpx
import websockets

from app.models.trading import Quote, Bar, Trade, OrderSide, utc_now
from app.adapters.alpaca_adapter import AlpacaDataAdapter
from app.adapters import defeatbeta_adapter
from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.core.config import settings
from app.core.markets import (
    MARKETS,
    freshness_updates,
    is_market_open,
    market_of,
    resolve_symbol,
    within_day_range,
)
from app.core.timeframes import (
    HIST_PAGE_BARS,
    HIST_PAGE_SPAN_SECONDS,
    HIST_SEGMENT_BARS,
    TIMEFRAME_SPAN_DAYS,
    TIMEFRAME_TTLS,
    WHOLE_WINDOW_SPAN_SECONDS,
)
from app.engine.bar_cache import BarCache
from app.engine.state_store import JsonStateStore

logger = logging.getLogger(__name__)

# Upper bound on cached (symbol, timeframe) bar entries; evicts oldest first.
MAX_CACHE_ENTRIES = 300
# Maximum fan-out rate for streamed quotes (matches the websocket manager's
# 40ms conflation). IEX prints far faster than any screen can redraw.
QUOTE_EMIT_SECONDS = 0.04
# Soft cap on runtime-added stream subscriptions (WS SUBSCRIBE for symbols
# outside the boot set). IEX has no hard per-connection limit, but an
# unbounded set would let a looping client inflate the tick fan-out forever.
MAX_DYNAMIC_SYMBOLS = 50

# Reconnect policy. A connection only earns a fresh backoff once it has
# actually stayed up; see ``next_reconnect_backoff`` for why.
INITIAL_RECONNECT_BACKOFF = 1.0
MAX_RECONNECT_BACKOFF = 30.0
STABLE_CONNECTION_SECONDS = 30.0

# A quota rejection is not a network problem: another client holds this key's
# single IEX session, so nothing we do locally makes the next attempt succeed.
# Retrying it at network cadence only spams the log and the endpoint (observed:
# 135 identical lines in an hour), so it backs off harder and logs sparsely.
QUOTA_INITIAL_BACKOFF = 5.0
QUOTA_MAX_BACKOFF = 60.0
QUOTA_LOG_EVERY = 10
# Alpaca's wording for "you already have as many sessions as your plan allows".
QUOTA_ERROR_MARKERS = ("connection limit exceeded", "code=406")


def next_reconnect_backoff(
    backoff: float,
    connected_seconds: float,
    *,
    stable_after: float = STABLE_CONNECTION_SECONDS,
    factor: float = 1.5,
    cap: float = MAX_RECONNECT_BACKOFF,
) -> float:
    """Backoff for the next reconnect attempt.

    Resetting to one second on every successful ``connect`` is not safe: the
    stream can accept the socket and then drop it seconds later (Alpaca evicts
    duplicate sessions for one key, and a second backend sharing production
    credentials produces exactly that endless ping-pong). Under that pattern a
    connect-based reset reconnects every second forever, hammering the endpoint
    and masking the cause. Only a connection that stayed up long enough earns a
    clean slate; short-lived ones keep escalating.
    """
    if connected_seconds >= stable_after:
        return INITIAL_RECONNECT_BACKOFF
    return min(backoff * factor, cap)


def is_quota_rejection(message: str) -> bool:
    """True when the auth failure is "this key already has its session".

    Distinguishing it matters: a wrong key or a flaky network is fixed on our
    side, while "connection limit exceeded" can only be fixed by whoever else
    is holding the session. Treating both the same produces a log that reads
    like a network fault and hides the real owner.
    """
    text = (message or "").lower()
    return any(marker in text for marker in QUOTA_ERROR_MARKERS)


# Timeframes whose upstream volume needs reconciliation against the daily
# tape. 1d/1wk/1mo are excluded because they ARE the authority.
INTRADAY_TIMEFRAMES = frozenset({"1m", "5m", "15m", "1h", "4h"})
# A day whose intraday sum exceeds the authoritative total by more than this
# factor is data corruption, not tape drift: it gets rescaled. The margin
# keeps genuinely busy days (a late-day surge summing to 1.3x the running
# total mid-session) from being rewritten on a guess.
INTRADAY_VOLUME_OVERSIZED = 2.0

# US intraday timeframes whose history continues past Yahoo's coverage
# ceiling via the Alpaca IEX archive (free feed, measured 2026-11-13: 15m/5m
# bars back to 2020-07-27 — five years; 1m stays on Yahoo's 7d window, which
# already covers its whole upstream). The archive is IEX-only: OHLC is real
# traded price, per-bar volume is ~2% of the consolidated tape — honest
# under-reporting the reconciliation never rewrites.
ALPACA_DEEP_HIST_TIMEFRAMES = frozenset({"5m", "15m"})

# The measured physical floor of the IEX archive (2026-11-14: the QQQ 15m
# page chain ends at 2016-01-01; every symbol listed before then shares it).
# A cursor above this epoch always has more archive below it — the anchor for
# ``_has_more`` where a page-full can never happen (first page is clamped to
# Yahoo's 59d span).
DEEP_HIST_FLOOR = 1451606400  # 2016-01-01 00:00 UTC


def reconcile_intraday_volume(
    bars: List[Bar],
    volume_by_day: Dict[str, float],
    tz_name: str,
    *,
    oversize_factor: float = INTRADAY_VOLUME_OVERSIZED,
) -> List[Bar]:
    """Scale down intraday days whose summed volume contradicts the daily tape.

    Yahoo's intraday endpoints (15m/1h) inflate volume for some symbols —
    measured 2026-11-12: QQQ's 15m bars summed to 374M shares against a real
    session total of 21M (~18x) while SPY read a plausible 1.3x, and a single
    11-05 15m bar carried 95M shares where the same 1h slot showed 11.7M.
    The 4h view inherits the error because it is aggregated from 1h bars.
    The 1d endpoint and ``daily_stats`` (regularMarketVolume) agree with each
    other, so per venue-local day they are the authority. The intl feed
    (HK/JP/CN) is not covered here, deliberately: an upstream audit found
    zero inflated days across 0700.HK/7203.T/600519.SS (410 trading days,
    sums under-reporting rather than inflating), so there is nothing to
    reconcile for the YahooQuoteFeed path yet.

    Each day is checked independently: when its bars sum to more than
    ``oversize_factor`` × the authoritative total, that day's bars are scaled
    proportionally (factor = daily / summed), which preserves the intraday
    shape — only the unit is wrong upstream. Days at or under the threshold
    are left untouched (a mildly elevated sum may be real, and an
    under-reporting day cannot be reconstructed from its own bars). Days with
    no authoritative figure are passed through as-is.
    """
    if not bars or not volume_by_day:
        return bars
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return bars

    day_of: Dict[int, str] = {}
    day_sum: Dict[str, float] = {}
    for i, b in enumerate(bars):
        if b.volume <= 0:
            continue
        day = datetime.fromtimestamp(b.time, tz=tz).strftime("%Y-%m-%d")
        day_of[i] = day
        day_sum[day] = day_sum.get(day, 0.0) + b.volume

    factors: Dict[str, float] = {}
    for day, total in day_sum.items():
        authority = volume_by_day.get(day)
        if authority and total > authority * oversize_factor:
            factors[day] = authority / total
    if not factors:
        return bars

    out: List[Bar] = []
    for i, b in enumerate(bars):
        day = day_of.get(i)
        if day is not None and day in factors:
            b = b.model_copy(update={"volume": round(b.volume * factors[day])})
        out.append(b)
    return out


class AlpacaMarketFeedService:
    """
    Dual-engine market data feed:
    - Alpaca Markets IEX WebSocket Stream for sub-second live ticks and trades.
    - Yahoo Finance Rapid Chart Adapter for high-depth multi-timeframe K-lines (1m/5m/15m/1h/4h/1d).
    - Multi-timeframe in-memory TTL caching with background preheating.
    """

    # Default for services built with ``__new__`` (tests that exercise one
    # method): no snapshot on disk unless ``__init__`` wired one up.
    _stats_store: Optional[JsonStateStore] = None
    # Daily context (previous close, day range, volume) is refreshed at most
    # this often. During a session the range moves; after the close it is
    # static, so a far longer TTL keeps the same consolidated close for the
    # rest of the day/weekend without hammering upstream.
    #
    # Each refresh costs one Yahoo chart request per symbol, and the payload
    # is parsed on the single uvicorn worker's event loop. A 45s TTL meant
    # 14 requests every 45s even with nobody connected; that parse load was
    # enough to starve the loop and push /api/health past 20 seconds. Three
    # minutes keeps the consolidated close honest while leaving the worker
    # idle between user interactions.
    SNAPSHOT_TTL = 180.0
    SNAPSHOT_TTL_CLOSED = 3600.0
    # With no client connected the snapshot sweep still runs, just rarely: a
    # quote payload nobody is watching is not worth one Yahoo request per
    # symbol every three minutes.
    IDLE_SWEEP_SECONDS = 300.0
    # Chart prewarming exists only to make the first chart load instant. With
    # no client connected there is nothing to prewarm for, so the loop sleeps
    # instead of re-fetching multi-thousand-bar candle payloads forever.
    IDLE_PREWARM_SECONDS = 30.0
    # Deep-history pre-run paces one segment page every DEEP_HIST_PREWARM_PAUSE
    # seconds (measured 2026-11-14: a 15m IEX page takes ~15-20s upstream, and
    # Alpaca's free tier allows 200 req/min — the pause keeps the runner far
    # under it while it walks a symbol's full archive, ~100 pages / ~30min for
    # 15m back to 2019). Unlike the fresh-window prewarm this walk IS gated
    # (2026-11-15) on chart subscribers plus a per-pass page budget: see the
    # constants block below for the measured failure that forced both.
    # Kept for the sliding-edge jump inside the walk (a ceiling hop is one
    # page like any other now, paced by DEEP_HIST_PAGE_PACE); the walk's own
    # page cadence moved to DEEP_HIST_PAGE_PACE below.
    DEEP_HIST_PREWARM_PAUSE = 1.0
    # Backoff between "temporary nothing" answers while walking (cooldown
    # windows last 30s; five misses over ~2min then give the symbol up).
    DEEP_HIST_RETRY_PAUSE = 25.0
    # Post-boot delay before the walk begins, and the pace once running. The
    # 2026-11-14 outage: two restarts in 3 minutes (deploy + ops verify) put
    # the walk's fetch+write loop on top of the boot preload while swap was
    # already warm — load 10.6 on 2 cores, agent dead, box wedged for 40+
    # minutes. The walk serves no live user: 15 minutes of grace lets the
    # preload settle and the fresh windows land, and a 4s/page pace caps the
    # write rate well under what a standard PD + swap can absorb.
    #
    # 2026-11-15 re-measure killed the "steady state is nearly free" claim:
    # the archive CANNOT be completed. TSLA 5m alone needs ~570 pages to the
    # 2016 floor (2000 5m bars ≈ 7 days per page over 10.7 years), the full
    # catalog needs ~9,000 segments, and the disk budget caps at 4,000 files
    # — so every hourly pass disk-hits its way down the chain and then keeps
    # *fetching* fresh frontier pages from Alpaca (measured: ~3,700 upstream
    # calls/hour, ~30 min of JSON parse at 40-86% of the e2-micro's one
    # core, with the market closed and nobody watching). Two caps fix it:
    #
    # * DEEP_HIST_PASS_PAGE_BUDGET bounds how many upstream pages a pass may
    #   fetch (the disk-hit part of the walk is ~10ms/page and unlimited);
    #   the pass spends its budget then yields — a bounded background chore
    #   instead of a 30-minute CPU marathon.
    # * The idle gate (below) stops passes entirely while no chart client is
    #   connected: pre-warming a decade of archives for an empty room is the
    #   exact "pegged worker with the tab closed" failure the fresh-window
    #   prewarm already learned to avoid.
    DEEP_HIST_BOOT_GRACE = 900.0
    DEEP_HIST_PAGE_PACE = 4.0
    DEEP_HIST_PASS_PAGE_BUDGET = 50
    # Idle-server poll while the gate waits (short: a client connecting at
    # boot-grace end should get its first pass within minutes, not an hour).
    DEEP_HIST_IDLE_POLL = 300.0
    # Re-walk cadence: history is immutable and the disk cache holds 30d, so
    # an hourly pass re-verifies pages that cannot change. Six hours still
    # re-primes long-TTL expiries well ahead of any client need.
    DEEP_HIST_REWALK_SECONDS = 6 * 3600.0
    DEEP_HIST_PREWARM_TFS = ("15m", "5m")

    def __init__(
        self,
        initial_symbols: Optional[List[str]] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        stream_url: Optional[str] = None,
        data_base_url: Optional[str] = None,
        feed: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ):
        self.symbols: Set[str] = set(
            sym.upper() for sym in (initial_symbols or settings.default_symbols)
        )
        # The boot set can never be unsubscribed at runtime (a stray or buggy
        # WS client must not be able to kick SPY off the stream); everything
        # else is fair game for deactivate.
        self._core_symbols: Set[str] = frozenset(self.symbols)
        # Runtime subscription changes (WS SUBSCRIBE/UNSUBSCRIBE) travel this
        # queue to the listener loop, which forwards incremental
        # subscribe/unsubscribe frames to the live Alpaca socket. Created
        # lazily on the loop side: an asyncio.Queue binds to the loop that
        # first touches it, and activate() may run before start().
        self._sub_control: Optional[asyncio.Queue] = None
        self._sub_control_lock = threading.Lock()
        self._sub_control_pending: List = []
        # Keep-alive refs for background daily-stats warm futures.
        self._daily_warm_tasks: set = set()
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

        # Multi-timeframe bars, kept on disk so a deploy does not start cold.
        # ``cache_dir`` is None in the test suite (STATE_DIR=off), which makes
        # this a pure RAM cache.
        self._bar_cache = BarCache(
            cache_dir,
            max_entries=MAX_CACHE_ENTRIES,
            ttl_for=lambda tf: TIMEFRAME_TTLS.get(tf, 60.0),
        )
        # Background refreshes run here so an expired entry never blocks a
        # request on Yahoo. Two workers bound the upstream fan-out.
        self._refresh_pool: Optional[ThreadPoolExecutor] = None
        self._refresh_pool_lock = threading.Lock()
        # Daily context (prev close / day range / volume) also survives a
        # restart: re-sweeping 14 symbols at boot is pure waste.
        self._stats_store = (
            JsonStateStore(cache_dir, filename="market_daily_stats.json")
            if cache_dir
            else None
        )

        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._prewarm_task: Optional[asyncio.Task] = None
        # Optional callable returning True while a client is subscribed to a
        # symbol. ``main`` attaches it; None keeps background prewarming on.
        self.subscriber_probe: Optional[Callable[[], bool]] = None
        # Fan-out throttle: the UI consumes quotes at ~25 FPS (40ms), so a
        # per-symbol emit window keeps the tick loop from re-decorating and
        # re-serialising every IEX print. ``latest_quotes`` is always updated.
        self._last_quote_emit: Dict[str, float] = {}

        # Stream health, readable without a journalctl session. A rejected
        # auth (another client holds the key's single session) looks exactly
        # like "the chart is quiet" from the outside, so the state has to be
        # something the health probe and the UI can see.
        self._stream_state: str = "idle"
        self._stream_since: float = 0.0
        self._stream_last_error: Optional[str] = None
        self._stream_failures: int = 0
        self._stream_quota: bool = False

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

        # Restore what the previous process already fetched, then fill the gaps.
        self._load_daily_stats_snapshot()
        self._preload_historical_bars_sync()
        # Warm the consolidated daily context before the first read: the
        # preloaded REST quotes carry no trade, and a row must not briefly
        # display a one-venue previous close as the market's. Not forced: a
        # snapshot restored from disk within its TTL is good enough.
        self.refresh_daily_stats()

    # The cache owns these; the names are kept because tests and the tickers
    # route read/write them directly.
    @property
    def multi_tf_bars(self) -> Dict[str, Dict[str, List[Bar]]]:
        return self._bar_cache.bars

    @property
    def _bars_cache_ts(self) -> Dict[str, Dict[str, float]]:
        return self._bar_cache.fetched_at

    # -- stream health ----------------------------------------------------
    def _record_stream_event(self, state: str, error: Optional[str] = None) -> None:
        """Track the socket state so it can be reported without reading logs.

        Only a *connected* event clears the failure count. Every reconnect
        attempt passes through ``connecting`` first; counting that as success
        would zero ``consecutive_failures`` before each failure could stack,
        and a two-hour rejection storm would read as a single failure.
        """
        self._stream_state = state
        self._stream_since = time.time()
        if error is not None:
            self._stream_last_error = error
            self._stream_quota = is_quota_rejection(error)
            self._stream_failures += 1
        elif state == "connected":
            self._stream_failures = 0
            self._stream_quota = False

    def stream_health(self) -> Dict[str, Any]:
        """Socket state: live, or degraded and why.

        A rejected session and a quiet market look identical from the outside
        (no ticks either way), so this is the only honest way for the health
        probe and the UI to tell them apart.
        """
        uptime = 0.0
        if self._stream_state == "connected" and self._stream_since:
            uptime = max(0.0, time.time() - self._stream_since)
        return {
            "provider": "alpaca",
            "feed": self.feed,
            "state": self._stream_state,
            "connected": self._stream_state == "connected",
            "uptime_s": round(uptime, 1),
            "consecutive_failures": self._stream_failures,
            "quota_exceeded": self._stream_quota,
            "last_error": self._stream_last_error,
            # The US daily-history fallback's own observability: cached frames
            # and the last upstream failure, so an outage is readable in
            # /api/health instead of only in the app log.
            "daily_fallback": defeatbeta_adapter.stats(),
            # The intraday charts' actual upstream is Yahoo (Alpaca serves
            # quotes/1m); surface its HTTP-failure counter here too so a
            # broken span contract (e.g. a tightened 59d ceiling blanking
            # every 5m/15m window) is visible on the US side as well.
            "yahoo_http_errors": YahooFinanceAdapter.http_error_stats(),
            # Same story for the deep-history archive: the paging chain
            # serves everything past Yahoo's 59d ceiling, and its failure
            # mode (HTTP error mid-chain) is chart holes, not an exception
            # the probe would see.
            "deep_hist_errors": self.deep_hist_error_stats(),
        }

    def _preload_historical_bars_sync(self) -> None:
        """Fetch latest historical 1-minute bars and quotes via REST on initialization.

        A restart is not a reason to refetch candles the previous process
        already stored: a 1m cache entry younger than a day still describes
        the same session, so it seeds ``historical_bars`` and that symbol
        skips its REST bars call. Quotes are always refetched — a quote goes
        stale in seconds, a past candle does not.
        """
        try:
            with httpx.Client(headers=self.headers, timeout=15.0) as client:
                for sym in self.symbols:
                    cached = self._bar_cache.get(sym, "1m")
                    if cached and self._bar_cache.age(cached) < 24 * 3600:
                        self.historical_bars[sym.upper()] = cached.bars[-120:]
                    else:
                        self._preload_bars_from_rest(client, sym)

                    # Fetch latest quote
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

    def _preload_bars_from_rest(self, client: httpx.Client, sym: str) -> None:
        # Deliberately kept out of the bar cache. These are the IEX venue's own
        # bars (~2% of consolidated volume); the cache's 1m entries are the
        # consolidated series the chart draws. Persisting them would let a
        # restart serve a candle from the wrong venue and look like a hit, and
        # the real one could not replace it until the TTL expired. They live
        # for this process only, as a last-resort fallback for ``get_bars``.
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

    def _cache_put(
        self, sym: str, tf: str, bars: List[Bar], ts: Optional[float] = None
    ) -> None:
        """Store bars in the cache (RAM + disk, oldest entry evicted)."""
        self._bar_cache.put(sym, tf, bars, fetched_at=ts)

    # -- fetching --------------------------------------------------------
    def _refresh_pool_executor(self) -> ThreadPoolExecutor:
        with self._refresh_pool_lock:
            if self._refresh_pool is None:
                self._refresh_pool = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="bar-refresh"
                )
            return self._refresh_pool

    def _volume_authority(self, sym: str) -> Dict[str, float]:
        """Per-day authoritative session volumes, keyed by venue-local day.

        The cached 1d series is the per-day truth (the 1d endpoint is not
        subject to the intraday inflation — measured: QQQ's 11-12 1d bar
        agrees with daily_stats' regularMarketVolume while its 15m bars run
        18x). ``daily_stats`` covers today when no 1d series is cached yet,
        but only in its consolidated form: the Alpaca snapshot fallback is a
        single IEX venue (~2% of tape) and would shrink honest bars 50x.
        Deliberately cache-only — never a network call inside a fetch path.
        """
        authority: Dict[str, float] = {}
        try:
            tz = ZoneInfo(market_of(sym).timezone)
        except Exception:
            return authority
        entry = self._bar_cache.get(sym, "1d")
        if entry and entry.bars:
            for b in entry.bars:
                if b.volume > 0:
                    day = datetime.fromtimestamp(b.time, tz=tz).strftime("%Y-%m-%d")
                    authority[day] = b.volume
        stats = self.daily_stats.get(sym) or {}
        volume = stats.get("volume")
        if volume and stats.get("consolidated"):
            today = datetime.now(tz).strftime("%Y-%m-%d")
            # A session total only grows, so whichever source reports more is
            # the better one: intraday the stats entry is the fresher running
            # total, after the close a stale pre-close entry must not drag the
            # cached 1d bar's final figure down.
            authority[today] = max(authority.get(today, 0.0), float(volume))
        return authority

    def _reconcile_volume(self, sym: str, tf: str, bars: List[Bar]) -> List[Bar]:
        """Guard every Yahoo-sourced intraday cache write (see the helper).

        Runs on both the fresh-window and the historical-segment fetch paths,
        which also covers 4h: the adapter aggregates it from 1h before this
        sees the bars, and the per-day sums are identical.
        """
        if tf not in INTRADAY_TIMEFRAMES or not bars:
            return bars
        authority = self._volume_authority(sym)
        if not authority:
            return bars
        try:
            return reconcile_intraday_volume(bars, authority, market_of(sym).timezone)
        except Exception as err:
            logger.warning("Volume reconcile skipped for %s %s: %s", sym, tf, err)
            return bars

    def _fetch_upstream(self, sym: str, tf: str) -> Optional[List[Bar]]:
        """Fetch one key and store it. The caller must own the refresh slot."""
        try:
            bars = YahooFinanceAdapter.fetch_bars(sym, timeframe=tf)
            if not bars and tf in defeatbeta_adapter.SUPPORTED_TIMEFRAMES \
                    and market_of(sym).code == "US":
                # Yahoo answered the whole-window request with nothing: the
                # 429 Edge disguise, or a listing Yahoo's coverage misses.
                # DefeatBeta's 30y US dailies fill the window instead of
                # leaving the chart blank (1d directly; 1wk/1mo aggregated);
                # a later successful Yahoo refresh overwrites the fallback
                # through the normal cycle. Trimmed to Yahoo's own whole-
                # window span, so the chart does not "shrink" when Yahoo
                # recovers — unless that would empty the window (a listing
                # dead for the whole span keeps its full real history).
                fallback = defeatbeta_adapter.fetch_history(sym, tf)
                if fallback:
                    span = WHOLE_WINDOW_SPAN_SECONDS.get(tf)
                    if span is None:
                        bars = fallback[0]
                    else:
                        cutoff = time.time() - span
                        bars = [b for b in fallback[0] if b.time >= cutoff] or fallback[0]
            if bars:
                bars = self._reconcile_volume(sym, tf, bars)
                # NOTE: never write last_trade_prices here — a historical close
                # must not overwrite a fresher live trade price.
                self._cache_put(sym, tf, bars)
                return bars
            return None
        except Exception as err:
            logger.warning("Yahoo Finance fetch failed for %s (%s): %s", sym, tf, err)
            self._bar_cache.cool_down(sym, tf)
            return None
        finally:
            self._bar_cache.end_refresh(sym, tf)

    def _fetch_and_cache(self, sym: str, tf: str) -> Optional[List[Bar]]:
        """Single-flight fetch: lead it, or wait for whoever is leading.

        Returns the bars on success, None on failure (after arming the
        cooldown). Never raises: this also runs on background threads.
        """
        if not self._bar_cache.begin_refresh(sym, tf):
            # Somebody else is already fetching this key; wait for their result.
            self._bar_cache.wait_for_refresh(sym, tf)
            entry = self._bar_cache.get(sym, tf)
            return entry.bars if entry else None
        return self._fetch_upstream(sym, tf)

    def _schedule_refresh(self, sym: str, tf: str) -> None:
        """Refresh a stale key in the background, at most one task per key."""
        if self._bar_cache.is_cooling(sym, tf):
            return
        if not self._bar_cache.begin_refresh(sym, tf):
            return  # another caller already scheduled this fetch
        try:
            self._refresh_pool_executor().submit(self._fetch_upstream, sym, tf)
        except RuntimeError:
            # Interpreter shutting down or pool already closed.
            self._bar_cache.end_refresh(sym, tf)

    def get_bars_with_meta(
        self, symbol: str, timeframe: str = "15m"
    ) -> Tuple[List[Bar], float]:
        """Bars plus the wall-clock time they were fetched (0.0 when unserved).

        Stale data is served immediately and refreshed in the background: a
        chart two minutes behind is useful, a chart that blocks for a second
        is not. Only a key with nothing cached at all pays the upstream
        round-trip, and concurrent callers share that one fetch.
        """
        sym = symbol.upper()
        tf = timeframe.lower()
        entry = self._bar_cache.get(sym, tf)

        if entry:
            if not self._bar_cache.is_fresh(entry, tf):
                self._schedule_refresh(sym, tf)
            return entry.bars, entry.fetched_at

        # Nothing cached: this caller has to wait, but only one of them fetches.
        if not self._bar_cache.is_cooling(sym, tf):
            bars = self._fetch_and_cache(sym, tf)
            if bars:
                return bars, self._bar_cache.fetched_at_of(sym, tf)

        # 1. If 1m requested, fallback to local 1m bars if available
        if tf == "1m" and self.historical_bars.get(sym):
            return self.historical_bars[sym], 0.0

        return [], 0.0

    def get_bars(self, symbol: str, timeframe: str = "15m") -> List[Bar]:
        """Bars for a symbol/timeframe, from cache whenever one exists."""
        bars, _ = self.get_bars_with_meta(symbol, timeframe)
        return bars

    def get_historical_bars(self, symbol: str) -> List[Bar]:
        return self.get_bars(symbol, timeframe="1m")

    def get_historical_segment(
        self, symbol: str, timeframe: str, before: int
    ) -> Tuple[List[Bar], float, bool]:
        """One immutable page of history strictly older than ``before``.

        Mirrors ``YahooQuoteFeed.get_historical_segment``: the US feed's own
        bar upstream is the same Yahoo chart endpoint, so the paging mechanics
        are identical; the segment lives under the same (symbol, timeframe)
        cache with an extended subkey.
        """
        sym = symbol.upper()
        tf = timeframe.lower()
        page_size = HIST_PAGE_BARS.get(tf, HIST_SEGMENT_BARS)
        seg_tf = self._bar_cache.hist_timeframe(tf, before)
        entry = self._bar_cache.get(sym, seg_tf)
        if entry:
            if not self._bar_cache.is_fresh(entry, seg_tf):
                self._schedule_hist_refresh(sym, tf, seg_tf, before)
            return entry.bars, entry.fetched_at, self._has_more(entry.bars, page_size, before, tf, sym)

        if self._bar_cache.is_cooling(sym, seg_tf):
            return [], 0.0, False
        if not self._bar_cache.begin_refresh(sym, seg_tf):
            self._bar_cache.wait_for_refresh(sym, seg_tf)
            entry = self._bar_cache.get(sym, seg_tf)
            if entry:
                return entry.bars, entry.fetched_at, self._has_more(entry.bars, page_size, before, tf, sym)
            return [], 0.0, False
        try:
            bars = self._fetch_hist_segment(sym, tf, seg_tf, before)
        finally:
            self._bar_cache.end_refresh(sym, seg_tf)
        if bars is None:
            return [], 0.0, False
        return bars, self._bar_cache.fetched_at_of(sym, seg_tf), self._has_more(bars, page_size, before, tf, sym)

    @staticmethod
    def _has_more(bars: List[Bar], page_size: int, before: int, tf: str, sym: str) -> bool:
        # A page-full is the normal "more below" signal, but it can never fire
        # for the FIRST page: its 366d window is clamped to Yahoo's 59d span
        # (15m ≈ 1119 bars < 2000), so a length-only check strands the client
        # at the seed edge forever (measured 2026-11-14: QQQ 15m page 1 =
        # 1119 bars, has_more=0, chart stuck at 09-16 while the prewalk sat
        # unrequested on disk). For the US deep timeframes the real floor is
        # the IEX archive's, not Yahoo's sliding edge: any cursor above it
        # definitionally has more history below. Empty pages still stop the
        # client (it halts on an empty merge) and the walker (failure count).
        if len(bars) >= page_size:
            return True
        if not bars:
            # An empty page never promises more: the client halts on an empty
            # merge and the walker counts it as a failure — has_more=1 here
            # would only fake a retry loop.
            return False
        return (
            tf in ALPACA_DEEP_HIST_TIMEFRAMES
            and market_of(sym).code == "US"
            and before > DEEP_HIST_FLOOR
        )

    def _fetch_hist_segment(self, sym: str, tf: str, seg_tf: str, before: int) -> Optional[List[Bar]]:
        try:
            page_size = HIST_PAGE_BARS.get(tf, HIST_SEGMENT_BARS)
            span_days = TIMEFRAME_SPAN_DAYS.get(tf)
            oldest_allowed = int(time.time()) - span_days * 86400 if span_days else 0
            alpaca_deep = (
                span_days is not None and tf in ALPACA_DEEP_HIST_TIMEFRAMES
                and market_of(sym).code == "US"
                and oldest_allowed and before < oldest_allowed
            )
            period1 = max(0, before - HIST_PAGE_SPAN_SECONDS)
            if alpaca_deep:
                # Past Yahoo's coverage ceiling: this page can only come from
                # the IEX archive (measured 2026-11-13: 15m/5m bars back to
                # 2020-07). OHLC is real traded price; per-bar volume runs ~2%
                # of the consolidated tape — under-reporting, which the
                # reconciliation below deliberately never rewrites.
                bars = self._fetch_alpaca_hist(sym, tf, period1, before)
            else:
                if oldest_allowed and period1 < oldest_allowed:
                    period1 = oldest_allowed
                bars = YahooFinanceAdapter.fetch_bars(sym, timeframe=tf, period1=period1, period2=before)
                bars = [b for b in bars if b.time < before]
            # Reconcile before the page trim: a day split across the page
            # boundary would sum to less than its real total and could slip
            # past the threshold.
            bars = self._reconcile_volume(sym, tf, bars)
            if len(bars) > page_size:
                bars = bars[-page_size:]
            if not bars and not alpaca_deep and span_days and tf in ALPACA_DEEP_HIST_TIMEFRAMES \
                    and market_of(sym).code == "US" and before > DEEP_HIST_FLOOR:
                # The sliding-edge gap: a cursor ABOVE the ceiling can also
                # answer empty (measured 2026-11-14: the fresh window's oldest
                # bar 09-16 13:30 sits above Yahoo's oldest, which slid past
                # it with "now"). The archive serves this cursor fine — fall
                # through to it instead of cooling down, so the client's
                # first prefill page (which always uses this cursor) lands in
                # the archive instead of stopping the walk at the seed edge.
                deep = self._fetch_alpaca_hist(sym, tf, max(0, before - HIST_PAGE_SPAN_SECONDS), before)
                deep = self._reconcile_volume(sym, tf, deep)
                if len(deep) > page_size:
                    deep = deep[-page_size:]
                if deep:
                    self._bar_cache.put(sym, seg_tf, deep)
                    return deep
                # Both sources empty above the floor: the symbol is younger
                # than the archive (e.g. COIN, IPO 2021) — a real terminal
                # edge. Cache the emptiness.
                self._bar_cache.put(sym, seg_tf, [], time.time())
                return []
            if not bars:
                # An empty page is only a *terminal* edge when it comes from
                # the physical archive (alpaca_deep: the IEX floor is fixed).
                # On the Yahoo path the upstream edge MOVES with the 59d
                # window: a cursor just inside the ceiling can legitimately
                # answer empty today (measured 2026-11-14: cursor 09-16 13:30,
                # Yahoo's oldest bar had slid past it) and non-empty tomorrow.
                # Caching that as a "boundary" strands the chart at the seed
                # edge forever — cool down instead so the next request
                # retries.
                if not alpaca_deep:
                    self._bar_cache.cool_down(sym, seg_tf)
                else:
                    self._bar_cache.put(sym, seg_tf, [], time.time())
                return []
            self._bar_cache.put(sym, seg_tf, bars)
            return bars
        except Exception as err:
            logger.warning("Yahoo hist segment failed for %s %s<%s: %s", sym, tf, before, err)
            self._bar_cache.cool_down(sym, seg_tf)
            return None

    def _fetch_alpaca_hist(self, sym: str, tf: str, period1: int, before: int) -> List[Bar]:
        """One deep-history window from the Alpaca IEX archive, paged to the end.

        Alpaca's ``end`` is inclusive while the segment contract is strictly
        ``time < before``, so the bound is shifted one second. The whole
        window is gathered and the caller trims to the page contract, so
        ``X-Bars-Has-More`` keeps meaning "this window is longer than one
        page" — not "the HTTP page was full".

        The chain MUST run to exhaustion: the per-response page is much
        smaller than the requested ``limit`` (measured 2026-11-13: ~750
        bars per response on QQQ 15m regardless of limit=10000), so a
        fixed small page count silently truncates the 366d window and
        strands a multi-month hole next to the cursor that no later page
        can fill (paging only walks older). Alpaca always terminates the
        token chain; the cap only guards a runaway loop. Any HTTP failure
        raises — the caller cool-downs instead of caching, because a
        partial gather cached as a real segment would be a fake boundary.
        """
        tf_map = {"5m": "5Min", "15m": "15Min"}
        interval = tf_map[tf]
        params: Dict[str, Any] = {
            "symbols": sym,
            "timeframe": interval,
            "start": datetime.fromtimestamp(period1, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": datetime.fromtimestamp(before - 1, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": "10000",
            "adjustment": "split",
        }
        bars: List[Bar] = []
        pages = 0
        with httpx.Client(headers=self.headers, timeout=30.0) as client:
            while True:
                resp = client.get(f"{self.data_base_url}/stocks/bars", params=params)
                if resp.status_code != 200:
                    self.record_deep_hist_error(resp.status_code)
                    raise RuntimeError(
                        f"Alpaca deep hist HTTP {resp.status_code} for {sym} {tf} <{before}")
                payload = resp.json()
                pages += 1
                bars.extend(AlpacaDataAdapter.parse_rest_bars_response(payload, symbol=sym))
                token = payload.get("next_page_token")
                if not token:
                    break
                if pages >= 100:  # 100 pages ≈ 75k bars — no real 366d window needs this
                    self.record_deep_hist_error(0)
                    raise RuntimeError(
                        f"Alpaca deep hist token chain did not terminate for {sym} {tf} <{before}")
                params["page_token"] = token
        self.deep_hist_ok()
        bars = [b for b in bars if b.time < before]
        return bars

    # Deep-history HTTP-failure observability (mirror of the Yahoo adapter's
    # counter): a broken archive or a rate-limited chain must be visible in
    # /api/health, not only as multi-month chart holes that log lines.
    _deep_hist_errors = {"count": 0, "consecutive": 0, "last_ts": 0.0}
    _deep_hist_lock = threading.Lock()

    @classmethod
    def record_deep_hist_error(cls, status: int) -> None:
        with cls._deep_hist_lock:
            stats = cls._deep_hist_errors
            stats["count"] += 1
            stats["consecutive"] += 1
            stats["last_ts"] = time.time()

    @classmethod
    def deep_hist_ok(cls) -> None:
        """A fully gathered chain: the upstream is answering — reset the streak."""
        with cls._deep_hist_lock:
            cls._deep_hist_errors["consecutive"] = 0

    @classmethod
    def deep_hist_error_stats(cls) -> Dict[str, Any]:
        with cls._deep_hist_lock:
            return dict(cls._deep_hist_errors)

    def _schedule_hist_refresh(self, sym: str, tf: str, seg_tf: str, before: int) -> None:
        if self._bar_cache.is_cooling(sym, seg_tf):
            return
        if not self._bar_cache.begin_refresh(sym, seg_tf):
            return
        try:
            self._refresh_pool_executor().submit(self._fetch_hist_segment_and_close, sym, tf, seg_tf, before)
        except RuntimeError:
            self._bar_cache.end_refresh(sym, seg_tf)

    def _fetch_hist_segment_and_close(self, sym: str, tf: str, seg_tf: str, before: int) -> None:
        try:
            self._fetch_hist_segment(sym, tf, seg_tf, before)
        finally:
            self._bar_cache.end_refresh(sym, seg_tf)

    def decorate_quote(self, quote: Optional[Quote]) -> Optional[Quote]:
        """Public decoration entry point for callers outside this feed.

        The tick path stopped decorating (2026-11-17: the per-tick rebuild
        kept ~1/3 of a core busy with zero websocket clients), so the
        broadcast path in ``main`` decorates behind its own subscriber gate
        via this method.
        """
        return self._decorate_daily(quote)

    def _decorate_daily(self, quote: Optional[Quote]) -> Optional[Quote]:
        """Attach the consolidated daily context and the age of the print.

        Alpaca's quote payloads carry neither a day range nor a reference
        close, and its snapshot is IEX only. Two consequences are fixed here:

        * The % change on a row is computed from ``prev_close``. IEX's own
          previous close is one venue's last print (733.43 for SPY when the
          consolidated close was 733.00), so it has to be replaced with the
          consolidated figure carried by ``daily_stats``.
        * ``last`` must be a trade. While the venue is open the venue print is
          the freshest one available; once it is shut the consolidated close
          is the authoritative price, not a single-venue print that missed the
          closing auction.
        * A book is only worth showing if a consumer could actually trade
          against it. A one-sided or absurdly wide IEX book (measured: SPY
          bid 712.63 / ask 764.24, a 7% spread) is withheld and
          ``book_available`` is set False rather than passing it off as a
          market.
        * ``volume`` means the session's consolidated volume. IEX's own number
          is a fraction of it and is replaced or zeroed, never shown as the
          total.

        The quote is also stamped with ``age_seconds`` and ``is_stale`` so the
        UI can tell a live feed from a correctly-remembered close.
        """
        if quote is None:
            return None

        symbol = quote.symbol.upper()
        updates: Dict[str, Any] = {}
        stats = self.daily_stats.get(symbol) or {}

        if stats:
            updates["prev_close"] = quote.prev_close or stats.get("prev_close")
            updates["day_high"] = quote.day_high or stats.get("day_high")
            updates["day_low"] = quote.day_low or stats.get("day_low")

            # IEX is one venue carrying a fraction of consolidated volume, so
            # its size is not "the volume of the day" and must not be rendered
            # as such. Prefer the consolidated figure; drop the venue's number
            # rather than pass off ~80 shares as a session total.
            if stats.get("volume"):
                updates["volume"] = int(stats["volume"])
            elif quote.volume:
                updates["volume"] = 0

            trusted = stats.get("last")
            day_lo = updates.get("day_low")
            day_hi = updates.get("day_high")
            if trusted:
                try:
                    venue_open = is_market_open(market_of(symbol))
                except Exception:
                    venue_open = True
                if not venue_open and stats.get("consolidated"):
                    updates["last"] = trusted
                elif quote.last <= 0 and within_day_range(trusted, day_lo, day_hi):
                    updates["last"] = trusted
                elif (
                    not within_day_range(quote.last, day_lo, day_hi)
                    and within_day_range(trusted, day_lo, day_hi)
                ):
                    updates["last"] = trusted

        # An IEX-only book is one-sided or absurdly wide around the close/in the
        # after hours. A consumer that sees a bid must be able to trade against
        # it, so such a book is withheld instead of passed through as if it were
        # the market. The real print above still stands.
        book = bool(
            AlpacaDataAdapter.is_tradeable_quote(quote.bid, quote.ask)
        )
        updates["book_available"] = book
        if not book:
            updates["bid"] = 0.0
            updates["ask"] = 0.0

        # When ``last`` came from the consolidated tape, its timestamp must
        # come with it: the venue quote's own time describes a different print,
        # so the age shown would belong to the wrong moment.
        effective_ts = quote.timestamp
        if "last" in updates and stats.get("last_ts"):
            try:
                effective_ts = datetime.fromtimestamp(
                    float(stats["last_ts"]), tz=timezone.utc
                )
                updates["timestamp"] = effective_ts
            except Exception:
                effective_ts = quote.timestamp

        try:
            updates.update(freshness_updates(effective_ts, market_of(symbol)))
        except Exception:
            pass

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
        """Refresh per-symbol daily context from the consolidated tape.

        A watchlist row needs a previous close (for the % change) and today's
        range. Those must come from the whole market, not from one venue's
        share of it: Alpaca's free feed is IEX only (~2% of volume), whose
        daily bar misses the closing auction, so its previous close was 733.43
        for SPY when the consolidated close was 733.00. Yahoo carries the
        consolidated figure, so it is the primary source here and the Alpaca
        snapshot is only the fallback for symbols Yahoo cannot answer.
        """
        syms = sorted(symbols or self.symbols)
        if not syms:
            return

        # The timestamp is set before the (slow) upstream sweep so a second
        # caller waits for the TTL instead of starting a duplicate burst.
        with self._snapshot_lock:
            now = time.time()
            ttl = self._daily_ttl()
            if not force and self._snapshot_ts and now - self._snapshot_ts < ttl:
                return
            self._snapshot_ts = now

        missing = self._refresh_consolidated_daily(syms)
        if missing:
            self._refresh_alpaca_snapshots(missing)
        self._save_daily_stats_snapshot()

    def _load_daily_stats_snapshot(self) -> None:
        """Restore the previous process's daily context, if there is one.

        Sweeping 14 symbols at boot is pure waste when the file on disk is
        still inside its TTL — the TTL check in ``refresh_daily_stats``
        decides whether the restored copy is good enough.
        """
        if self._stats_store is None:
            return
        snapshot = self._stats_store.load()
        if not isinstance(snapshot, dict):
            return
        stats = snapshot.get("stats")
        fetched_at = snapshot.get("fetched_at")
        if not isinstance(stats, dict) or not isinstance(fetched_at, (int, float)):
            return
        try:
            self.daily_stats.update(
                {
                    str(sym).upper(): dict(entry)
                    for sym, entry in stats.items()
                    if isinstance(entry, dict)
                }
            )
        except (TypeError, ValueError) as err:
            logger.warning("Ignoring market daily stats snapshot: %s", err)
            return
        self._snapshot_ts = float(fetched_at)

    def _save_daily_stats_snapshot(self) -> None:
        if self._stats_store is None:
            return
        self._stats_store.save({"fetched_at": time.time(), "stats": self.daily_stats})

    def _daily_ttl(self) -> float:
        """Longer TTL when every venue is shut: a close does not move."""
        try:
            closed = all(not is_market_open(market_of(sym)) for sym in self.symbols)
        except Exception:
            closed = False
        return self.SNAPSHOT_TTL_CLOSED if closed else self.SNAPSHOT_TTL

    @staticmethod
    def _daily_from_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
        """Consolidated daily context from a Yahoo chart ``meta`` block.

        ``last_ts`` is the exchange time of ``last`` itself. Without it the
        served quote keeps the venue quote's timestamp while carrying a
        different venue's price, and the age shown belongs to the wrong print.
        """

        def num(key: str) -> Optional[float]:
            value = meta.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
            return None

        entry: Dict[str, Any] = {"consolidated": 1.0}
        for key, source in (
            ("prev_close", "chartPreviousClose"),
            ("last", "regularMarketPrice"),
            ("day_high", "regularMarketDayHigh"),
            ("day_low", "regularMarketDayLow"),
            ("volume", "regularMarketVolume"),
        ):
            value = num(source) or (num("previousClose") if key == "prev_close" else None)
            if value is not None:
                entry[key] = value

        # Exchange time of the consolidated print, so ``last`` and its age
        # describe the same moment.
        trade_ts = meta.get("regularMarketTime")
        if isinstance(trade_ts, (int, float)) and trade_ts > 0:
            entry["last_ts"] = float(trade_ts)

        return entry if len(entry) > 1 else {}

    def _refresh_consolidated_daily(self, syms: List[str]) -> List[str]:
        """Fetch consolidated daily context; return symbols that failed."""
        missing: List[str] = []
        for sym in syms:
            try:
                meta = YahooFinanceAdapter.fetch_meta(sym)
            except Exception as err:
                logger.warning("Consolidated daily fetch failed for %s: %s", sym, err)
                meta = None
            entry = self._daily_from_meta(meta) if meta else {}
            if entry:
                self.daily_stats[sym] = {**self.daily_stats.get(sym, {}), **entry}
            else:
                missing.append(sym)
        return missing

    def _refresh_alpaca_snapshots(self, syms: List[str]) -> None:
        """Single-venue fallback for symbols Yahoo did not answer."""
        try:
            with httpx.Client(headers=self.headers, timeout=10.0) as client:
                resp = client.get(
                    f"{self.data_base_url}/stocks/snapshots",
                    params={"symbols": ",".join(syms), "feed": self.feed},
                )
                if resp.status_code != 200:
                    return
                stats = AlpacaDataAdapter.parse_snapshots_response(resp.json())
                for sym, entry in (stats or {}).items():
                    # Never let the fallback overwrite a consolidated close.
                    current = self.daily_stats.get(sym) or {}
                    if current.get("consolidated"):
                        continue
                    self.daily_stats[sym] = {**current, **entry}
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

    # -- watch (runtime stream subscriptions) -------------------------------
    def activate(self, symbol: str) -> None:
        """Add a symbol to the live IEX stream at runtime (WS SUBSCRIBE).

        Adds to the subscription set and queues an incremental subscribe
        frame for the listener loop; a reconnect would pick the symbol up
        anyway, but the queue makes the first tick arrive without waiting
        for one. Daily context (prev close / day range) is warmed in the
        background so the first decorated quote is not range-less. Core
        symbols are no-ops (already subscribed).
        """
        sym = self._canonical(symbol)
        if not sym:
            return
        with self._sub_control_lock:
            if sym in self.symbols:
                return
            if len(self.symbols - self._core_symbols) >= MAX_DYNAMIC_SYMBOLS:
                logger.warning(
                    "Dynamic subscription cap reached (%d); not subscribing %s",
                    MAX_DYNAMIC_SYMBOLS, sym,
                )
                return
            self.symbols.add(sym)
            self._queue_control(("add", sym))
        self._warm_daily_stats_background(sym)

    def deactivate(self, symbol: str) -> None:
        """Remove a runtime-added symbol from the live IEX stream.

        Boot-set symbols are protected: a stray or buggy WS client must not
        be able to kick SPY off the stream for every other consumer.
        """
        sym = self._canonical(symbol)
        if not sym:
            return
        with self._sub_control_lock:
            if sym in self._core_symbols or sym not in self.symbols:
                return
            self.symbols.discard(sym)
            self._queue_control(("remove", sym))

    @staticmethod
    def _canonical(symbol: str) -> Optional[str]:
        ref = resolve_symbol(symbol)
        return ref.symbol if ref else None

    @staticmethod
    def _sub_change_frame(change: tuple) -> str:
        """One runtime change -> incremental Alpaca frame (JSON text).

        Our internal verbs ("add"/"remove") map onto the stream's actions
        ("subscribe"/"unsubscribe").
        """
        action, sym = change
        stream_action = {"add": "subscribe", "remove": "unsubscribe"}[action]
        return json.dumps(
            {"action": stream_action, "quotes": [sym], "trades": [sym], "bars": [sym]}
        )

    def _queue_control(self, change: tuple) -> None:
        """Hand a subscription change to the listener loop.

        Before the loop starts there is no loop-bound queue to hold the
        change, so pending items park in a plain list; the loop drains both
        on first connect.
        """
        if self._sub_control is not None:
            self._sub_control.put_nowait(change)
        else:
            self._sub_control_pending.append(change)

    def _take_sub_control(self) -> asyncio.Queue:
        """Loop-side accessor: create the queue bound to the running loop.

        Pre-loop changes parked in the plain list were already applied to
        ``self.symbols``, so the boot subscribe covers them — dropping here
        avoids a redundant duplicate subscribe frame per symbol.
        """
        with self._sub_control_lock:
            if self._sub_control is None:
                self._sub_control = asyncio.Queue()
                for change in self._sub_control_pending:
                    action, sym = change
                    if action == "add" and sym in self.symbols:
                        continue
                    self._sub_control.put_nowait(change)
                self._sub_control_pending.clear()
            return self._sub_control

    def _warm_daily_stats_background(self, sym: str) -> None:
        """Warm one symbol's daily context off the caller's thread."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            task = loop.run_in_executor(None, self.ensure_daily_stats, [sym])
            # Keep a reference so the executor future is not garbage-collected.
            self._daily_warm_tasks.add(task)
            task.add_done_callback(self._daily_warm_tasks.discard)
        except Exception:
            pass

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
        self._deep_hist_task = asyncio.create_task(self._deep_hist_prewarm_loop())

    async def stop(self) -> None:
        self._running = False
        self._record_stream_event("stopped")
        for task in (self._ws_task, self._sync_task, self._prewarm_task, self._deep_hist_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        with self._refresh_pool_lock:
            pool, self._refresh_pool = self._refresh_pool, None
        if pool is not None:
            # Do not wait: an in-flight upstream fetch may be seconds from
            # returning, and a shutting-down process must not stall on it.
            pool.shutdown(wait=False, cancel_futures=True)
        YahooFinanceAdapter.close_client()

    async def _websocket_listener_loop(self) -> None:
        """Connects and reconnects to Alpaca IEX stream with exponential backoff.

        Two failures need different pacing. A dropped socket is worth retrying
        within seconds. A rejected auth because another client holds this key's
        single session is not: the owner has to leave, which we neither see nor
        control, so hammering the endpoint at network cadence only fills the
        log. Quota rejections back off on their own schedule and log sparsely.
        """
        backoff = INITIAL_RECONNECT_BACKOFF
        quota_backoff = QUOTA_INITIAL_BACKOFF
        quota_hits = 0
        while self._running:
            connected_at = time.monotonic()
            self._record_stream_event("connecting")
            try:
                async with websockets.connect(self.stream_url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Connected to Alpaca IEX Streamer: %s", self.stream_url)

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
                    for item in auth_data if isinstance(auth_data, list) else [auth_data]:
                        if isinstance(item, dict) and item.get("T") == "error":
                            # Wrong key, or another backend already holds the
                            # session for this key. Fail loudly instead of
                            # looping on a subscription that will never tick.
                            raise RuntimeError(
                                f"Alpaca stream auth rejected: {item.get('msg')} (code={item.get('code')})"
                            )

                    # 3. Subscribe to quotes and bars. Pre-loop runtime
                    # changes (activate() before start()) must be folded in:
                    # take over the pending queue here.
                    sub_control = self._take_sub_control()
                    sub_msg = {
                        "action": "subscribe",
                        "quotes": list(self.symbols),
                        "bars": list(self.symbols),
                        "trades": list(self.symbols)
                    }
                    await ws.send(json.dumps(sub_msg))
                    sub_resp = await ws.recv()

                    self._record_stream_event("connected")
                    logger.info(
                        "Alpaca IEX stream authenticated and subscribed: %s",
                        ", ".join(sorted(self.symbols)),
                    )
                    quota_backoff = QUOTA_INITIAL_BACKOFF
                    quota_hits = 0

                    # 4. Message processing loop. Two sources wake it: an
                    # IEX frame on the socket, or a runtime subscription
                    # change queued by activate()/deactivate() — the latter
                    # is forwarded as an incremental frame without waiting
                    # for a reconnect.
                    recv_task: Optional[asyncio.Task] = None
                    ctrl_task: Optional[asyncio.Task] = None
                    msg_str: Optional[str] = None
                    while self._running:
                        if recv_task is None:
                            recv_task = asyncio.ensure_future(ws.recv())
                        if ctrl_task is None:
                            ctrl_task = asyncio.ensure_future(sub_control.get())
                        done, _ = await asyncio.wait(
                            {recv_task, ctrl_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if recv_task in done:
                            msg_str = recv_task.result()
                            recv_task = None
                        else:
                            recv_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await recv_task
                            recv_task = None
                            msg_str = None  # control-only wake
                        if ctrl_task in done:
                            change = ctrl_task.result()
                            ctrl_task = None
                            if change is not None:
                                await ws.send(self._sub_change_frame(change))
                                logger.info(
                                    "Alpaca stream %s %s at runtime",
                                    change[0], change[1],
                                )
                        else:
                            ctrl_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await ctrl_task
                            ctrl_task = None
                        if msg_str is None:
                            continue
                        items = json.loads(msg_str)
                        if not isinstance(items, list):
                            items = [items]

                        for item in items:
                            t_type = item.get("T")
                            if t_type == "error":
                                logger.error(
                                    "Alpaca stream error: %s (code=%s)",
                                    item.get("msg"),
                                    item.get("code"),
                                )
                                continue
                            if t_type == "subscription":
                                # Ack for a subscribe/unsubscribe frame: the
                                # only server-side proof a runtime change took.
                                logger.info(
                                    "Alpaca stream subscription now: quotes=%s trades=%s",
                                    ",".join(item.get("quotes") or []),
                                    ",".join(item.get("trades") or []),
                                )
                                continue
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
                                # Conflate at the source. Parsing, decorating,
                                # storing and scheduling a broadcast per IEX
                                # print kept a core busy all session while the
                                # UI can only consume ~25 FPS; anything inside
                                # the 40ms window is dropped (the next print
                                # carries the same or newer book).
                                now_tick = time.monotonic()
                                if now_tick - self._last_quote_emit.get(sym, 0.0) < QUOTE_EMIT_SECONDS:
                                    continue
                                self._last_quote_emit[sym] = now_tick

                                last_p = self.last_trade_prices.get(sym)
                                quote = AlpacaDataAdapter.parse_stream_quote(item, last_price=last_p)
                                if quote:
                                    # Store and forward the RAW venue quote.
                                    # The decoration here used to rebuild the
                                    # Quote with consolidated day stats and
                                    # freshness per tick (two is_market_open
                                    # round trips + a model_copy), which kept
                                    # ~1/3 of a core busy during a session
                                    # even with zero websocket clients. The
                                    # matching/market-to-market consumers only
                                    # read last/bid/ask, which are raw fields,
                                    # and the read path (get_latest_quote)
                                    # decorates on the way out -- so the
                                    # display work belongs there, done once
                                    # per served request instead of once per
                                    # accepted print.
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
                # A close frame carries the *reason* the stream hung us up on
                # (Alpaca evicts duplicate sessions for one key), so surface
                # code/reason instead of the opaque exception text alone.
                # Read them off the frame: ``ConnectionClosed.code``/``.reason``
                # are deprecated since websockets 13.1.
                close_frame = getattr(e, "rcvd", None) or getattr(e, "sent", None)
                close_code = getattr(close_frame, "code", None)
                close_reason = getattr(close_frame, "reason", None)
                uptime = time.monotonic() - connected_at
                self._record_stream_event("degraded", str(e))

                if self._stream_quota:
                    # Nothing local fixes this. Pace the retries and say it
                    # once per burst instead of once per attempt; each line is
                    # 30s apart, so the repetition is what buried the cause.
                    quota_hits += 1
                    delay = quota_backoff
                    quota_backoff = min(quota_backoff * 1.5, QUOTA_MAX_BACKOFF)
                    if quota_hits == 1 or quota_hits % QUOTA_LOG_EVERY == 0:
                        logger.warning(
                            "Alpaca stream session unavailable (%s): another client "
                            "holds this key's IEX session. Retrying quietly at %.0fs; "
                            "quotes still refresh over REST. attempt=%d",
                            e, delay, quota_hits,
                        )
                else:
                    delay = backoff
                    backoff = next_reconnect_backoff(backoff, uptime)
                    logger.warning(
                        "Alpaca Streamer disconnected (%s%s) after %.0fs, reconnecting in %.1fs...",
                        e,
                        f", code={close_code}, reason={close_reason!r}" if close_code is not None else "",
                        uptime,
                        delay,
                    )
                await asyncio.sleep(delay)
            finally:
                # Exception/cancel exits must not strand the pending
                # recv/control futures: their exceptions would otherwise
                # surface as "Task exception was never retrieved" noise.
                for task in (locals().get("recv_task"), locals().get("ctrl_task")):
                    if task is not None and not task.done():
                        task.cancel()

    async def _periodic_quote_sync_loop(self) -> None:
        """
        Polls latest quote every 15 seconds during off-hours or quiet periods
        to keep frontend alive and heartbeating.
        """
        last_idle_sweep = 0.0
        while self._running:
            try:
                await asyncio.sleep(15)
                if not self._running:
                    break

                # Keep previous closes and day ranges warm for the watchlist.
                # The sweep is one Yahoo request per symbol, so it skips itself
                # while nobody is connected — but not indefinitely: on an
                # always-on server that would leave the day range frozen.
                now = time.time()
                if self._has_chart_subscribers() or now - last_idle_sweep > IDLE_SWEEP_SECONDS:
                    last_idle_sweep = now
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

    def _has_chart_subscribers(self) -> bool:
        """True while at least one websocket client is watching a quote or a bar.

        Registered by ``main`` so the loop can tell "somebody is on a chart"
        from "the server is sitting idle" without importing the websocket
        manager into the feed.
        """
        probe = getattr(self, "subscriber_probe", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:
            return True

    async def _prewarm_cache_loop(self) -> None:
        """
        Background task:
        Smoothly pre-warms key candles (1m, 5m, 15m, 1h, 1d) for core symbols
        (SPY, QQQ, AAPL, NVDA, TSLA).
        Runs gently with rate-limit protection so user requests are served
        without waiting on Yahoo.

        1m is in the list because it is the timeframe the chart opens on, and
        it is the one a fresh deploy cannot already have: the boot preload
        fills it from the IEX venue, which is not the consolidated series
        ``get_bars`` serves. Leaving it out meant the first minute chart after
        every deploy paid a full upstream round-trip, then stayed cold on disk
        until someone happened to request it again.

        Skipped entirely while no client is connected: candle payloads are
        megabytes of JSON parsed on the event loop, and prewarming for an
        empty room is what kept the worker pegged once the tab was closed.
        """
        await asyncio.sleep(2.0)
        core_symbols = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
        core_timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]

        while self._running:
            try:
                if not self._has_chart_subscribers():
                    await asyncio.sleep(self.IDLE_PREWARM_SECONDS)
                    continue
                for sym in core_symbols:
                    if not self._running:
                        break
                    for tf in core_timeframes:
                        if not self._running:
                            break
                        now = time.time()
                        cached_ts = self._bars_cache_ts.get(sym, {}).get(tf, 0.0)
                        ttl = TIMEFRAME_TTLS.get(tf, 60.0)

                        if now - cached_ts > (ttl * 0.7) and not self._bar_cache.is_cooling(sym, tf):
                            try:
                                # Single-flight: if a request is already fetching
                                # this key, wait for its result instead of adding
                                # a second upstream call.
                                await asyncio.to_thread(self._fetch_and_cache, sym, tf)
                            except Exception as e:
                                logger.debug("Prewarm failed for %s (%s): %s", sym, tf, e)
                            await asyncio.sleep(0.5)

                await asyncio.sleep(45.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error in prewarm cache loop: %s", e)
                await asyncio.sleep(10.0)

    async def _deep_hist_prewarm_loop(self) -> None:
        """Walk every catalog symbol's archive toward its floor.

        The chart's "show me all the data" ask is answered by the segment
        chain, but a chain of ~100 IEX pages at ~15-20s each is ~30 minutes of
        wall time no user should spend watching a badge. The archive is
        immutable, so the whole walk is a *server-side* concern: this runner
        pages each (symbol, timeframe) via ``get_historical_segment`` — the
        exact path a chart request takes — until ``X-Bars-Has-More`` reports
        the floor. Every page lands in the same RAM+disk cache with its
        7-day TTL, so once a span has been walked the client's prefill is a
        sequence of cache hits (measured ~300ms per page from CF/edge).

        Every market walks, not just the US list (2026-11-15: HK/JP/CN
        charts were first-touch crawls). The floors differ by source: US
        15m/5m page past Yahoo's 59d edge into the IEX archive; other
        markets stop at Yahoo's ceiling — their walk is a handful of pages,
        then the walker's floor detection ends it. 1h/4h/1d Yahoo spans
        (730d / unlimited) are market-independent and walk everywhere.
        Prewarm timeframes stay 15m/5m: the deep 1h/4h/1d walk is a
        separate decision (110 pages × 3 more tfs would triple the boot
        IO for spans users rarely page).

        Restart-safe by construction: pages already on disk short-circuit in
        the cache, so a re-boot resumes near where the last process left off.
        Ordering walks one timeframe per symbol fully before the next
        timeframe: the chart's default (15m) completes first. Upstream
        trouble surfaces inside ``get_historical_segment`` as a
        ``([], 0.0, False)`` tuple (failure == empty), which the walker
        treats as retryable rather than terminal — the except below is only
        a safety net for escapes that bypass that contract.

        Two throttle gates (2026-11-15, see the constants' history): the
        whole loop idles while no chart client is connected, and each pass
        carries a page budget so the frontier extension cannot run away —
        the archive to the 2016 floor does not fit in the disk budget, so
        without a cap every pass ends in a long real-fetch tail.
        """
        await asyncio.sleep(self.DEEP_HIST_BOOT_GRACE)
        tfs = list(self.DEEP_HIST_PREWARM_TFS)
        while self._running:
            if not self._has_chart_subscribers():
                # Nobody is watching a chart: paging the archive serves
                # nothing. Re-check every few minutes instead of holding the
                # pass cadence, so a client that connects gets its first
                # bounded pass promptly.
                await asyncio.sleep(self.DEEP_HIST_IDLE_POLL)
                continue
            for tf in tfs:
                for sym in self._deep_hist_symbols():
                    if not self._running:
                        return
                    try:
                        await self._deep_hist_walk_symbol(
                            sym, tf, page_budget=self.DEEP_HIST_PASS_PAGE_BUDGET
                        )
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        logger.warning("Deep-hist prewalk %s %s failed: %s", sym, tf, exc)
                        # Safety net (the walk itself already parks on
                        # temporary empties): something escaped the
                        # failure==empty contract — brief park, next symbol.
                        await asyncio.sleep(10.0)
                        break
            # The full-pass re-walk re-serves every page from cache; the only
            # upstream work is the frontier extension, now bounded by the
            # per-symbol page budget above.
            await asyncio.sleep(self.DEEP_HIST_REWALK_SECONDS)

    def _deep_hist_symbols(self) -> List[str]:
        """Every market's catalog, US first, one walk slot per symbol.

        The walk serves whatever a chart could open: the tracked US list
        plus each market's curated catalog (HK/JP/CN were skipped outright
        and left first-touch crawls). US leads so the busiest charts warm
        first; interleaving markets mid-pass would only reorder cache hits,
        and the page pace (DEEP_HIST_PAGE_PACE) already caps the write rate
        for the whole sequence — the 1GB box's protection is the pace, not
        the symbol list.
        """
        seen: Dict[str, str] = {}
        for sym in sorted(self.symbols):
            seen[sym.upper()] = sym.upper()
        for market in MARKETS.all():
            for sym in market.catalog:
                seen.setdefault(sym.upper(), sym.upper())
        return list(seen.values())

    async def _deep_hist_walk_symbol(self, sym: str, tf: str, *, page_budget: Optional[int] = None) -> None:
        """One symbol/timeframe: page to the floor via the public segment API.

        ``page_budget`` caps how many pages this walk may take from UPSTREAM
        (a cache hit costs ~10ms of disk parse and is not counted — its
        ``fetched_at`` predates this walk). The archive to the floor does not
        fit the disk budget, so an uncapped walk re-extends the frontier
        every pass (measured 2026-11-15: ~3,700 Alpaca calls/hour at 40-86%
        of the box's one core); the budget turns each pass into bounded
        work. ``None`` keeps the old unlimited behavior (tests).
        """
        # The fresh window's oldest bar is the natural first cursor.
        entry = self._bar_cache.get(sym, tf)
        oldest = int(entry.bars[0].time) if entry and entry.bars else int(time.time())
        failures = 0
        empty_misses = 0  # consecutive "temporary nothing" answers (fetched_at == 0.0)
        fetches = 0  # pages answered by upstream, not cache (fetched_at newer than walk start)
        started_at = time.time()
        while self._running:
            if page_budget is not None and fetches >= page_budget:
                # Budget spent: yield the pass. The next pass resumes from
                # the disk chain — pages written so far are hits there.
                return
            bars, fetched, has_more = await asyncio.to_thread(
                self.get_historical_segment, sym, tf, oldest
            )
            if fetched > started_at:
                # A page stamped during this walk came from upstream (disk
                # hits carry the writing pass's timestamp). Track it against
                # the budget; pace only on real fetches.
                fetches += 1
            if not bars:
                # Two kinds of empty, per the 503 contract (routes.py):
                # fetched_at == 0.0 is "couldn't get it right now" — upstream
                # failure, cooldown window, single-flight the waiter did not
                # land. The old code counted it as a floor failure and gave
                # up after 3, leaving a hole mid-archive for hours (and the
                # outer except never fired because _fetch_hist_segment
                # converts upstream errors to this empty tuple — measured
                # 2026-11-14: an Alpaca 429 storm never reached the
                # "upstream-wide outage" park). A fetched_at > 0 empty IS the
                # terminal fact (floor or young symbol) and stops the walk.
                if not fetched:
                    empty_misses += 1
                    failures = 0
                    if empty_misses >= 5:
                        # Persistent upstream trouble, not a race: stop
                        # walking this symbol; the caller's outer loop and
                        # the next boot's disk resume will pick it up.
                        return
                    await asyncio.sleep(self.DEEP_HIST_RETRY_PAUSE)
                    continue
                empty_misses = 0
                failures += 1
                # An empty page at a cursor still inside Yahoo's 59d window
                # is the sliding-edge artifact, not the floor: Yahoo's oldest
                # bar moves with "now", so the page can answer empty today
                # and non-empty tomorrow. Jump the cursor past the ceiling so
                # the next page routes to the IEX archive instead of
                # stranding the walk at the seed edge.
                span_days = TIMEFRAME_SPAN_DAYS.get(tf)
                ceiling = int(time.time()) - span_days * 86400 if span_days else 0
                if oldest >= ceiling:
                    oldest = ceiling - 86400
                    failures = 0
                    await asyncio.sleep(self.DEEP_HIST_PAGE_PACE)
                    continue
                if failures >= 3 or not has_more:
                    # Three consecutive terminal empties with the cursor
                    # unmoved: the floor. Stop.
                    return
                await asyncio.sleep(5.0)
                continue
            empty_misses = 0
            failures = 0
            next_oldest = int(bars[0].time)
            if next_oldest >= oldest:
                # Cursor did not move backwards: no more data below.
                return
            oldest = next_oldest
            if not has_more:
                return
            await asyncio.sleep(self.DEEP_HIST_PAGE_PACE)

