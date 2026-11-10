"""
FastAPI Main Application Entrypoint for Schwab & Alpaca Trader Hub.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.auth import token_matches
from app.core.markets import MARKETS, resolve_symbol
from app.models.trading import (
    Quote, Bar, Trade, Order, OrderSide, OrderType
)
from app.engine.portfolio_ledger import PortfolioLedger
from app.engine.matching_engine import VirtualMatchingEngine
from app.engine.state_store import JsonStateStore, resolve_state_dir
from app.engine.market_feed import MarketFeedService
from app.engine.alpaca_market_feed import AlpacaMarketFeedService
from app.engine.alpaca_broker import AlpacaPaperBroker
from app.engine.base_broker import BaseBroker
from app.engine.fx_service import FxService
from app.engine.yahoo_feed import YahooQuoteFeed
from app.engine.multi_market_feed import MultiMarketFeed
from app.engine.market_router import MarketRouterBroker
from app.api.websocket_manager import WebSocketManager
from app.api.routes import create_api_router

LOG_FORMAT = "%(levelname)s [%(name)s] %(message)s"


class _RedactWsQueryFilter(logging.Filter):
    """Keep the legacy ``?token=`` out of uvicorn's access records.

    uvicorn logs each WebSocket handshake from the protocol layer, which holds
    its own copy of the scope: no ASGI middleware can see that call. The only
    interception point that works is the LogRecord itself, so this filter
    strips the query string from the request path before a handler formats it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args or "WebSocket" not in str(record.msg):
            return True
        args = tuple(record.args)
        if len(args) < 2 or not isinstance(args[1], str):
            return True
        path = args[1]
        clean = path.split("?", 1)[0]
        if clean != path:
            record.args = (args[0], clean) + args[2:]
        return True


def _configure_logging() -> None:
    """Make app loggers reach journald.

    uvicorn only configures its own ``uvicorn.*`` loggers, so the root logger
    keeps no handler and stdlib emits nothing below WARNING: every
    ``logger.info`` in this app was silently dropped in production.

    Called at import time so the virtual-book restore (which runs while this
    module is built) is visible, and again from the lifespan; it is idempotent.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)

    # uvicorn logs WebSocket handshakes from its protocol layer, so the app
    # scope cannot be scrubbed; filter the record instead.
    uvicorn_error = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _RedactWsQueryFilter) for f in uvicorn_error.filters):
        uvicorn_error.addFilter(_RedactWsQueryFilter())


_configure_logging()

# 1. Market Data Provider Factory
if settings.market_data_provider == "alpaca":
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_API_SECRET must be set via environment."
        )
    us_feed = AlpacaMarketFeedService(
        initial_symbols=settings.default_symbols,
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        stream_url=settings.alpaca_data_stream_url,
        data_base_url=settings.alpaca_data_base_url,
        feed=settings.alpaca_feed
    )
else:
    us_feed = MarketFeedService(initial_symbols=settings.default_symbols)

# Non-US venues (HK / JP / CN) are served by Yahoo; the US stream is untouched.
fx = FxService(enabled=settings.fx_enabled)
intl_feed = YahooQuoteFeed(symbols=[], enabled=settings.intl_market_data_enabled)
feed = MultiMarketFeed(us_feed, intl_feed)

# 2. Broker / Execution Provider Factory
ledger = PortfolioLedger(initial_capital=settings.initial_capital, fx=fx)
# The virtual book must outlive a deploy: US positions live at Alpaca, but
# HK / JP / CN are matched in-process and would otherwise be wiped on restart.
state_dir = (
    resolve_state_dir(settings.state_dir) if settings.state_persist_enabled else None
)
state_store = JsonStateStore(state_dir) if state_dir else None
if state_dir:
    logging.getLogger("app.main").info("Virtual book state directory: %s", state_dir)
virtual_engine = VirtualMatchingEngine(
    ledger=ledger,
    default_slippage_bps=settings.default_slippage_bps,
    default_commission_per_order=settings.default_commission,
    fx=fx,
    enforce_sessions_for=frozenset({"HK", "JP", "CN"}),
    state_store=state_store,
)

if settings.broker_provider == "alpaca_paper":
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_API_SECRET must be set via environment."
        )
    us_broker: BaseBroker = AlpacaPaperBroker(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        base_url=settings.alpaca_paper_base_url
    )
else:
    us_broker = virtual_engine

# One account view across Alpaca (US) and the multi-market virtual engine.
broker: BaseBroker = MarketRouterBroker(us_broker, virtual_engine)

ws_manager = WebSocketManager()

# Chart prewarming is only useful while somebody is actually looking at a
# chart. Hand the feed a presence check instead of letting it re-parse the
# same multi-thousand-bar payloads for an empty server.
if hasattr(us_feed, "subscriber_probe"):
    us_feed.subscriber_probe = ws_manager.has_subscribers

logger = logging.getLogger("app.main")

# Used to schedule coroutines from worker threads (e.g. broker fill polling)
# back onto the main event loop in a thread-safe way.
_main_loop: asyncio.AbstractEventLoop | None = None


def _schedule_coro(coro) -> None:
    loop = _main_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)


def on_feed_quote(quote: Quote) -> None:
    # Feed quote into execution broker & mark-to-market
    broker.on_quote(quote)
    # Nobody subscribed (or only unauthenticated sockets): skip serialising and
    # scheduling a broadcast that would be dropped anyway.
    if not ws_manager.has_subscribers():
        return
    _schedule_coro(ws_manager.broadcast_quote(quote.model_dump(mode="json")))


def on_feed_bar(symbol: str, bar: Bar) -> None:
    if not ws_manager.has_subscribers():
        return
    _schedule_coro(ws_manager.broadcast_bar(symbol, bar.model_dump(mode="json")))


def on_feed_trade(trade: Trade) -> None:
    if not ws_manager.has_subscribers():
        return
    _schedule_coro(ws_manager.broadcast_trade(trade.model_dump(mode="json")))


async def _broadcast_order_filled(trade: Trade, order: Order) -> None:
    try:
        p = await asyncio.to_thread(broker.get_portfolio)
    except Exception as exc:
        logger.warning("Order filled but portfolio refresh failed: %s", exc)
        p = None
    payload = {
        "type": "ORDER_FILLED",
        "trade": trade.model_dump(mode="json"),
        "order": order.model_dump(mode="json"),
    }
    if p is not None:
        payload["portfolio"] = {
            "cash": p.cash,
            "total_equity": p.total_equity,
            "market_value": p.market_value,
            "unrealized_pnl": p.unrealized_pnl,
            "realized_pnl": p.realized_pnl,
        }
    # A failed refresh omits the snapshot instead of broadcasting zeros: the
    # client treats holdings as unknown, never as an emptied account (the REST
    # route answers 503 for the same reason). The client refetches on every
    # fill anyway, so the next snapshot it can read is the authoritative one.
    await ws_manager.broadcast(payload)


def on_order_fill(trade: Trade, order: Order) -> None:
    _schedule_coro(_broadcast_order_filled(trade, order))


# Register callbacks
feed.subscribe_quote(on_feed_quote)
feed.subscribe_bar(on_feed_bar)
feed.subscribe_trade(on_feed_trade)
broker.register_fill_listener(on_order_fill)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _configure_logging()
    _main_loop = asyncio.get_running_loop()
    # Startup: Launch market feed loop
    await feed.start()
    yield
    # Shutdown: Stop feed loop
    await feed.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan
)

# CORS: exact origin allow-list (no wildcard + credentials combination).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount REST API
app.include_router(create_api_router(broker, feed))


WS_AUTH_TIMEOUT_SEC = 5.0


def _clean_symbol(value: object, market: object = None) -> str | None:
    """Only real ticker shapes may reach the feed or broker from a socket.

    Accepts every supported market: ``SPY``, ``0700.HK``, ``7203.T``,
    ``600519.SS``; ``market`` disambiguates a bare numeric code.
    """
    ref = resolve_symbol(value, market if isinstance(market, str) else None)
    return ref.symbol if ref else None


async def _send_portfolio_snapshot(websocket: WebSocket) -> None:
    """Send the initial snapshot; a broker hiccup must not tear the socket down."""
    try:
        p = await asyncio.to_thread(broker.get_portfolio)
        await websocket.send_json({
            "type": "PORTFOLIO_SNAPSHOT",
            "data": {
                "cash": p.cash,
                "total_equity": p.total_equity,
                "unrealized_pnl": p.unrealized_pnl,
                "realized_pnl": p.realized_pnl,
                "positions": {
                    sym: {
                        "symbol": pos.symbol,
                        "quantity": pos.quantity,
                        "avg_cost": pos.avg_cost,
                        "current_price": pos.current_price,
                        "unrealized_pnl": pos.unrealized_pnl,
                        "realized_pnl": pos.realized_pnl,
                        # Multi-market context: the position table formats
                        # native-currency amounts from these two fields.
                        "market": pos.market,
                        "currency": pos.currency,
                    }
                    for sym, pos in p.positions.items()
                    if pos.quantity > 0
                }
            }
        })
    except Exception as exc:
        logger.warning("Initial portfolio snapshot unavailable: %s", exc)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Authenticate before streaming.

    A browser worker can only pass the token in the URL (it cannot set headers
    on a WebSocket), and that URL lands verbatim in the uvicorn access log.
    Clients therefore authenticate with a first ``{"action": "AUTH",
    "token": ...}`` frame; the legacy ``?token=`` query parameter is still
    honored so a tab running an older bundle keeps working across a deploy.
    Both paths use the same constant-time comparison.
    """
    try:
        await ws_manager.connect(websocket)
    except Exception:
        # accept() itself failed; nothing was registered, so nothing to clean up.
        return

    query_token = websocket.query_params.get("token")
    authenticated = bool(query_token) and token_matches(query_token)
    if query_token and not authenticated:
        logger.warning("WebSocket rejected: invalid query token")
        await ws_manager.close(websocket, code=4401, reason="unauthorized")
        return

    auth_via = "query"
    if not authenticated:
        try:
            first = json.loads(
                await asyncio.wait_for(
                    websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SEC
                )
            )
        except Exception:
            first = None
        if not (
            isinstance(first, dict)
            and first.get("action") == "AUTH"
            and token_matches(first.get("token"))
        ):
            logger.warning("WebSocket rejected: missing or invalid AUTH frame")
            await ws_manager.close(websocket, code=4401, reason="unauthorized")
            return
        auth_via = "frame"

    logger.info(
        "WebSocket connected (auth via %s, clients=%d)",
        auth_via,
        len(ws_manager.active_connections),
    )
    # Only now may this socket receive a single byte.
    ws_manager.mark_authenticated(websocket)
    await _send_portfolio_snapshot(websocket)

    try:
        while True:
            text = await websocket.receive_text()
            try:
                msg = json.loads(text)
            except Exception:
                continue
            # Valid JSON is not necessarily an object; a bare list/string would
            # otherwise raise on msg.get() and leak the connection.
            if not isinstance(msg, dict):
                continue

            action = msg.get("action")
            if action == "AUTH":
                # Already authenticated to get here; a repeat frame (or a
                # handshake racing the snapshot) is an ack, not an error.
                await websocket.send_json({"type": "AUTH_OK"})
                continue

            if action == "SUBSCRIBE":
                sym = _clean_symbol(msg.get("symbol", "SPY"), msg.get("market"))
                if not sym:
                    await websocket.send_json({
                        "type": "SUBSCRIBE_ERROR", "error": "invalid symbol"
                    })
                    continue
                ws_manager.subscribe(websocket, sym)
                # Keep the polling feed awake for this specific symbol.
                feed.activate(sym)
                # Send immediate latest quote (off the event loop)
                q = await asyncio.to_thread(feed.get_latest_quote, sym)
                if q:
                    await websocket.send_json({"type": "QUOTE_UPDATE", "data": q.model_dump(mode="json")})

            elif action == "UNSUBSCRIBE":
                sym = _clean_symbol(msg.get("symbol"), msg.get("market"))
                if sym:
                    ws_manager.unsubscribe(websocket, sym)
                    feed.deactivate(sym)

            elif action == "SUBMIT_ORDER":
                try:
                    sym = _clean_symbol(msg.get("symbol"), msg.get("market"))
                    if not sym:
                        raise ValueError("invalid symbol")
                    q = await asyncio.to_thread(feed.get_latest_quote, sym)
                    if q:
                        broker.on_quote(q)

                    ord_obj = await asyncio.to_thread(
                        broker.submit_order,
                        symbol=sym,
                        side=OrderSide(msg["side"]),
                        order_type=OrderType(msg["order_type"]),
                        quantity=int(msg["quantity"]),
                        price=float(msg["price"]) if msg.get("price") is not None else None,
                        stop_price=float(msg["stop_price"]) if msg.get("stop_price") is not None else None,
                    )
                    await websocket.send_json({"type": "ORDER_UPDATE", "data": ord_obj.model_dump(mode="json")})
                except Exception as ex:
                    await websocket.send_json({"type": "ORDER_ERROR", "error": str(ex)})

            elif action == "PING":
                await websocket.send_json({"type": "PONG"})

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as exc:
        # A malformed frame, a send error or a client that vanished must still
        # run cleanup; otherwise the connection leaks in active_connections.
        logger.warning("WebSocket session ended unexpectedly: %s", exc)
        ws_manager.disconnect(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


# Mount Production Frontend SPA if dist folder exists
static_dir = os.getenv("STATIC_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../frontend/dist")))


@app.get("/health")
async def health_probe():
    """Unprefixed liveness probe.

    Declared before the SPA mount on purpose: ``/health`` used to reach the
    catch-all 404 handler below, which answered with ``index.html`` and a 200.
    Any external monitor pointed at ``/health`` therefore reported "healthy"
    even while the process was failing every real request. This mirrors
    ``/api/health`` (the deploy gate's endpoint) so both probes agree.
    """
    return {
        "status": "ok",
        "service": "schwab-trader-hub",
        "market_data_provider": settings.market_data_provider,
        "broker_provider": settings.broker_provider,
        "markets": MARKETS.codes(),
    }


@app.exception_handler(404)
async def spa_404_fallback(request: Request, exc):
    index_path = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_path) and not request.url.path.startswith("/api/"):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

