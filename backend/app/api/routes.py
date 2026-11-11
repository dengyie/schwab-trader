"""
REST API Routes for Market Data, Portfolio State, Order Execution, and AI Model Context.

Every market-data and order endpoint accepts symbols from any supported market
(US / HK / JP / CN); canonicalisation and rule enforcement live in
``app.core.markets`` so the HTTP layer stays thin.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import inspect
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, field_validator

from app.models.trading import (
    OrderSide, OrderType, Order, Position, Portfolio, Quote, Bar
)
from app.engine.base_broker import BaseBroker
from app.adapters.model_exporter import ModelFeatureExporter
from app.core.auth import require_auth
from app.core.config import settings
from app.core.markets import (
    MARKETS,
    market_clock,
    market_of,
    market_payload,
    resolve_symbol,
    within_day_range,
)
from app.core.timeframes import TIMEFRAME_SECONDS, TIMEFRAME_SPAN_DAYS, TIMEFRAME_TTLS

# Outer bound on the feed probes behind /api/health. Each feed already caps
# itself (MultiMarketFeed.HEALTH_PROBE_TIMEOUT); this covers a feed that
# ignores its own budget, and stays under the deploy gate's curl timeout.
HEALTH_BUDGET = 2.0
# /api/health is unauthenticated (the deploy gate probes it without a token).
# Upstream error text is operationally useful there but is a free-form string;
# cap it so a future verbose exception cannot leak internal detail verbatim.
HEALTH_ERROR_CLIP = 120


def _tickers_response(
    response: Response, market: str, rows: List[Dict[str, Any]], if_none_match: Optional[str]
) -> Any:
    """ETag over the rendered rows; a 304 only when the body truly matches.

    The hash is over the response itself because the cached path overlays live
    quotes per request - a timestamp-based validator would promise "unchanged"
    while prices moved.
    """
    body = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    etag = f'W/"tickers-{market}-{hashlib.sha1(body.encode()).hexdigest()[:16]}"'
    headers = {
        "ETag": etag,
        # Revalidate every poll: prices tick while a session is open, so the
        # browser must ask each time and let the ETag decide.
        "Cache-Control": "private, no-cache",
    }
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return rows


def _clip_error(value: Any) -> Any:
    if isinstance(value, str) and len(value) > HEALTH_ERROR_CLIP:
        return value[: HEALTH_ERROR_CLIP - 3] + "..."
    return value


class OrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None
    stop_price: Optional[float] = None
    # Optional hint for bare numeric codes (e.g. "0700" + market "HK").
    market: Optional[str] = None

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, v):
        if isinstance(v, str):
            return v.upper()
        return v

    @field_validator("order_type", mode="before")
    @classmethod
    def normalize_order_type(cls, v):
        if isinstance(v, str):
            v_upper = v.upper()
            if "STOP" in v_upper:
                return OrderType.STOP
            if "LIMIT" in v_upper:
                return OrderType.LIMIT
            return OrderType.MARKET
        return v


class ResetRequest(BaseModel):
    initial_capital: float = 100000.0


def _normalize_symbol(symbol: str, market: Optional[str] = None) -> str:
    ref = resolve_symbol(symbol, market)
    if ref is None:
        raise HTTPException(status_code=400, detail=f"Invalid symbol: {symbol!r}")
    return ref.symbol


def _normalize_timeframe(timeframe: str) -> str:
    tf = (timeframe or "").strip().lower()
    if tf not in TIMEFRAME_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported timeframe {timeframe!r}; supported: {sorted(TIMEFRAME_SECONDS)}",
        )
    return tf


def _normalize_before(before: Optional[int], timeframe: str) -> Optional[int]:
    """Validate the historical-segment cursor against the timeframe's span.

    Daily and coarser buckets are fetched whole (see TIMEFRAME_SPAN_DAYS), so
    a cursor there is a client bug and is rejected outright instead of being
    silently served a truncated window.
    """
    if before is None:
        return None
    if before <= 0:
        raise HTTPException(status_code=400, detail=f"Invalid before cursor: {before!r}")
    if TIMEFRAME_SPAN_DAYS.get(timeframe) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Timeframe {timeframe!r} is served whole; paging is not offered",
        )
    return int(before)


def _normalize_market(code: Optional[str]) -> str:
    market = MARKETS.get(code or "US")
    if market is None:
        raise HTTPException(status_code=400, detail=f"Unknown market: {code!r}")
    return market.code


def _etag_matches(if_none_match: Optional[str], etag: str) -> bool:
    """Whether a conditional request can be answered with 304.

    ``If-None-Match`` is a comma-separated list that may use weak validators
    (``W/"..."``); RFC 9110 says to compare the opaque tag, ignoring the
    weakness marker, so ``W/"x"`` and ``"x"`` match.
    """
    if not if_none_match:
        return False
    def opaque(value: str) -> str:
        value = value.strip()
        if value.startswith("W/"):
            value = value[2:]
        return value.strip('"')

    candidate = opaque(etag)
    return any(
        part.strip() == "*" or opaque(part) == candidate
        for part in if_none_match.split(",")
    )


# Day-range guard lives in app.core.markets so the REST rows and the position
# marks cannot drift apart.


def _pick_day_price(
    quote: Any,
    daily: Dict[str, float],
    bars: List[Any],
    low: Optional[float],
    high: Optional[float],
) -> Optional[float]:
    """Latest price for a watchlist row, ignoring quotes outside the day range.

    Alpaca's IEX quote for a thin moment can be a lone stale bid, which would
    otherwise render a price (and a day change) that contradicts the session
    range. Preference order is live quote -> snapshot's last trade -> last bar.
    """
    candidates: List[float] = []
    if quote is not None and getattr(quote, "last", 0) > 0:
        candidates.append(float(quote.last))
    if daily.get("last"):
        candidates.append(float(daily["last"]))
    if bars:
        candidates.append(float(bars[-1].close))
    if not candidates:
        return None
    for candidate in candidates:
        if within_day_range(candidate, low, high):
            return candidate
    return None


def _position_payload(pos: Position, fx_rates: Dict[str, float]) -> Dict[str, Any]:
    rate = fx_rates.get(pos.currency, 1.0)
    return {
        "symbol": pos.symbol,
        "market": pos.market,
        "currency": pos.currency,
        "quantity": pos.quantity,
        "avg_cost": pos.avg_cost,
        "current_price": pos.current_price,
        # Native-currency figures: what the position is worth on its own venue.
        "market_value": pos.market_value,
        "unrealized_pnl": pos.unrealized_pnl,
        "unrealized_pnl_pct": pos.unrealized_pnl_pct,
        "realized_pnl": pos.realized_pnl,
        # Base-currency (USD) figures used for account totals.
        "market_value_usd": pos.market_value_usd(rate),
        "unrealized_pnl_usd": pos.unrealized_pnl_usd(rate),
        "realized_pnl_usd": pos.realized_pnl_usd,
        "cost_basis_usd": pos.cost_basis_usd,
        "bought_on": pos.bought_on,
    }


def create_api_router(broker: BaseBroker, feed: Any) -> APIRouter:
    actual_feed = feed

    router = APIRouter(prefix="/api")

    _tickers_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _tickers_ttl = 5.0

    def _fx_rates() -> Dict[str, float]:
        getter = getattr(broker, "get_fx_rates", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return {"USD": 1.0}

    @router.get("/health")
    async def health_check():
        # Async on purpose: the deploy health gate must be answered by the
        # event loop, never queued behind threadpool work, so a busy feed
        # cannot turn a liveness probe into a timeout.
        payload: Dict[str, Any] = {
            "status": "ok",
            "service": "schwab-trader-hub",
            "market_data_provider": settings.market_data_provider,
            "broker_provider": settings.broker_provider,
            "markets": MARKETS.codes(),
        }
        # Liveness stays "ok": the process serves REST and the deploy gate must
        # not flap on it. A rejected upstream stream is reported next to it,
        # because that failure is otherwise invisible (no ticks looks the same
        # as a quiet market).
        feeds = getattr(actual_feed, "health", None)
        if callable(feeds):
            try:
                feed_health = feeds()
                if inspect.isawaitable(feed_health):
                    # The feed probes upstream state; a hung probe must not
                    # hang liveness longer than its own budget.
                    feed_health = await asyncio.wait_for(feed_health, timeout=HEALTH_BUDGET)
            except Exception as exc:
                feed_health = {"error": str(exc)}
            if isinstance(feed_health, dict):
                if "error" in feed_health:
                    feed_health["error"] = _clip_error(feed_health["error"])
                for entry in feed_health.values():
                    if isinstance(entry, dict):
                        if "last_error" in entry:
                            entry["last_error"] = _clip_error(entry["last_error"])
                        if "error" in entry:
                            entry["error"] = _clip_error(entry["error"])
            payload["feeds"] = feed_health
            if isinstance(feed_health, dict) and any(
                isinstance(entry, dict) and entry.get("quota_exceeded")
                for entry in feed_health.values()
            ):
                payload["degraded"] = ["market_stream_quota_exceeded"]
        return payload

    @router.get("/auth/verify", dependencies=[Depends(require_auth)])
    def verify_token():
        """Token validity check used by the frontend login gate (401 if invalid)."""
        return {"ok": True}

    # Market catalog ------------------------------------------------------
    @router.get("/markets", dependencies=[Depends(require_auth)])
    def list_markets() -> Dict[str, Any]:
        """Every supported market with its rules and current session state."""
        return {
            "base_currency": "USD",
            "fx_rates": _fx_rates(),
            "markets": [
                {**market_payload(market), "clock": market_clock(market)}
                for market in MARKETS.all()
            ],
        }

    # Market Data Endpoints ----------------------------------------------
    @router.get("/market/bars", dependencies=[Depends(require_auth)])
    def get_bars(
        response: Response,
        symbol: str = "SPY",
        timeframe: str = "15m",
        market: Optional[str] = None,
        before: Optional[int] = None,
        if_none_match: Optional[str] = Header(None),
    ) -> Any:
        # The feed serves whatever it has and refreshes in the background, so
        # this is fast even when the data is older than its TTL; the headers
        # tell the client how fresh it actually is.
        # Never fabricate candles: an empty list tells the client data is unavailable.
        sym = _normalize_symbol(symbol, market)
        tf = _normalize_timeframe(timeframe)
        cursor = _normalize_before(before, tf)

        if cursor is not None:
            # Historical segment: immutable data strictly older than the
            # cursor. Never refreshed on the hot path; failure == empty.
            bars, fetched_at, has_more = actual_feed.get_historical_segment(sym, timeframe=tf, before=cursor)
            records = ModelFeatureExporter.to_records(bars or [])
            etag = f'W/"{sym}-{tf}-h{cursor}-{int(fetched_at)}"'
            # History does not age: browsers keep the segment for a day and
            # the CDN edge for a week; the ETag still turns a revalidation
            # into a 304. Fresh-window headers below are untouched.
            # Cache-contract caveats:
            # - ``public``/``s-maxage`` invites a shared cache to store these
            #   responses even though the route sits behind require_auth.
            #   Verified safe in production (2026-11-11): the payload is
            #   user-identical public market data and the CF cache rule matches
            #   only GET /api/market/bars?...before=... — a URL-keyed request
            #   whose authorization header is NOT part of CF's cache key, so
            #   a cached segment can never cross users (there is nothing
            #   per-user in it). Fresh-window responses stay ``private`` and
            #   cache as DYNAMIC. If this endpoint ever gains a per-user
            #   dimension, drop ``public`` first.
            # - ``X-Bars-Oldest`` is an ops/debug contract: the frontend drives
            #   paging off ``X-Bars-Has-More`` and does not read this header.
            response.headers["ETag"] = etag
            response.headers["X-Bars-Has-More"] = "1" if has_more else "0"
            response.headers["X-Bars-Oldest"] = str(bars[0].time if bars else 0)
            response.headers["Cache-Control"] = (
                "public, max-age=86400, s-maxage=604800, stale-while-revalidate=86400"
            )
            if _etag_matches(if_none_match, etag):
                return Response(status_code=304, headers=dict(response.headers))
            return records

        if hasattr(actual_feed, "get_bars_with_meta"):
            bars, fetched_at = actual_feed.get_bars_with_meta(sym, timeframe=tf)
        elif hasattr(actual_feed, "get_bars"):
            bars, fetched_at = actual_feed.get_bars(sym, timeframe=tf), 0.0
        else:
            bars, fetched_at = actual_feed.get_historical_bars(sym), 0.0
        records = ModelFeatureExporter.to_records(bars or [])

        if not fetched_at:
            # Nothing cached behind this response (or a feed that does not
            # track fetch times): the client must ask again next time.
            response.headers["X-Cache"] = "MISS"
            response.headers["Cache-Control"] = "no-store"
            return records

        ttl = TIMEFRAME_TTLS.get(tf, 60.0)
        age = max(0.0, time.time() - fetched_at)
        remaining = max(0, int(ttl - age))
        # Weak validator: two renders of the same fetch are equivalent, and the
        # client only needs to know whether the data changed.
        etag = f'W/"{sym}-{tf}-{int(fetched_at)}"'
        headers = {
            "ETag": etag,
            "Age": str(int(age)),
            "X-Cache": "HIT" if age < ttl else "STALE",
            # Browsers and Caddy may reuse it for the rest of its TTL; after
            # that they revalidate, and the ETag turns an unchanged fetch into
            # a 304 instead of a 200 carrying 200KB of identical candles.
            "Cache-Control": f"private, max-age={remaining}",
        }
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        response.headers.update(headers)
        return records

    @router.get("/market/tickers", dependencies=[Depends(require_auth)])
    def get_tickers(
        response: Response,
        market: str = "US",
        if_none_match: Optional[str] = Header(None),
    ) -> Any:
        """Catalog quotes for one market, with a short shared cache.

        The ETag hashes the rendered rows, not the cache timestamp: the cached
        path overlays live quotes per request, so only a genuinely identical
        body earns a 304. ``Cache-Control: no-cache`` keeps every poll
        revalidating instead of trusting a stale copy.
        """
        code = _normalize_market(market)
        market_def = MARKETS.require(code)

        cached = _tickers_cache.get(code)
        if cached and (time.time() - cached[0] < _tickers_ttl):
            fresh: List[Dict[str, Any]] = []
            for item in cached[1]:
                sym = item["symbol"]
                q = actual_feed.get_latest_quote(sym)
                current_p = q.last if (q and q.last > 0) else item["price"]
                # A stale bid-only quote must not drag the row outside the range.
                if not within_day_range(current_p, item.get("low24h"), item.get("high24h")):
                    current_p = item["price"]
                prev = item.get("openPrice") or current_p
                copy_item = dict(item)
                copy_item["price"] = current_p
                copy_item["priceChange"] = round(current_p - prev, 6)
                copy_item["priceChangePercent"] = round(
                    ((current_p - prev) / prev * 100.0) if prev else 0.0, 2
                )
                # The row's timestamp is the print's own time, never "now":
                # rewriting it to the request time is what made an hours-old
                # close look freshly updated.
                copy_item["lastUpdated"] = (
                    int(q.timestamp.timestamp() * 1000)
                    if q is not None
                    else int(item.get("lastUpdated") or 0)
                )
                copy_item["isStale"] = bool(q.is_stale) if q is not None else False
                fresh.append(copy_item)
            return _tickers_response(response, code, fresh, if_none_match)

        feed_symbols = getattr(actual_feed, "symbols", None) or []
        symbols = list(market_def.catalog)
        for sym in feed_symbols:
            if market_of(sym).code == code and sym not in symbols:
                symbols.append(sym)

        tickers: List[Dict[str, Any]] = []
        now_ts = int(time.time() * 1000)

        # One batched warm-up for the whole market, not one call per row.
        # It must be the catalog *as this request enumerates it*: the tickers
        # for a market read their daily context from ``daily_stats``, and a
        # symbol that is not warmed keeps ``get_daily_stats`` empty.
        warmer = getattr(actual_feed, "ensure_daily_stats", None)
        if callable(warmer):
            try:
                warmer(code, symbols)
            except Exception:
                pass

        for sym in symbols:
            quote = actual_feed.get_latest_quote(sym)
            daily = actual_feed.get_daily_stats(sym)
            bars = getattr(actual_feed, "historical_bars", {}).get(sym) or []

            high = (
                daily.get("day_high")
                or (quote.day_high if quote and quote.day_high else None)
                or (max([b.high for b in bars]) if bars else None)
            )
            low = (
                daily.get("day_low")
                or (quote.day_low if quote and quote.day_low else None)
                or (min([b.low for b in bars]) if bars else None)
            )

            last_price = _pick_day_price(quote, daily, bars, low, high)
            if last_price is None:
                # No trustworthy price: omit instead of inventing one.
                continue
            high = high or last_price
            low = low or last_price
            # Day change is measured against the previous close, which is what
            # a watchlist row is expected to show.
            open_price = (
                (quote.prev_close if (quote and quote.prev_close) else None)
                or daily.get("prev_close")
                or daily.get("day_open")
                or (bars[0].open if bars else last_price)
            )
            volume = (
                (daily.get("volume") if daily.get("volume") else 0)
                or (quote.volume if quote else 0)
                or (sum([b.volume for b in bars]) if bars else 0.0)
            )
            price_change = last_price - open_price
            pct_change = (price_change / open_price * 100.0) if open_price else 0.0

            tickers.append({
                "symbol": sym,
                "market": code,
                "currency": quote.currency if quote else market_def.currency,
                "price": last_price,
                "priceChange": round(price_change, 6),
                "priceChangePercent": round(pct_change, 2),
                "high24h": high,
                "low24h": low,
                "volume24h": volume,
                "quoteVolume24h": round(volume * last_price, 2),
                "openPrice": open_price,
                "trades24h": len(bars),
                "lastUpdated": (
                    int(quote.timestamp.timestamp() * 1000)
                    if quote is not None
                    else (int(bars[-1].time * 1000) if bars else now_ts)
                ),
                "isStale": bool(quote.is_stale) if quote is not None else False,
            })

        _tickers_cache[code] = (time.time(), tickers)
        return _tickers_response(response, code, tickers, if_none_match)

    @router.get("/market/quote", dependencies=[Depends(require_auth)])
    def get_quote(symbol: str = "SPY", market: Optional[str] = None) -> Dict[str, Any]:
        sym = _normalize_symbol(symbol, market)
        quote = actual_feed.get_latest_quote(sym)
        if not quote:
            raise HTTPException(status_code=404, detail=f"Quote not available for {sym}")
        return quote.model_dump(mode="json")

    @router.get("/market/clock", dependencies=[Depends(require_auth)])
    def get_market_clock(
        market: Optional[str] = None, symbol: Optional[str] = None
    ) -> Dict[str, Any]:
        """Trading-session clock so the UI can tell 'market closed' from 'feed broken'."""
        code = market
        if symbol and not code:
            ref = resolve_symbol(symbol)
            code = ref.market.code if ref else None
        clock = broker.get_market_clock(_normalize_market(code))
        if clock is None:
            raise HTTPException(status_code=503, detail="Broker clock unavailable")
        return clock

    @router.get("/market/model-context", dependencies=[Depends(require_auth)])
    def get_model_context(symbol: str = "SPY", market: Optional[str] = None) -> Dict[str, Any]:
        """Provides AI/Quant model ready market summary and technical indicators."""
        sym = _normalize_symbol(symbol, market)
        quote = actual_feed.get_latest_quote(sym)
        bars = actual_feed.get_historical_bars(sym)
        position = broker.get_position(sym)
        return ModelFeatureExporter.format_agent_market_context(sym, quote, bars, position)

    # Portfolio & Orders Endpoints ---------------------------------------
    @router.get("/portfolio", dependencies=[Depends(require_auth)])
    def get_portfolio() -> Dict[str, Any]:
        try:
            p = broker.get_portfolio()
        except Exception as exc:
            # Never fabricate an account balance when the broker is unreachable.
            raise HTTPException(status_code=503, detail=f"Broker unavailable: {exc}")
        fx_rates = p.fx_rates or {"USD": 1.0}
        return {
            "base_currency": "USD",
            "cash": p.cash,
            "initial_capital": p.initial_capital,
            "market_value": p.market_value,
            "total_equity": p.total_equity,
            "realized_pnl": p.realized_pnl,
            "unrealized_pnl": p.unrealized_pnl,
            "total_pnl": p.total_pnl,
            "total_pnl_pct": p.total_pnl_pct,
            "fx_rates": fx_rates,
            "positions": {
                sym: _position_payload(pos, fx_rates)
                for sym, pos in p.positions.items()
                if pos.quantity > 0
            },
        }

    @router.post("/portfolio/reset", dependencies=[Depends(require_auth)])
    def reset_portfolio(req: ResetRequest):
        broker.reset(req.initial_capital)
        return {
            "status": "reset",
            "initial_capital": req.initial_capital,
            "base_currency": "USD",
        }

    @router.get("/portfolio/history", dependencies=[Depends(require_auth)])
    def get_portfolio_history(period: str = "1M", timeframe: str = "1D") -> Dict[str, Any]:
        # None means "no history at the venue" (fresh account, or Alpaca down
        # and nothing cached): the client shows its empty state, never zeros.
        history = broker.get_equity_history(period=period, timeframe=timeframe)
        if not history:
            raise HTTPException(status_code=404, detail="equity history unavailable")
        return {"base_currency": "USD", **history}

    @router.get("/orders", dependencies=[Depends(require_auth)])
    def list_orders() -> List[Dict[str, Any]]:
        orders = broker.get_orders()
        return [ord.model_dump(mode="json") for ord in orders]

    @router.post("/orders", dependencies=[Depends(require_auth)])
    def submit_order(req: OrderRequest) -> Dict[str, Any]:
        sym = _normalize_symbol(req.symbol, req.market)
        try:
            # Sync latest quote before submitting
            quote = actual_feed.get_latest_quote(sym)
            if quote:
                broker.on_quote(quote)

            order = broker.submit_order(
                symbol=sym,
                side=req.side,
                order_type=req.order_type,
                quantity=req.quantity,
                price=req.price,
                stop_price=req.stop_price
            )
            return order.model_dump(mode="json")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.delete("/orders/{order_id}", dependencies=[Depends(require_auth)])
    def cancel_order(order_id: str) -> Dict[str, Any]:
        order = broker.cancel_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return order.model_dump(mode="json")

    return router
