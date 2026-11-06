"""
REST API Routes for Market Data, Portfolio State, Order Execution, and AI Model Context.
"""
from __future__ import annotations
import re
import time
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, field_validator

from app.models.trading import (
    OrderSide, OrderType, Order, Position, Portfolio, Quote, Bar
)
from app.engine.base_broker import BaseBroker
from app.adapters.model_exporter import ModelFeatureExporter
from app.core.auth import require_auth
from app.core.config import settings
from app.core.timeframes import TIMEFRAME_SECONDS

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class OrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None
    stop_price: Optional[float] = None

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


def _normalize_symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"Invalid symbol: {symbol!r}")
    return sym


def _normalize_timeframe(timeframe: str) -> str:
    tf = (timeframe or "").strip().lower()
    if tf not in TIMEFRAME_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported timeframe {timeframe!r}; supported: {sorted(TIMEFRAME_SECONDS)}",
        )
    return tf


def create_api_router(
    ledger_or_broker: Any,
    engine: Optional[Any] = None,
    feed: Any = None
) -> APIRouter:
    # Support both create_api_router(ledger, engine, feed) and create_api_router(broker, feed)
    if engine is not None and hasattr(engine, "submit_order"):
        broker: BaseBroker = engine
        actual_feed = feed
    elif hasattr(ledger_or_broker, "submit_order"):
        broker: BaseBroker = ledger_or_broker
        actual_feed = engine if feed is None else feed
    else:
        broker: BaseBroker = engine
        actual_feed = feed

    router = APIRouter(prefix="/api")

    _cached_tickers: List[Dict[str, Any]] = []
    _cached_tickers_ts: float = 0.0
    _tickers_ttl: float = 5.0

    @router.get("/health")
    def health_check():
        return {
            "status": "ok",
            "service": "schwab-trader-hub",
            "market_data_provider": settings.market_data_provider,
            "broker_provider": settings.broker_provider
        }

    @router.get("/auth/verify", dependencies=[Depends(require_auth)])
    def verify_token():
        """Token validity check used by the frontend login gate (401 if invalid)."""
        return {"ok": True}

    # Market Data Endpoints
    @router.get("/market/bars", dependencies=[Depends(require_auth)])
    def get_bars(symbol: str = "SPY", timeframe: str = "15m") -> List[Dict[str, Any]]:
        # Fetch multi-timeframe candles with in-memory TTL caching.
        # Never fabricate candles: an empty list tells the client data is unavailable.
        sym = _normalize_symbol(symbol)
        tf = _normalize_timeframe(timeframe)
        if hasattr(actual_feed, "get_bars"):
            bars = actual_feed.get_bars(sym, timeframe=tf)
        else:
            bars = actual_feed.get_historical_bars(sym)
        return ModelFeatureExporter.to_records(bars or [])

    @router.get("/market/tickers", dependencies=[Depends(require_auth)])
    def get_tickers() -> List[Dict[str, Any]]:
        nonlocal _cached_tickers, _cached_tickers_ts
        now = time.time()
        if _cached_tickers and (now - _cached_tickers_ts < _tickers_ttl):
            # Instant memory return with thread-safe copy and updated tick prices
            fresh_tickers: List[Dict[str, Any]] = []
            for item in _cached_tickers:
                sym = item["symbol"]
                q = getattr(actual_feed, "latest_quotes", {}).get(sym)
                last_p = getattr(actual_feed, "last_trade_prices", {}).get(sym)
                current_p = q.last if (q and q.last > 0) else (last_p if (last_p and last_p > 0) else item["price"])
                open_p = item.get("openPrice") or current_p
                copy_item = dict(item)
                copy_item["price"] = current_p
                copy_item["priceChange"] = round(current_p - open_p, 2)
                copy_item["priceChangePercent"] = round(((current_p - open_p) / open_p * 100.0) if open_p else 0.0, 2)
                fresh_tickers.append(copy_item)
            return fresh_tickers

        feed_symbols = getattr(actual_feed, "symbols", None)
        symbols = list(feed_symbols) if feed_symbols else []
        for s in ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "AMZN", "GOOGL", "META", "IWM", "COIN", "PLTR", "NFLX"]:
            if s not in symbols:
                symbols.append(s)

        tickers = []
        now_ts = int(time.time() * 1000)

        for sym in symbols:
            quote = actual_feed.get_latest_quote(sym)
            bars = getattr(actual_feed, "historical_bars", {}).get(sym) or []
            last_price = quote.last if (quote and quote.last > 0) else (bars[-1].close if bars else None)
            if not last_price:
                # No live quote and no historical bars: omit instead of inventing a price.
                continue
            open_price = bars[0].open if bars else last_price
            high_24h = max([b.high for b in bars], default=last_price)
            low_24h = min([b.low for b in bars], default=last_price)
            vol_24h = sum([b.volume for b in bars]) if bars else 0.0
            price_change = round(last_price - open_price, 2)
            pct_change = round((price_change / open_price * 100.0) if open_price else 0.0, 2)

            tickers.append({
                "symbol": sym,
                "price": last_price,
                "priceChange": price_change,
                "priceChangePercent": pct_change,
                "high24h": high_24h,
                "low24h": low_24h,
                "volume24h": vol_24h,
                "quoteVolume24h": round(vol_24h * last_price, 2),
                "openPrice": open_price,
                "trades24h": len(bars),
                "lastUpdated": now_ts
            })

        _cached_tickers = tickers
        _cached_tickers_ts = now
        return tickers

    @router.get("/market/quote", dependencies=[Depends(require_auth)])
    def get_quote(symbol: str = "SPY") -> Dict[str, Any]:
        sym = _normalize_symbol(symbol)
        quote = actual_feed.get_latest_quote(sym)
        if not quote:
            raise HTTPException(status_code=404, detail=f"Quote not available for {sym}")
        return quote.model_dump(mode="json")

    @router.get("/market/model-context", dependencies=[Depends(require_auth)])
    def get_model_context(symbol: str = "SPY") -> Dict[str, Any]:
        """Provides AI/Quant model ready market summary and technical indicators."""
        sym = _normalize_symbol(symbol)
        quote = actual_feed.get_latest_quote(sym)
        bars = actual_feed.get_historical_bars(sym)
        position = broker.get_position(sym)
        return ModelFeatureExporter.format_agent_market_context(sym, quote, bars, position)

    # Portfolio & Orders Endpoints
    @router.get("/portfolio", dependencies=[Depends(require_auth)])
    def get_portfolio() -> Dict[str, Any]:
        try:
            p = broker.get_portfolio()
        except Exception as exc:
            # Never fabricate an account balance when the broker is unreachable.
            raise HTTPException(status_code=503, detail=f"Broker unavailable: {exc}")
        return {
            "cash": p.cash,
            "initial_capital": p.initial_capital,
            "market_value": p.market_value,
            "total_equity": p.total_equity,
            "realized_pnl": p.realized_pnl,
            "unrealized_pnl": p.unrealized_pnl,
            "total_pnl": p.total_pnl,
            "total_pnl_pct": p.total_pnl_pct,
            "positions": {
                sym: {
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "avg_cost": pos.avg_cost,
                    "current_price": pos.current_price,
                    "market_value": pos.market_value,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "unrealized_pnl_pct": pos.unrealized_pnl_pct
                }
                for sym, pos in p.positions.items()
                if pos.quantity > 0
            }
        }

    @router.post("/portfolio/reset", dependencies=[Depends(require_auth)])
    def reset_portfolio(req: ResetRequest):
        broker.reset(req.initial_capital)
        return {"status": "reset", "initial_capital": req.initial_capital}

    @router.get("/orders", dependencies=[Depends(require_auth)])
    def list_orders() -> List[Dict[str, Any]]:
        orders = broker.get_orders()
        return [ord.model_dump(mode="json") for ord in orders]

    @router.post("/orders", dependencies=[Depends(require_auth)])
    def submit_order(req: OrderRequest) -> Dict[str, Any]:
        try:
            # Sync latest quote before submitting
            quote = actual_feed.get_latest_quote(req.symbol)
            if quote:
                broker.on_quote(quote)

            order = broker.submit_order(
                symbol=req.symbol,
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
