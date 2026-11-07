"""
FastAPI Main Application Entrypoint for Schwab & Alpaca Trader Hub.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from urllib.parse import parse_qsl, urlencode
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.auth import token_matches
from app.models.trading import (
    Quote, Bar, Trade, Order, OrderSide, OrderType
)
from app.engine.portfolio_ledger import PortfolioLedger
from app.engine.matching_engine import VirtualMatchingEngine
from app.engine.market_feed import MarketFeedService
from app.engine.alpaca_market_feed import AlpacaMarketFeedService
from app.engine.alpaca_broker import AlpacaPaperBroker
from app.engine.base_broker import BaseBroker
from app.api.websocket_manager import WebSocketManager
from app.api.routes import create_api_router

# 1. Market Data Provider Factory
if settings.market_data_provider == "alpaca":
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_API_SECRET must be set via environment."
        )
    feed = AlpacaMarketFeedService(
        initial_symbols=settings.default_symbols,
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        stream_url=settings.alpaca_data_stream_url,
        data_base_url=settings.alpaca_data_base_url,
        feed=settings.alpaca_feed
    )
else:
    feed = MarketFeedService(initial_symbols=settings.default_symbols)

# 2. Broker / Execution Provider Factory
ledger = PortfolioLedger(initial_capital=settings.initial_capital)
virtual_engine = VirtualMatchingEngine(
    ledger=ledger,
    default_slippage_bps=settings.default_slippage_bps,
    default_commission_per_order=settings.default_commission
)

if settings.broker_provider == "alpaca_paper":
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_API_SECRET must be set via environment."
        )
    broker: BaseBroker = AlpacaPaperBroker(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        base_url=settings.alpaca_paper_base_url
    )
else:
    broker = virtual_engine

# Compatibility aliases
engine = broker

ws_manager = WebSocketManager()

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
    _schedule_coro(ws_manager.broadcast_quote(quote.model_dump(mode="json")))


def on_feed_bar(symbol: str, bar: Bar) -> None:
    _schedule_coro(ws_manager.broadcast_bar(symbol, bar.model_dump(mode="json")))


def on_feed_trade(trade: Trade) -> None:
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
        "portfolio": {
            "cash": p.cash if p else 0.0,
            "total_equity": p.total_equity if p else 0.0,
            "unrealized_pnl": p.unrealized_pnl if p else 0.0,
            "realized_pnl": p.realized_pnl if p else 0.0,
        }
    }
    await ws_manager.broadcast(payload)


def on_order_fill(trade: Trade, order: Order) -> None:
    _schedule_coro(_broadcast_order_filled(trade, order))


# Register callbacks
feed.subscribe_quote(on_feed_quote)
feed.subscribe_bar(on_feed_bar)
feed.subscribe_trade(on_feed_trade)
broker.register_fill_listener(on_order_fill)


def _configure_logging() -> None:
    """Make app loggers reach journald.

    uvicorn only configures its own ``uvicorn.*`` loggers, so the root logger
    keeps no handler and stdlib emits nothing below WARNING: every
    ``logger.info`` in this app was silently dropped in production.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(levelname)s [%(name)s] %(message)s"
        ))
        root.addHandler(handler)
    root.setLevel(level)


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


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
WS_AUTH_TIMEOUT_SEC = 5.0
LEGACY_TOKEN_KEY = "ws.legacy_query_token"


class LegacyQueryTokenMiddleware:
    """Hide the legacy ``?token=`` credential from the access log.

    uvicorn logs ``get_path_with_query_string(scope)`` once per handshake, so a
    tab still running the pre-AUTH bundle keeps writing the admin token to
    journald. Routing only uses ``scope["path"]``, so the token is moved into a
    scoped key here: the endpoint reads it through ``_legacy_query_token`` and
    the access log shows a bare ``/ws``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        params = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        tokens = [value for key, value in params if key == "token"]
        if not tokens:
            await self.app(scope, receive, send)
            return

        rest = [(key, value) for key, value in params if key != "token"]
        scrubbed = dict(scope)
        scrubbed["query_string"] = urlencode(rest).encode("latin-1")
        scrubbed.setdefault("state", {})
        scrubbed["state"][LEGACY_TOKEN_KEY] = tokens[0]
        await self.app(scrubbed, receive, send)


def _legacy_query_token(websocket: WebSocket) -> str | None:
    """Scoped token re-applied by the middleware, or the raw query outside it."""
    state = websocket.scope.get("state") or {}
    if LEGACY_TOKEN_KEY in state:
        return state[LEGACY_TOKEN_KEY]
    return websocket.query_params.get("token")


# Strip the legacy ?token= before uvicorn logs the scope.
app.add_middleware(LegacyQueryTokenMiddleware)


def _clean_symbol(value: object) -> str | None:
    """Only real ticker shapes may reach the feed or broker from a socket."""
    if not isinstance(value, str):
        return None
    sym = value.strip().upper()
    return sym if _SYMBOL_RE.match(sym) else None


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
                        "unrealized_pnl": pos.unrealized_pnl
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

    query_token = _legacy_query_token(websocket)
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
                sym = _clean_symbol(msg.get("symbol", "SPY"))
                if not sym:
                    await websocket.send_json({
                        "type": "SUBSCRIBE_ERROR", "error": "invalid symbol"
                    })
                    continue
                ws_manager.subscribe(websocket, sym)
                # Send immediate latest quote (off the event loop)
                q = await asyncio.to_thread(feed.get_latest_quote, sym)
                if q:
                    await websocket.send_json({"type": "QUOTE_UPDATE", "data": q.model_dump(mode="json")})

            elif action == "UNSUBSCRIBE":
                sym = _clean_symbol(msg.get("symbol"))
                if sym:
                    ws_manager.unsubscribe(websocket, sym)

            elif action == "SUBMIT_ORDER":
                try:
                    sym = _clean_symbol(msg.get("symbol"))
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


@app.exception_handler(404)
async def spa_404_fallback(request: Request, exc):
    index_path = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_path) and not request.url.path.startswith("/api/"):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

