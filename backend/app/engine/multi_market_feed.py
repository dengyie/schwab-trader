"""
Routes market-data reads to the feed that owns the symbol's market.

US symbols keep the Alpaca streaming service (unchanged behaviour); every other
venue is served by the Yahoo polling feed. The wrapper exposes the same surface
the API and the websocket handler already use, so no caller needs to know which
provider answered.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.markets import resolve_symbol
from app.models.trading import Bar, Quote, Trade

logger = logging.getLogger(__name__)


class MultiMarketFeed:
    # A feed probe that hangs must not hang the health endpoint: the deploy
    # gate gives up long before this, and a timed-out feed is itself the
    # signal we want to report.
    HEALTH_PROBE_TIMEOUT = 1.0

    def __init__(self, us_feed, intl_feed, us_market_code: str = "US"):
        self._us = us_feed
        self._intl = intl_feed
        self._us_code = us_market_code

    # -- routing ----------------------------------------------------------
    def _route(self, symbol: object):
        ref = resolve_symbol(symbol)
        if ref is None:
            return None, None
        if ref.market.code == self._us_code:
            return ref.symbol, self._us
        return ref.symbol, self._intl

    # -- quotes -----------------------------------------------------------
    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        canonical, feed = self._route(symbol)
        if feed is None:
            return None
        try:
            return feed.get_latest_quote(canonical)
        except Exception as exc:
            logger.warning("Quote lookup failed for %s: %s", canonical, exc)
            return None

    def get_bars(self, symbol: str, timeframe: str = "15m") -> List[Bar]:
        canonical, feed = self._route(symbol)
        if feed is None:
            return []
        try:
            if hasattr(feed, "get_bars"):
                return feed.get_bars(canonical, timeframe=timeframe)
            return feed.get_historical_bars(canonical)
        except Exception as exc:
            logger.warning("Bar lookup failed for %s (%s): %s", canonical, timeframe, exc)
            return []

    def get_bars_with_meta(self, symbol: str, timeframe: str = "15m") -> Tuple[List[Bar], float]:
        """Bars plus when they were fetched, for HTTP cache validators.

        Feeds that do not track a fetch time report 0.0, which the API turns
        into "always revalidate" instead of a bogus freshness claim.
        """
        canonical, feed = self._route(symbol)
        if feed is None:
            return [], 0.0
        try:
            if hasattr(feed, "get_bars_with_meta"):
                return feed.get_bars_with_meta(canonical, timeframe=timeframe)
            if hasattr(feed, "get_bars"):
                return feed.get_bars(canonical, timeframe=timeframe), 0.0
            return feed.get_historical_bars(canonical), 0.0
        except Exception as exc:
            logger.warning("Bar lookup failed for %s (%s): %s", canonical, timeframe, exc)
            return [], 0.0

    def get_historical_bars(self, symbol: str) -> List[Bar]:
        return self.get_bars(symbol, timeframe="1m")

    def ensure_daily_stats(
        self, market: Optional[str] = None, symbols: Optional[List[str]] = None
    ) -> None:
        """Warm the daily context for a whole market, at most one upstream call.

        ``symbols`` lets a caller warm exactly the rows it is about to serve
        (a watchlist can hold names outside the catalog); without it the market
        catalog is used.
        """
        feed = self._us if (market or self._us_code).upper() == self._us_code else self._intl
        warmer = getattr(feed, "ensure_daily_stats", None)
        if not callable(warmer):
            return
        try:
            warmer(list(symbols) if symbols else self._market_symbols(feed, market))
        except Exception as exc:
            logger.warning("Daily stats warm-up failed: %s", exc)

    def _market_symbols(self, feed, market: Optional[str]) -> Optional[List[str]]:
        if not market:
            return None
        try:
            from app.core.markets import MARKETS

            return list(MARKETS.require(market.upper()).catalog)
        except Exception:
            return None

    def get_daily_stats(self, symbol: str) -> Dict[str, float]:
        """Previous close and today's range/volume, when the provider has them."""
        canonical, feed = self._route(symbol)
        if feed is None:
            return {}
        getter = getattr(feed, "get_daily_stats", None)
        if not callable(getter):
            return {}
        try:
            return getter(canonical) or {}
        except Exception as exc:
            logger.warning("Daily stats lookup failed for %s: %s", canonical, exc)
            return {}

    # -- subscriptions ----------------------------------------------------
    def subscribe_quote(self, callback: Callable[[Quote], None]) -> None:
        self._us.subscribe_quote(callback)
        self._intl.subscribe_quote(callback)

    def subscribe_bar(self, callback: Callable[[str, Bar], None]) -> None:
        self._us.subscribe_bar(callback)
        self._intl.subscribe_bar(callback)

    def subscribe_trade(self, callback: Callable[[Trade], None]) -> None:
        if hasattr(self._us, "subscribe_trade"):
            self._us.subscribe_trade(callback)

    # -- watch (websocket subscriptions) ----------------------------------
    def activate(self, symbol: str) -> None:
        canonical, feed = self._route(symbol)
        if feed is not None and hasattr(feed, "activate"):
            feed.activate(canonical)

    def deactivate(self, symbol: str) -> None:
        canonical, feed = self._route(symbol)
        if feed is not None and hasattr(feed, "deactivate"):
            feed.deactivate(canonical)

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        await self._us.start()
        await self._intl.start()

    async def stop(self) -> None:
        for feed in (self._intl, self._us):
            try:
                await feed.stop()
            except Exception:
                pass

    # -- health -----------------------------------------------------------
    async def health(self) -> Dict[str, Any]:
        """Per-venue feed state, so a probe can name the degraded feed.

        A whole-app "ok" hides the two cases that matter operationally: the US
        stream being rejected while REST keeps prices moving, and the US feed
        itself failing to answer at all. The former already shows up as
        ``quota_exceeded`` / ``consecutive_failures``; here a feed that takes
        more than ``HEALTH_PROBE_TIMEOUT`` to reply, or that raises, is reported
        as degraded rather than silently dropped.
        """
        async def _probe(feed: object, method: str) -> Dict[str, Any]:
            getter = getattr(feed, method, None)
            if not callable(getter):
                return {"state": "unavailable",
                        "error": f"feed has no callable {method}()"}
            loop = asyncio.get_running_loop()
            task = loop.create_task(asyncio.to_thread(getter))
            try:
                done, _ = await asyncio.wait({task}, timeout=self.HEALTH_PROBE_TIMEOUT)
            finally:
                if not task.done():
                    task.cancel()
            if not done:
                return {"state": "unreachable", "connected": False,
                        "error": f"{method}() timed out after "
                                 f"{self.HEALTH_PROBE_TIMEOUT:.1f}s"}
            try:
                result = task.result()
            except Exception as exc:
                return {"state": "unreachable", "connected": False, "error": str(exc)}
            if not isinstance(result, dict):
                return {"state": "unreachable", "connected": False,
                        "error": f"{method}() returned {type(result).__name__}"}
            return result

        # Concurrent on purpose: the probes are independent, and serialised
        # they would add up to 2s of worst case against the route's own
        # HEALTH_BUDGET — a pair of slow probes would then wipe the whole
        # per-feed payload down to a single timeout error.
        us_state, intl_state = await asyncio.gather(
            _probe(self._us, "stream_health"),
            _probe(self._intl, "health"),
        )
        return {"us": us_state, "intl": intl_state}

    # -- attributes read by the API layer ---------------------------------
    @property
    def symbols(self) -> List[str]:
        return list(dict.fromkeys(list(self._us.symbols) + list(self._intl.symbols)))

    @property
    def latest_quotes(self) -> Dict[str, Quote]:
        merged: Dict[str, Quote] = dict(getattr(self._intl, "latest_quotes", {}))
        merged.update(getattr(self._us, "latest_quotes", {}))
        return merged

    @property
    def last_trade_prices(self) -> Dict[str, float]:
        merged: Dict[str, float] = dict(getattr(self._intl, "last_trade_prices", {}))
        merged.update(getattr(self._us, "last_trade_prices", {}))
        return merged

    @property
    def historical_bars(self) -> Dict[str, List[Bar]]:
        merged: Dict[str, List[Bar]] = dict(getattr(self._intl, "historical_bars", {}))
        merged.update(getattr(self._us, "historical_bars", {}))
        return merged
